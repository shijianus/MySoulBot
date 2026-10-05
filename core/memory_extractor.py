"""异步反思抽取器（非阻塞）。

一轮对话结束后，把最近的对话片段丢进后台队列，由轻量模型同时产出**两条轨**：

- 【事实】关于用户、跨对话仍然成立的客观情况 → `MEMORY.md`
- 【关系动态】我们之间相处方式的演变（什么有效、什么让对话变紧、对方在什么状态下
  不喜欢什么）→ `RELATIONS.md`

第二条轨就是「反思与自我演进」的自动通道：记的不只是他说了什么，而是我该怎么做。

关键约束：
- `submit()` 只入队，绝不 await LLM，因此不会拖慢流式回复。
- 两条轨共用**一次** LLM 调用，不额外增加延迟与费用。
- 单 worker 串行消费，天然避免并发写记忆；写盘另有存储层锁兜底。
- 任何异常都在这里收敛成日志 + 回调结果，永不冒泡回主对话循环。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from config import Settings
from core.storage_manager import DYNAMIC_MARK, FACT_LINE, StorageManager

logger: Final = logging.getLogger("mysoulbot.extractor")

Message = dict[str, str]
OutcomeCallback: Final = Callable[[str, list[str], list[str], str | None], "Awaitable[None] | None"]

NONE_TOKENS: Final[frozenset[str]] = frozenset({"none", "null", "n/a", "无", "没有", "无新事实"})
# 真实网关上观察到模型会漏掉左括号（`-2026-10-03] 事实`）或用全角括号，
# 这里只做标点级修复：仍然必须有合法日期 + 非空正文，否则照旧丢弃。
REPAIRABLE_FACT: Final[re.Pattern[str]] = re.compile(
    r"^[-*•]?\s*[【\[]?\s*(\d{4}-\d{2}-\d{2})\s*[】\]]?\s*[:：]?\s*(\S.*)$"
)
MIN_FACT_CHARS: Final[int] = 4
MAX_FACT_CHARS: Final[int] = 120
MAX_DYNAMIC_CHARS: Final[int] = 160
QUEUE_LIMIT: Final[int] = 128
DIALOGUE_MAX_CHARS: Final[int] = 6000

SYSTEM_PROMPT: Final[str] = """你是角色扮演引擎的记忆抽取器。你唯一的工作是从对话里读出两条轨，其余一概不管。

【A 事实】关于用户的、跨对话仍然成立的内容，必须同时满足：
1. 来源是**用户说的话**。角色自己的陈述、你的推测，都不算。
2. 它是稳定的：长期偏好与习惯、明确答应过的约定、现实情况（所在城市、职业、家人、身体状况、正在坚持的事）、用户明确表示不喜欢什么。
3. 它会改变角色之后对待用户的方式。「哦」「嗯」这类没有信息量的内容不抽。

【B 关系动态】我们之间**怎么相处**的演变。这是反思，不是流水账：
1. 必须从双方真实的互动里看得出来，不臆测、不总结成人生道理。
2. 它要能改变角色下一次的言行。写成可执行的分寸，例：「注意到对方疲惫时讨厌冗长建议，应保持精简陪伴」。
3. 对方的态度变化、我哪里说对了 / 说砸了，都可以记。
4. 大多数轮次没有新东西——没有就别写，不要为凑数造一条。

不要抽取：
- 只属于当下这一刻的情绪或状态（「今天有点累」不抽；「最近一直失眠」要抽）；
- 已经在「已有记忆」或「已有关系动态」里出现过的事，哪怕换了说法；
- 支付账号、证件号、密码等敏感信息；
- 用户对角色本身的夸赞（「你真温柔」）——那是印象，不是相处规律。

输出规则（严格执行，多余字符会导致写入失败）：
- 每条占一行，格式固定为：- [YYYY-MM-DD] 内容
- 属于【B 关系动态】的，在内容最前面加两个大于号，形如：- [YYYY-MM-DD] >>对方累了就嫌话多，宜短
- 日期一律使用「今日日期」给出的值，不要自己编。
- 句子不超过 40 个字。
- A 最多 {max_facts} 条，B 最多 {max_dynamics} 条，各自按重要程度排。
- 若两条轨都没有新内容，只输出一行：NONE"""

USER_BLOCK: Final[str] = """【已有记忆，不得重复】
{existing}

【已有关系动态，不得重复】
{relations}

【今日日期】
{today}

【最近对话】
{dialogue}

现在开始抽取。"""

NO_FACTS: Final[str] = "（空）"


@dataclass
class ExtractionOutcome:
    """一次抽取的结果，供调用方观察。"""

    user_id: str
    facts: list[str] = field(default_factory=list)
    dynamics: list[str] = field(default_factory=list)
    raw: str = ""
    error: str | None = None
    duplicates: int = 0
    llm_ms: int = 0

    @property
    def wrote(self) -> bool:
        return bool(self.facts or self.dynamics)


class MemoryExtractor:
    """后台事实抽取器。生命周期由 `start()` / `aclose()` 管理。"""

    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        *,
        on_outcome: OutcomeCallback | None = None,
        index: Any = None,  # noqa: ANN401 - core.vector_index.VectorIndex，引它会绕循环
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._on_outcome = on_outcome
        # 向量索引：新记下来的事顺手进索引，下次这句才勾得起它。
        # 它是加速器——传不进、写失败都不许影响记忆本身落盘。
        self._index = index
        self._client: AsyncOpenAI | None = None
        self._queue: asyncio.Queue[tuple[str, list[Message], dt.date]] = asyncio.Queue(
            maxsize=QUEUE_LIMIT
        )
        self._worker: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[ExtractionOutcome]] = set()
        self._inflight = 0
        self._stats: dict[str, int] = {
            "queued": 0,
            "written": 0,
            "reflected": 0,
            "empty": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------ 生命周期
    @property
    def enabled(self) -> bool:
        return self._settings.extractor_enabled

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def backlog(self) -> int:
        """待处理 + 正在处理中的条目数。`queue.qsize()` 会在 worker 取走后归零，
        所以必须加上在途计数，否则 `wait_idle()` 会在 LLM 请求飞行途中误报「已排空」。"""
        return self._queue.qsize() + self._inflight

    def start(self) -> None:
        """启动后台 worker；重复调用无副作用。"""
        if not self.enabled:
            logger.info("记忆抽取已禁用（EXTRACTOR_ENABLED=false）")
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="memory-extractor")

    async def wait_idle(self, timeout: float = 60.0) -> int:
        """等待队列中的任务全部处理完毕（含在途 LLM 请求），返回剩余积压数。"""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except TimeoutError:
            logger.warning("记忆抽取未在 %.0fs 内完成", timeout)
        return self.backlog

    async def aclose(self, timeout: float = 30.0) -> None:
        """等队列排空后关闭 worker；超时则放弃剩余任务。"""
        if self._worker is not None:
            await self.wait_idle(timeout)
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
        await self._close_client()

    async def _close_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            logger.debug("抽取客户端关闭异常", exc_info=True)
        self._client = None

    # ------------------------------------------------------------ 提交
    def submit(
        self, user_id: str, window: Sequence[Message], today: dt.date | None = None
    ) -> bool:
        """非阻塞入队。返回 False 表示本轮放弃抽取（未启用 / 内容不足 / 队列满）。"""
        if not self.enabled:
            return False
        messages = [dict(m) for m in window if str(m.get("content", "")).strip()]
        if len(messages) < 2:
            return False
        try:
            self._queue.put_nowait((user_id, messages, today or dt.date.today()))
        except asyncio.QueueFull:
            logger.warning("%s 抽取队列已满，跳过本轮（积压 %d）", user_id, self._queue.qsize())
            return False
        self._stats["queued"] += 1
        return True

    async def extract_now(
        self, user_id: str, window: Sequence[Message], today: dt.date | None = None
    ) -> ExtractionOutcome:
        """立即完整执行一次抽取并等待结果（给手动命令与测试用，不走队列）。"""
        return await self._process(user_id, [dict(m) for m in window], today or dt.date.today())

    # ------------------------------------------------------------ worker
    async def _run(self) -> None:
        logger.debug("抽取 worker 已启动")
        while True:
            user_id, window, today = await self._queue.get()
            self._inflight += 1
            try:
                outcome = await self._process(user_id, window, today)
            except Exception as exc:  # noqa: BLE001 - worker 必须活着
                logger.error("抽取任务异常: %s\n%s", exc, traceback.format_exc())
                self._stats["failed"] += 1
                outcome = ExtractionOutcome(
                    user_id=user_id, error=f"{type(exc).__name__}: {exc}"
                )
            finally:
                self._inflight -= 1
                self._queue.task_done()
            await self._notify_safe(outcome)

    async def _process(
        self, user_id: str, window: list[Message], today: dt.date
    ) -> ExtractionOutcome:
        outcome = ExtractionOutcome(user_id=user_id)
        if not self._settings.extractor_enabled:
            outcome.error = "抽取器未启用"
            return outcome
        if len(window) < 2:
            outcome.error = "对话不足两条，跳过"
            return outcome

        started = time.perf_counter()
        try:
            raw = await self._call_llm(user_id, window, today)
            if not raw.strip():
                # 推理型模型偶尔把预算全花在思考上导致正文为空，重试一次再判失败
                logger.info("%s 抽取返回空正文，重试一次", user_id)
                raw = await self._call_llm(user_id, window, today)
        except (APIConnectionError, APITimeoutError) as exc:
            self._stats["failed"] += 1
            outcome.error = f"接口不可达（{type(exc).__name__}）"
            logger.warning("记忆抽取网络失败: %s", outcome.error)
            return outcome
        except APIStatusError as exc:
            self._stats["failed"] += 1
            outcome.error = f"接口返回 HTTP {exc.status_code}"
            logger.warning("记忆抽取失败 %s: %s", exc.status_code, str(exc)[:200])
            return outcome
        except Exception as exc:  # noqa: BLE001
            self._stats["failed"] += 1
            outcome.error = f"{type(exc).__name__}: {exc}"
            logger.warning("记忆抽取调用异常: %s", outcome.error)
            return outcome

        outcome.raw = raw
        outcome.llm_ms = int((time.perf_counter() - started) * 1000)
        if not raw.strip():
            self._stats["failed"] += 1
            outcome.error = (
                "抽取模型返回空正文——推理型模型会把正文留在 reasoning_content，"
                "需要调高 EXTRACTOR_MAX_TOKENS 或换一个非推理模型"
            )
            logger.warning("%s %s", user_id, outcome.error)
            return outcome

        facts, dynamics = self._parse(raw)
        if not facts and not dynamics:
            self._stats["empty"] += 1
            logger.debug("%s 本轮无新内容（%dms）", user_id, outcome.llm_ms)
            return outcome

        try:
            outcome.facts = await self._storage.append_facts(user_id, facts, on_date=today)
            outcome.dynamics = (
                await self._storage.append_dynamics(user_id, dynamics, on_date=today)
                if dynamics and self._settings.reflection_enabled
                else []
            )
            await self._index_writes(user_id, outcome, today)
        except Exception as exc:  # noqa: BLE001 - 写盘失败不影响对话
            self._stats["failed"] += 1
            outcome.error = f"写入记忆失败：{type(exc).__name__}: {exc}"
            logger.error("%s 记忆落盘失败: %s", user_id, exc)
            return outcome

        outcome.duplicates = max(0, len(facts) + len(dynamics) - len(outcome.facts) - len(outcome.dynamics))
        self._stats["written"] += len(outcome.facts)
        self._stats["reflected"] += len(outcome.dynamics)
        if outcome.facts:
            logger.info("%s 新增长期记忆 %d 条", user_id, len(outcome.facts))
        if outcome.dynamics:
            logger.info("%s 新增关系动态 %d 条", user_id, len(outcome.dynamics))
        return outcome

    # ------------------------------------------------------------ LLM
    async def _index_writes(self, user_id: str, outcome: ExtractionOutcome,
                            today: dt.date) -> None:
        """把这一轮真正落盘的记忆喂进向量索引。

        失败只是「下次这句勾不起它」，不是记忆丢了——md 文件才是真相来源，
        索引只是那条让人想起东西的路。所以这里吞掉一切异常，绝不带崩抽取。
        """
        index = getattr(self, "_index", None)
        if index is None or not getattr(index, "enabled", False):
            return
        day = today.isoformat()
        try:
            if outcome.facts:
                await index.upsert(user_id, "fact", [(day, text) for text in outcome.facts])
            if outcome.dynamics:
                await index.upsert(user_id, "relation", [(day, text) for text in outcome.dynamics])
        except Exception as exc:  # noqa: BLE001 - 加速器坏了不许连累记忆写入
            logger.debug("记忆进向量索引失败（忽略）：%s", exc)

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key, base_url = self._settings.extractor_credentials()
            self._client = AsyncOpenAI(
                api_key=api_key or "EMPTY",
                base_url=base_url,
                timeout=self._settings.extractor_timeout,
                max_retries=1,
            )
        return self._client

    async def _call_llm(self, user_id: str, window: list[Message], today: dt.date) -> str:
        existing, relations = await asyncio.gather(
            self._storage.read_facts(user_id), self._storage.read_relations(user_id)
        )
        prompt = USER_BLOCK.format(
            existing=self._render_existing(existing),
            relations=self._render_existing(relations),
            today=today.isoformat(),
            dialogue=self._render_dialogue(window),
        )
        limit = self._settings.extractor_max_facts
        system = SYSTEM_PROMPT.format(
            max_facts=limit, max_dynamics=limit if self._settings.reflection_enabled else 0
        )
        response = await self._get_client().chat.completions.create(
            model=self._settings.effective_extractor_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=self._settings.extractor_temperature,
            max_tokens=self._settings.extractor_max_tokens,
            stream=False,
            timeout=self._settings.extractor_timeout,
        )
        return self._content_of(response)

    @staticmethod
    def _content_of(response: Any) -> str:
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError) as exc:
            raise RuntimeError("抽取返回结构异常") from exc
        text = getattr(message, "content", None)
        if isinstance(text, list):  # 部分兼容接口返回分段内容
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in text
            )
        return str(text or "").strip()

    @staticmethod
    def _render_existing(facts: Sequence[tuple[str, str]]) -> str:
        if not facts:
            return NO_FACTS
        return "\n".join(f"- [{day}] {text}" for day, text in facts[-30:])

    @staticmethod
    def _render_dialogue(window: Sequence[Message]) -> str:
        lines: list[str] = []
        total = 0
        for item in reversed(window):
            role = "用户" if item.get("role") == "user" else "角色"
            content = str(item.get("content", "")).strip().replace("\n", " ")
            line = f"{role}: {content}"
            total += len(line)
            if total > DIALOGUE_MAX_CHARS:
                lines.append("……（更早内容略）")
                break
            lines.append(line)
        lines.reverse()
        return "\n".join(lines)

    # ------------------------------------------------------------ 解析
    def _parse(self, raw: str) -> tuple[list[str], list[str]]:
        """把抽取输出分成【事实】与【关系动态】两轨，只接受 `- [YYYY-MM-DD] 内容` 严格格式。

        两轨靠 `>>` 前缀区分（模型侧的唯一额外要求）；没带前缀的一律进事实轨，
        所以旧的抽取行为完全不受影响。日期由引擎统一盖章
        （见 `StorageManager._append_entries` 的 on_date），模型写的日期只用于校验格式。
        """
        cleaned = re.sub(r"```[a-zA-Z]*", " ", raw or "").replace("```", " ")
        facts: list[str] = []
        dynamics: list[str] = []
        dropped: list[str] = []
        repaired = 0
        for line in cleaned.splitlines():
            text = line.strip().strip("`")
            if not text:
                continue
            lowered = text.casefold()
            if lowered in NONE_TOKENS or lowered.startswith("none"):
                continue
            match = FACT_LINE.match(text)
            if match is None:
                match = REPAIRABLE_FACT.match(text)
                if match is None:
                    dropped.append(text)
                    continue
                repaired += 1
            body = match.group(2).strip()
            is_dynamic = body.startswith((DYNAMIC_MARK, "》》", ">>"))
            entry = body.lstrip(">》").strip() if is_dynamic else body
            entry = entry.strip("。.；;，,、 ")
            ceiling = MAX_DYNAMIC_CHARS if is_dynamic else MAX_FACT_CHARS
            if not MIN_FACT_CHARS <= len(entry) <= ceiling:
                dropped.append(text)
                continue
            bucket = dynamics if is_dynamic else facts
            if entry not in bucket:
                bucket.append(entry)
        if repaired:
            logger.info("抽取输出有 %d 行括号残缺，已按日期前缀修复", repaired)
        if dropped:
            logger.warning(
                "抽取输出有 %d 行不符合 `- [YYYY-MM-DD] 内容` 格式，已丢弃：%s",
                len(dropped),
                " | ".join(item[:40] for item in dropped[:3]),
            )
        limit = self._settings.extractor_max_facts
        return facts[:limit], dynamics[:limit]

    # ------------------------------------------------------------ 通知
    async def _notify_safe(self, outcome: ExtractionOutcome) -> None:
        if self._on_outcome is None:
            return
        try:
            result = self._on_outcome(
                outcome.user_id, outcome.facts, outcome.dynamics, outcome.error
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - 回调故障不得影响 worker
            logger.debug("抽取回调异常", exc_info=True)
