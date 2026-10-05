"""核心对话调度器。

串联「分层存储 → 双层灵魂 Prompt → 流式模型调用 → 静默工具往返 → 日志落盘 → 后台反思抽取」。
支持任意 OpenAI 兼容接口（base_url + api_key 由 config 注入）。

工具往返对用户不可见：模型下单 → 引擎静默执行 → 结果只回到模型的消息列表里 →
模型用角色自己的话说出来。界面上永远只有一段角色表达。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from config import Settings
from core.card_loader import PersonaLibrary, Preset
from core.clawd_soul import ClawdSoul
from core.mood_soul import MoodSoul
from core.identity import resolve_identity
from core.recap import SessionRecap
from core.cognition import CognitionLoop
from core.judgment import JudgmentLoop, observe
from core.memory_extractor import MemoryExtractor
from core.prompt_builder import (
    TIER_FULL,
    TIER_QUICK,
    Message,
    PromptBuilder,
    PromptLayers,
    history_from_log_records,
)
from core.upstream import Route, UpstreamPool
from core.presence import (
    SLOTS,
    Mood,
    Patience,
    Presence,
    assess_mood,
    build_presence,
    resolve_now,
)
from core.rapport import Rapport, RapportEngine
from core.storage_manager import StorageManager, StorageError
from core.tools.base import ToolContext
from core.tools.protocol import Directive, StreamGuard, extract_directives
from core.tools.registry import ToolRegistry
from core.vision import (
    ImageRef,
    VisionError,
    content_parts,
    ingest,
    note as vision_note,
    trim,
)

logger: Final = logging.getLogger("mysoulbot.bot")

# 整条回复只由这些词拼成时，等于没说。「收到」「好的」不配当回答——重问一次，
# 也别递到对方面前。判定用「覆盖」而不是正则：把「好的收到明白了」这种拼起来的也认出来。
_FILLER_WORDS: Final[tuple[str, ...]] = (
    "收到", "好的", "好嘞", "好", "嗯哼", "嗯", "哦", "噢", "哎", "行吧", "行", "成", "得嘞",
    "明白了", "明白", "了解了", "了解", "知道了", "知道", "没问题", "没事", "有道理", "确实",
    "辛苦了", "辛苦", "okay", "ok", "yep", "yes", "哈哈", "呵呵", "嘿嘿", "哈", "呵",
)
_FILLER_STRIP: Final[str] = "。！!？~、.,…， ：:　\t "

_REASK_HINT: Final[str] = (
    "（上一条你没给出实际内容。就着对方这句话直接回，说一个具体的点；"
    "不许只回「收到」「好的」「嗯」这类词。）"
)


def _has_substance(text: str) -> bool:
    """这一句接住话了没有：空、或者整句只由套话拼成，都算没接住。"""
    body = (text or "").strip().strip(_FILLER_STRIP).strip()
    if not body or len(body) > 20:
        return bool(body)
    covered, index = 0, 0
    while index < len(body):
        for word in sorted(_FILLER_WORDS, key=len, reverse=True):
            if body.startswith(word, index):
                index += len(word)
                covered += len(word)
                break
        else:
            return True  # 有一个字不在套话表里，就说明她真的说了话
    return covered < len(body) if body else False
HISTORY_MULTIPLIER: Final[int] = 4
TOOL_TRAIL_ROUNDS: Final[int] = 3


def _flatten_line(text: str) -> str:
    """比「同一行」之前先抹平差异：换行、连续空格与大小写都不算两个样子。"""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


class BotError(RuntimeError):
    """已经过翻译、可直接展示给用户的错误。"""

    def __init__(self, message: str, *, hint: str = "", cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.cause = cause

    def __str__(self) -> str:
        return f"{self.message}（{self.hint}）" if self.hint else self.message


@dataclass
class Session:
    """单个用户的会话状态。"""

    user_id: str
    history: list[Message] = field(default_factory=list)
    turns: int = 0
    busy: bool = False
    last_layers: PromptLayers | None = None
    persona_slug: str = ""
    persona_name: str = ""
    gen_params: dict[str, Any] = field(default_factory=dict)
    tool_mode: str = ""  # ""=自动；接口不认原生工具时被降级成 "inline"
    # 上一句我们说完的时间与原文：判断回路要量的「他隔多久回的、回了多长」
    # 只有下一回合开头才知道，所以得在这儿留个底
    last_reply_at: float = 0.0
    last_reply_text: str = ""
    last_bubbles: int = 1
    vision_mode: str = ""  # ""=自动；接口不认多模态内容时被降级成 "off"
    tool_calls: int = 0
    # 体温与温度：一次刷新，整轮复用（保证同一轮内重复组装 prompt 结果一致）
    stamp: dt.datetime = field(default_factory=lambda: dt.datetime.now().astimezone())
    presence: Any = None  # noqa: ANN401 - core.presence.Presence
    rapport: Any = None  # noqa: ANN401 - core.rapport.Rapport
    state: dict[str, Any] = field(default_factory=dict)
    facts_seen: int = 0
    dynamics_seen: int = 0

    def append(self, role: str, content: str, cap: int) -> None:
        self.history.append({"role": role, "content": content})
        limit = max(cap, 2) * HISTORY_MULTIPLIER
        if len(self.history) > limit:
            del self.history[: -limit]

    def merged_params(self, settings: Settings) -> dict[str, Any]:
        """全局配置打底，人格级参数覆盖。"""
        params: dict[str, Any] = {
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "max_tokens": settings.max_tokens,
            "frequency_penalty": settings.frequency_penalty,
        }
        params.update(self.gen_params)
        return params


@dataclass
class PersonaApplied:
    """一次人格切换的结果。"""

    slug: str
    name: str
    title: str
    previous_slug: str
    backup_path: Path | None
    greeting: str
    history_reset: bool
    config: dict[str, float] = field(default_factory=dict)


class MySoulBot:
    """对话引擎门面。CLI / 未来的 HTTP 层都只依赖它。"""

    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        prompt_builder: PromptBuilder,
        extractor: MemoryExtractor,
        library: PersonaLibrary | None = None,
        clawd: ClawdSoul | None = None,
        recap: SessionRecap | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._prompts = prompt_builder
        self._extractor = extractor
        self.library = library or PersonaLibrary(settings)
        self.clawd = clawd or ClawdSoul(settings)
        self.mood = MoodSoul(settings)
        # QQ 账号操作口：网桥在服务起来之后才建好，只能事后 bind
        self.qq: Any = None
        # 回看只有一个实例：写的人（引擎）和读的人（提示词装配）必须共用同一份后台队列，
        # 各开各的就会出现「写了但等的是自己那队」这种丢要点
        self.recap = recap or SessionRecap(settings, storage)
        self._prompts.bind_recap(self.recap)
        self.cognition = CognitionLoop(settings, storage, self.mood)
        # 判断回路：后端根据真实结果攒「怎么说」，写进 JUDGMENT.md，下一轮直接改前端取舍
        self.judgment = JudgmentLoop(settings, storage)
        self._prompts.bind_judgment(self.judgment.ledger)
        self.rapport = RapportEngine(settings, storage)
        self._client: AsyncOpenAI | None = None
        self._client_hook: Any = None   # 探针接管位：scripts 用它按线路包一层记账壳
        # 上游路由池：分档选路 + 按实测速度均衡 + 同一回合内换家。没配 ROUTES 就是一条线，
        # 行为与从前一致
        self.routes = UpstreamPool(settings)
        self._pool_clients: dict[str, AsyncOpenAI] = {}
        self._sessions: dict[str, Session] = {}
        self._today = dt.date.today()

    # ------------------------------------------------------------ 资源
    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def storage(self) -> StorageManager:
        return self._storage

    @property
    def extractor(self) -> MemoryExtractor:
        return self._extractor

    @property
    def model(self) -> str:
        return self._settings.model

    def registry(self, user_id: str, *, group_mode: bool | None = None) -> ToolRegistry:
        """给某个用户装配可用工具。工具层失败不影响对话——空注册表就是「没有能使的劲」。

        `group_mode` 是回合级场景：QQ 那一头同一秒可能既在群里又在私聊里，
        工具的群聊锁（例如「群聊里不改写灵魂」）必须跟着这一句话的场合走，而不是跟着全局配置。
        """
        if not self._settings.tools_enabled:
            return ToolRegistry(self._context(user_id, group_mode), [])
        try:
            return ToolRegistry(self._context(user_id, group_mode))
        except Exception as exc:  # noqa: BLE001 - 工具装配失败只是少点能力
            logger.warning("工具装配失败，本轮无工具可用: %s", exc)
            return ToolRegistry(self._context(user_id, group_mode), [])

    def _context(self, user_id: str, group_mode: bool | None = None) -> ToolContext:
        return ToolContext(
            settings=self._scene(group_mode), storage=self._storage, user_id=user_id,
            clawd=self.clawd, mood=self.mood,
            identity=resolve_identity(self._settings, user_id,
                                      source="group" if group_mode else "solo"),
            qq=self.qq,
        )

    def bind_vector_index(self, index: Any) -> None:  # noqa: ANN401 - VectorIndex
        """把检索端接到同一份索引上。没绑就是按最近 N 条取记忆——旧行为，不是坏了。"""
        self._prompts.bind_vector(index)

    def bind_qq_port(self, port: Any) -> None:  # noqa: ANN401 - OneBotBridge，引它会循环
        """把网桥交给引擎，账号级工具才有手可以伸。

        网桥是在服务起来之后才建好的，所以只能事后绑，不在构造参数里传。
        没绑上就是 None——那些工具直接不可用，而不是拿个空壳去假装能发。
        """
        self.qq = port

    def _scene(self, group_mode: bool | None) -> Settings:
        """把「这一句话的场合」换成一份只在这一回合生效的配置副本。

        不改全局 `chat_mode`：那会让同一进程里下一条私聊消息也带上群聊准则，
        而 QQ 恰恰是群与私聊共用一个引擎实例的那个入口。
        """
        if group_mode is None or group_mode == self._settings.group_mode:
            return self._settings
        return self._settings.model_copy(update={"chat_mode": "group" if group_mode else "solo"})

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._settings.api_key or "EMPTY",
                base_url=self._settings.base_url,
                timeout=self._settings.request_timeout,
                max_retries=self._settings.max_retries,
            )
        return self._client

    def client_for(self, route: Route) -> AsyncOpenAI:
        """这条线的真客户端，按 (base_url, key) 缓存。"""
        client = self._pool_clients.get(route.client_id())
        if client is None:
            client = AsyncOpenAI(
                api_key=route.api_key or "EMPTY",
                base_url=route.base_url,
                timeout=route.timeout or self._settings.request_timeout,
                max_retries=self._settings.max_retries,
            )
            self._pool_clients[route.client_id()] = client
        return client

    async def ask_once(self, prompt: str, *, max_tokens: int = 120,
                       timeout: float = 20.0) -> str:
        """后台问一句，拿纯文本回来。给配对口令、招呼语这类短产出用。

        刻意不复用对话那条链：那条要挂人格、要切档、要落历史，而这里要的只是
        「按这个人的口气现编十个字」。失败就返回空串，由调用方决定兜底——
        配对不能因为一次生成失败就卡死。
        """
        # 候选 = 对话线路池 + 抽取器那条线。后者才是这里该用的：cognition / recap /
        # 记忆抽取这些后台生成都走 extractor_credentials()，它专挑便宜的小模型，
        # 而对话池里的 glm 对「只给十个字」这种短提示会只思考不落正文。
        candidates: list[tuple[str, str, str]] = [
            (route.name, route.model, "") for route in self.routes.routes
        ]
        ex_key, ex_base = self._settings.extractor_credentials()
        if ex_base:
            candidates.append(("extractor",
                               self._settings.extractor_model or self._settings.model, ex_base))

        last_error = ""
        for name, model, base in candidates:
            try:
                if base:
                    client = AsyncOpenAI(api_key=ex_key or "EMPTY", base_url=base,
                                         timeout=timeout, max_retries=0)
                else:
                    route = self.routes.route(name)
                    if route is None:
                        continue
                    client = self.client_for(route)
                completion = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.9, max_tokens=max_tokens,
                    ), timeout=timeout)
                body = str(completion.choices[0].message.content or "").strip()
                if body:
                    return body
                last_error = f"{name} 回了空"
            except Exception as exc:  # noqa: BLE001 - 换下一条线，别把配对卡死
                last_error = f"{name}: {str(exc)[:100]}"
            continue
        logger.warning("现生成没成功（%s），交调用方兜底", last_error or "没有可用线路")
        return ""

    def _route_client(self, route: Route) -> AsyncOpenAI:
        """取这条线的客户端；探针（`_client_hook`）接管时也要**按线路**各包一层。

        以前是「一旦有探针就全家共用它那一个客户端」——那等于把多线路悄悄打回一条：
        日志里模型名换成了 sol61，请求却还发给 GLM 那个域名，回退全变成 503。
        """
        if self._client_hook is not None:
            return self._client_hook(route, self.client_for)
        return self.client_for(route)

    def _fold_history(self, session: Session) -> None:
        """逐字只留最近几条，更早的压进会话回看。

        为什么要收口：原文一路堆到几十条，代价不是「她懂得更多」，而是上游多想一倍时间、
        更容易一个字都说不出，真要点反而抓不住。
        收口不等于忘掉——被挤出去的那些交去压成要点，下一句照样接得上。
        """
        if not self.recap.enabled:
            return
        tail = max(2, int(self._settings.recap_tail_turns))
        if len(session.history) <= tail:
            return
        dropped, session.history = session.history[:-tail], session.history[-tail:]
        lines = [str(item.get("content") or "") for item in dropped if item.get("content")]
        if lines:
            self.recap.note(session.user_id, lines)

    async def aclose(self) -> None:
        await self.recap.aclose()
        await self.cognition.aclose()
        for client in list(self._pool_clients.values()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("线路客户端关闭异常", exc_info=True)
        self._pool_clients.clear()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("对话客户端关闭异常", exc_info=True)
            self._client = None

    # ------------------------------------------------------------ 会话
    async def open_session(
        self, user_id: str, *, soul_text: str | None = None, restore: bool = True
    ) -> Session:
        """初始化用户目录并（可选）从日志恢复上下文。"""
        await self._storage.ensure_user(user_id, soul_text=soul_text)
        session = Session(user_id=user_id)
        if restore and self._settings.soul_files_only:
            # 红线：日志只做留档，不做上下文回放。同一会话内的多轮照旧连着说，
            # 但重启后不把过去聊过什么重新塞回提示词。
            restore = False
        if restore:
            limit = self._settings.context_max_turns
            try:
                records = await self._storage.read_recent_transcript(user_id, limit)
            except StorageError as exc:
                logger.warning("上下文恢复失败: %s", exc)
                records = []
            session.history = history_from_log_records(records)[-limit:]
            if session.history:
                logger.info("已为 %s 恢复 %d 条上下文", user_id, len(session.history))
        session.stamp = resolve_now(None, self._settings.user_timezone)
        try:
            session.state = await self._storage.read_state(user_id)
        except Exception as exc:  # noqa: BLE001 - 体温读不出来就当刚醒，别挡住对话
            logger.warning("%s 状态读取失败: %s", user_id, exc)
            session.state = {}
        meta = await self._storage.read_persona_meta(user_id)
        session.persona_slug = str(meta.get("slug") or "")
        session.persona_name = str(meta.get("name") or "")
        config = meta.get("config")
        session.gen_params = dict(config) if isinstance(config, dict) else {}
        self._sessions[user_id] = session
        return session

    def session(self, user_id: str) -> Session:
        session = self._sessions.get(user_id)
        if session is None:
            raise BotError(f"会话 {user_id} 未打开")
        return session

    # ------------------------------------------------------------ 人格
    async def apply_persona(
        self, user_id: str, preset: Preset, *, keep_history: bool = False, greet: bool = True
    ) -> PersonaApplied:
        """把人格预设应用到当前用户。

        语义：**记忆与画像保留，SOUL 替换并备份，近期上下文默认重置**。
        理由：MEMORY.md 记录的是「用户」的事实，与谁来扮演不相关，换人不该失忆；
        而旧上下文里满是上一任角色的语气，留着会污染新人格。
        """
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        previous_slug = session.persona_slug
        soul_text = preset.soul_text()
        if not soul_text.strip():
            raise BotError(f"人格 {preset.slug} 的 SOUL.md 是空的，已中止切换")

        backup = await self._storage.backup_doc(user_id, "SOUL")
        await self._storage.write_doc(user_id, "SOUL", soul_text)
        await self._storage.write_persona_meta(
            user_id,
            {
                "schema_version": 1,
                "slug": preset.slug,
                "name": preset.name,
                "title": preset.title,
                "source": preset.source,
                "config": preset.config,
                "first_mes": preset.first_mes,
                "applied_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
        session.persona_slug = preset.slug
        session.persona_name = preset.name
        session.gen_params = dict(preset.config)

        reset = False
        if not keep_history:
            session.history.clear()
            session.turns = 0
            reset = True

        greeting = ""
        if greet and preset.first_mes and (reset or not session.history):
            greeting = preset.first_mes.strip()
            session.append("assistant", greeting, self._settings.context_max_turns)
            await self._storage.append_transcript(
                user_id, [{"role": "assistant", "content": greeting, "kind": "greeting"}]
            )

        logger.info("已为 %s 应用人格 %s（上下文%s）", user_id, preset.slug, "保留" if keep_history else "重置")
        return PersonaApplied(
            slug=preset.slug,
            name=preset.name,
            title=preset.title,
            previous_slug=previous_slug,
            backup_path=backup,
            greeting=greeting,
            history_reset=reset,
            config=dict(preset.config),
        )

    # ------------------------------------------------------------ Prompt
    async def preview_prompt(
        self, user_id: str, *, today: dt.date | None = None, tools: ToolRegistry | None = None
    ) -> tuple[str, PromptLayers]:
        """返回当前将用于对话的 system prompt 与分层明细。"""
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        registry = tools if tools is not None else self.registry(user_id)
        presence, rapport = await self._pulse(session, persist=False)
        return await self._prompts.build_system_prompt(
            user_id,
            session.history,
            today=today or self._today,
            tool_mode=self._tool_mode(session, registry),
            tools=registry,
            presence=presence,
            rapport=rapport,
        )

    # ------------------------------------------------------------ 体温与温度
    async def _pulse(self, session: Session, *, persist: bool) -> tuple[Presence, Any]:
        """刷新这一轮的「此刻」与「我们之间」。

        温度只在**真实回合**里结算（`persist=True`）：上一轮后台攒下的事实与分寸
        此时才看得见，增量是真的。预览走只读路径，绝不累积——否则 `/panel prompt`
        连查两次就会凭空攒出 0.35 度，破坏同轮组装的一致性。
        """
        stale = Presence(stamp=session.stamp, slot=SLOTS[6], patience=Patience())
        published = await self.rapport.read(session.user_id)
        try:
            presence = build_presence(self._settings, session.state, session.stamp)
            if not persist:
                session.presence, session.rapport = presence, published
                return presence, published
            facts, dynamics = await asyncio.gather(
                self._storage.read_facts(session.user_id),
                self._storage.read_relations(session.user_id),
            )
            counters = dict(session.state.get("rapport") or {})
            rapport, counters = self.rapport.advance(
                published,
                counters,
                now=session.stamp,
                gap_days=presence.gap_days,
                late_night=presence.deep_night,
                repair=bool(session.state.get("pending_repair")),
                disclosure_delta=max(0, len(facts) - session.facts_seen),
                dynamic_delta=max(0, len(dynamics) - session.dynamics_seen),
            )
            session.facts_seen = max(session.facts_seen, len(facts))
            session.dynamics_seen = max(session.dynamics_seen, len(dynamics))
            session.state["rapport"] = counters
            session.state["pending_repair"] = False
            session.state.update(presence.to_state())
            await self._storage.write_state(session.user_id, session.state)
            await self.rapport.publish(session.user_id, rapport)
            session.presence, session.rapport = presence, rapport
            return presence, rapport
        except Exception as exc:  # noqa: BLE001 - 体温故障绝不打断回复
            logger.warning("%s 体温刷新失败: %s", session.user_id, exc)
            session.presence, session.rapport = stale, published
            return stale, published

    def presence_of(self, user_id: str) -> Presence | None:
        """当前体温快照（未对话过时为 None）。"""
        session = self._sessions.get(user_id)
        return session.presence if session else None

    def rapport_of(self, user_id: str) -> Any:  # noqa: ANN401 - Rapport
        session = self._sessions.get(user_id)
        return session.rapport if session else None


    # ------------------------------------------------------------ 对话
    async def stream_reply(
        self,
        user_id: str,
        user_text: str,
        *,
        today: dt.date | None = None,
        speakers: list[str] | None = None,
        now: dt.datetime | None = None,
        images: Sequence[Any] = (),  # noqa: ANN401 - core.vision.ImageRef 或图片来源字符串
        group_mode: bool | None = None,
        external_origin: bool = False,
        group_discretion: bool = False,
    ) -> AsyncIterator[str]:
        """流式产出一段角色回复。

        界面上只会看到角色说的话：工具下单被就地剥掉，执行过程完全静默，
        结果只补进送给模型的消息列表。生成器结束时，日志与反思抽取已提交后台。

        `images` 走原生多模态通道：模型是真的在看，不是在读一段别人的转述。
        `group_mode` 锁定这一回合的场景（群聊准则 + 工具群聊锁），留空跟随 CHAT_MODE。
        `external_origin` 表示话来自外部协议端（QQ）而非缔造者本人，挂外部客体准则。
        `group_discretion` 只给没被点名的群消息：这一句说不说话由她自己裁决。
        """
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        if session.busy:
            raise BotError("上一条回复还在生成中")
        session.busy = True

        day = today or self._today
        session.stamp = resolve_now(now, self._settings.user_timezone)
        registry = self.registry(user_id, group_mode=group_mode)
        mode = self._tool_mode(session, registry)
        params = session.merged_params(self._settings)
        refs, problems = await self._ingest(user_id, images)
        presence, rapport = await self._pulse(session, persist=True)
        can_see = self._can_see(session)
        vision_on = can_see and bool(refs)
        guard = StreamGuard(trim_closers=self._settings.trim_stock_closers)
        visible: list[str] = []
        ask_extra = ""       # 最后重问那趟附加的要求，只进这一次请求，不落盘不改历史
        groups: list[list[Message]] = []  # 每组=一次「下单+结果」，永不拆开，避免留下无主的 tool 消息
        rounds = 0
        completed = False
        escalated = False  # 正文被思考吃光时，允许把预算抬一档重跑一次
        nudged = False     # 抬档还不够时，最后把要求说白再问一次
        try:
            rebuild = True
            while True:
                if rebuild:
                    messages, session.last_layers = await self._prompts.build_messages(
                        user_id,
                        f"{user_text}\n{ask_extra}" if ask_extra else user_text,
                        session.history,
                        today=day,
                        tool_mode=mode,
                        tools=registry,
                        speakers=speakers,
                        presence=presence,
                        rapport=rapport,
                        images=refs,
                        vision_on=vision_on,
                        media_extra=problems,
                        group_mode=group_mode,
                        external_origin=external_origin,
                        group_discretion=group_discretion,
                    )
                    rebuild = False
                # 快捷档那一趟连工具清单都不挂：六个开关的名字就够把她按进一分多钟的盘算里，
                # 而「在吗」这种话本来就不需要她动手。同一趟也只给它开对冲：正文十几个字，
                # 多烧一把不心疼；一万字的重回合补一把等于把大请求付两遍。
                quick = session.last_layers.tier == TIER_QUICK
                sink: dict[str, Any] = {"tool_calls": []}
                ordered: list[Directive] = []
                try:
                    async for delta in self._call_stream(
                        messages + [m for group in groups for m in group],
                        params,
                        tools=registry.native_specs() if mode == "native" and not quick else None,
                        sink=sink,
                        hedge=quick,
                        tier=session.last_layers.tier,
                    ):
                        shown, found = guard.feed(delta)
                        ordered.extend(found)
                        if shown:
                            visible.append(shown)
                            yield shown
                except BotError as exc:
                    if self._should_degrade_native(exc, mode, groups, visible):
                        mode = "inline"
                        session.tool_mode = "inline"
                        rebuild = True
                        logger.info("接口不认原生工具调用，本轮改用行内暗号继续")
                        continue
                    if self._should_degrade_vision(exc, vision_on, groups, visible):
                        vision_on = False
                        can_see = False
                        session.vision_mode = "off"
                        groups = []
                        rebuild = True
                        logger.info("接口不认多模态内容，本轮退回：只承认收到图，不假装看见")
                        continue
                    if groups or "".join(visible).strip():
                        # 工具结果回填被拒或中途出错：已有内容照旧收尾，不甩机械错误
                        logger.info("工具往返中断，用已有内容收尾：%s", exc.message)
                        break
                    raise
                tail, tail_dirs = guard.flush()
                ordered.extend(tail_dirs)
                if tail:
                    visible.append(tail)
                    yield tail

                calls = self._collect_calls(mode, sink, ordered, registry, echo_of=user_text)
                if not calls:
                    empty_handed = not _has_substance("".join(visible))
                    if empty_handed and self._escalate_budget(params, escalated):
                        # 想满预算、正文一个字不剩：抬高一档重跑一次，别把空手当成回答送出去
                        escalated = True
                        visible = []
                        guard = StreamGuard(trim_closers=self._settings.trim_stock_closers)
                        logger.info("这一轮正文被思考吃光了，预算抬到 %s 重跑", params["max_tokens"])
                        continue
                    if empty_handed and not nudged:
                        # 预算已经抬到顶还是空手：最后把要求说白，再问一次。
                        # 宁可多说一句「就着这话回」，也不换成一句现成的套话糊弄过去
                        nudged = True
                        visible = []
                        guard = StreamGuard(trim_closers=self._settings.trim_stock_closers)
                        rebuild = True
                        ask_extra = _REASK_HINT
                        logger.info("重跑还是空手，加一句明确的要求再问一次")
                        continue
                    break
                if rounds >= self._settings.tool_max_rounds:
                    logger.info("本轮工具往返已达上限 %d，剩下的单不接", self._settings.tool_max_rounds)
                    break
                rounds += 1
                session.tool_calls += len(calls)
                groups = await self._run_tools(calls, groups, registry, mode, can_see)
            completed = True
        finally:
            try:
                await self._finalize(
                    session,
                    user_text,
                    "".join(visible),
                    not completed,
                    day,
                    [ref.label() for ref in refs],
                    group_mode=bool(group_mode),
                )
            finally:
                session.busy = False
        if completed and not _has_substance("".join(visible)):
            raise BotError(
                "模型没有返回可见内容",
                hint=(
                    "多为推理型模型把 MAX_TOKENS 全部消耗在思考上（本接口会把正文留在 "
                    "reasoning_content 里），或该网关模型已下线。请先调高 MAX_TOKENS，"
                    "或用 tests/probe_models.py 换一个模型。若刚才有工具下单，"
                    "也可能是接口拒绝了回填——可用 /panel tools 关掉工具再试。"
                    "带图发的话，还可能是这个模型看不了图：设 VISION_ENABLED=false 让它直说看不到。"
                ),
            )

    # ------------------------------------------------------------ 视觉
    def _escalate_budget(self, params: dict[str, Any], already: bool) -> bool:
        """推理型模型常把 max_tokens 想满、正文一个字不剩。

        空手回来时把预算抬一档再给一次机会：一次半长的等待，总比把「没接住」递到对面屏幕上强。
        只抬一次，且有上限——不然一个坏回合能在网关上烧掉二十分钟。
        """
        if already:
            return False
        cap = int(self._settings.empty_retry_max_tokens)
        current = int(params["max_tokens"])
        if cap <= 0 or current >= cap:
            return False
        # 抬太高反而更慢：预算就是这档模型的思考上限，3000 那档实测要等 300 秒才吐正文。
        # 2000 是「能出字」与「别让人等五分钟」的折中
        params["max_tokens"] = min(cap, max(int(current * 1.8), current + 400))
        return True

    async def _ingest(self, user_id: str, images: Sequence[Any]) -> tuple[list[ImageRef], str]:
        """把来路收成能递给模型的图；收不下的那些变成一句明白话，不静默吞掉。"""
        refs: list[ImageRef] = []
        problems: list[str] = []
        for item in images:
            if isinstance(item, ImageRef):
                refs.append(item)
                continue
            try:
                refs.append(
                    await asyncio.to_thread(
                        ingest, str(item), self._settings, self._storage, user_id
                    )
                )
            except VisionError as exc:
                problems.append(f"（他递来的东西收不下：{exc}）")
            except Exception as exc:  # noqa: BLE001 - 读盘故障不阻断对话
                logger.warning("%s 图像摄取失败: %s", user_id, exc)
                problems.append(f"（他递来的东西读不出来：{type(exc).__name__}）")
        limit = self._settings.vision_max_images
        if len(refs) > limit:
            problems.append(f"（一次最多看 {limit} 张，多出来的 {len(refs) - limit} 张我没接）")
        return refs[:limit], "\n".join(problems)

    def _can_see(self, session: Session) -> bool:
        """这个会话此刻看不看得见：配置开着，且接口没退回过。

        与「这一轮有没有图」分开判——工具拍来、画来的图同样要进眼睛。
        """
        return self._settings.vision_enabled and session.vision_mode != "off"

    @staticmethod
    def _should_degrade_vision(
        exc: BotError, vision_on: bool, groups: list[list[Message]], visible: list[str]
    ) -> bool:
        """只在第一趟、还没吐出任何正文时退回：中途改视神经会让已经说出口的话和证据脱节。"""
        if not vision_on or groups or "".join(visible).strip():
            return False
        cause = exc.cause
        return isinstance(cause, APIStatusError) and cause.status_code in {400, 404, 415, 422, 501}

    @staticmethod
    def _should_degrade_native(
        exc: BotError, mode: str, groups: list[list[Message]], visible: list[str]
    ) -> bool:
        """第一趟就报「不懂 tools」才降级；已有正文或已在回填结果时不再折腾。"""
        if mode != "native" or groups or "".join(visible).strip():
            return False
        cause = exc.cause
        return isinstance(cause, APIStatusError) and cause.status_code in {400, 404, 422, 501}

    def tool_mode(self, user_id: str) -> str:
        """当前会话实际采用的工具形态（none|native|inline），供控制台展示。"""
        session = self._sessions.get(user_id)
        if session is None:
            return "none"
        return self._tool_mode(session, self.registry(user_id))

    def _tool_mode(self, session: Session, registry: ToolRegistry) -> str:
        if not registry or not self._settings.tools_enabled:
            return "none"
        if session.tool_mode in {"native", "inline"}:
            return session.tool_mode
        return "native" if self._settings.tool_native_calling else "inline"

    @staticmethod
    def _echoed_by_user(directive: Directive, user_text: str) -> bool:
        """这单子来自他贴在对话框里的暗号，不是她想干活。

        两条判据：他原话里出现过的**整行**暗号（模型照搬），以及他原话里用暗号语法
        点过名的**同一个工具**（模型改了空格照样算）。他自己说「帮我同步一下」、
        模型据此下单是正当行为——这里只挡机器的语法被搬运回来的那一种。
        """
        if not user_text:
            return False
        if directive.name.casefold() in {
            item.name.casefold() for item in extract_directives(user_text)
        }:
            return True
        return _flatten_line(directive.line) in _flatten_line(user_text)

    @classmethod
    def _collect_calls(
        cls,
        mode: str,
        sink: dict[str, Any],
        ordered: list[Directive],
        registry: ToolRegistry,
        echo_of: str = "",
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """把这一趟的下单整理成 (call_id, 工具名, 参数)；复读来的单子不执行。"""
        if mode == "native" and sink.get("tool_calls"):
            calls: list[tuple[str, str, dict[str, Any]]] = []
            for call in sink["tool_calls"]:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append((str(call["id"] or f"call-{len(calls) + 1}"), str(call["name"]), args))
            return calls
        out: list[tuple[str, str, dict[str, Any]]] = []
        for directive in ordered:
            if cls._echoed_by_user(directive, echo_of):
                logger.info("挡下来自对方原话的暗号复读：%s", directive.name)
                continue
            tool = registry.resolve(directive.name)
            args = directive.args or (tool.from_bare(directive.raw_args) if tool else {})
            out.append((f"inline-{len(out) + 1}", directive.name, dict(args)))
        return out

    async def _run_tools(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        groups: list[list[Message]],
        registry: ToolRegistry,
        mode: str,
        can_see: bool,
    ) -> list[list[Message]]:
        """静默执行，然后把「下单 + 结果」补成一组消息。

        形态必须跟着降级走：接口刚拒绝了 `tools`，就不能再给它 `tool_calls`/`role:"tool"`
        这种它不认的结构——inline 模式改用普通消息把结果带回去。

        工具产出的图（看来的、拍来的、画来的）挂成一条带图片分段的 user 消息：
        模型下单要图，就得真的拿到图，而不是拿到一句「图已生成」。
        """
        pairs = await registry.call_many((name, call_args) for _, name, call_args in calls)
        taken, _ = trim(
            [ref for (_, _, _), (_, result) in zip(calls, pairs, strict=True)
             for ref in (result.meta.get("images") or [])],
            self._settings.vision_max_images,
        )
        eyes = can_see
        if mode == "native":
            announced = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(call_args, ensure_ascii=False),
                    },
                }
                for (call_id, name, call_args), _ in zip(calls, pairs, strict=True)
            ]
            group: list[Message] = [{"role": "assistant", "content": "", "tool_calls": announced}]
            group += [
                {"role": "tool", "tool_call_id": call["id"], "content": result.content}
                for call, (_, result) in zip(announced, pairs, strict=True)
            ]
            if taken and eyes:
                group.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_note(taken, seen=True)},
                            *content_parts(taken),
                        ],
                    }
                )
        else:
            notes = "\n\n".join(
                f"（内部结果，不是对方说的话）{name}：{result.content}"
                for (_, name, _), (_, result) in zip(calls, pairs, strict=True)
            )
            if taken and eyes:
                group = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"{notes}\n\n{vision_note(taken, seen=True)}"},
                            *content_parts(taken),
                        ],
                    }
                ]
            else:
                group = [{"role": "user", "content": notes}]
        # 整组进出：绝不留下没有下单头的 tool 消息，那会让严格网关直接 400
        return (groups + [group])[-TOOL_TRAIL_ROUNDS:]

    @staticmethod
    def _has_images(messages: list[Message]) -> bool:
        """这一趟请求里真的有图吗——判据是分段，不是「他这轮贴了图」：工具拍来、
        画来的图同样得交给看得见东西的那个模型。只有对话模型时留空 VISION_MODEL 即可，
        接口拒绝图像分段的退回路径由 `_should_degrade_vision` 负责。
        """
        return any(
            isinstance(message.get("content"), list)
            and any(isinstance(part, dict) and part.get("type") == "image_url"
                    for part in message["content"])
            for message in messages
        )

    async def _call_stream(
        self,
        messages: list[Message],
        params: dict[str, Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        sink: dict[str, Any] | None = None,
        hedge: bool = True,
        tier: str = TIER_FULL,
    ) -> AsyncIterator[str]:
        """打一趟流式请求，并在**同一回合内**自己完成选路与回退。

        顺序是「先在这条线里重试，重试用尽再换下一条线」：同一家内部偶尔排队是运气，
        连着几趟白等才是线路坏了。换家时不重发消息、不改历史，对面看到的还是同一句话。
        """
        has_images = self._has_images(messages)
        # 两道看门狗，都只管「拿到正文之前」这段时间：
        #   first_token_timeout  —— 连分片都不来 = 排在队尾，撤了重发
        #   first_visible_timeout —— 分片一直在来，但全是隐式思考、正文一个字没有；
        #                            这趟再等下去也只是把超时等满，换一条线才有下一把
        # 实测同一份请求：首分片 1.9s 与 254s 都出现过，思考能连吐 5000 字不落正文。
        patience = float(self._settings.first_token_timeout)
        visible_limit = float(self._settings.first_visible_timeout)
        # 对冲只给快捷档：那一趟正文十几个字，多烧一把不心疼；一万字的重回合再补一把，
        # 等于把同一份大请求付两遍，抢回来的那点时间不值这个价
        hedge_after = float(self._settings.first_visible_hedge) if hedge else 0.0
        chances = max(1, int(self._settings.first_token_retries) + 1)
        tried: tuple[str, ...] = ()
        last_error: BotError | None = None

        while True:
            route = self.routes.pick(tier, tried=tried)
            if route is None:
                break
            tried = (*tried, route.name)
            # 对冲补的那一把优先打**下一条线**：同一家里再排一次队多半还是那个水位，
            # 换一家才是真的多抽一次。只有一条线时才在同一家内补。
            rival = self.routes.pick(tier, tried=(route.name,)) if hedge_after > 0 else None
            emitted = False
            problem: BotError | None = None
            async for kind, payload in self._stream_on_route(
                route, messages, params, tools=tools, sink=sink, has_images=has_images,
                patience=patience, visible_limit=visible_limit, hedge_after=hedge_after,
                chances=chances, rival=rival,
            ):
                if kind == "delta":
                    emitted = True
                    yield payload
                else:
                    problem = payload
            if problem is None:
                return                        # 这条线说完了
            if emitted:
                # 已经开口了就不许换家重说：那会变成把同一句话讲两遍，比不回更难堪
                raise problem
            last_error = problem
            if len(tried) >= len(self.routes.routes):
                break                         # 每条线都试过了，别再兜圈
        if last_error is not None:
            raise last_error
        raise BotError(
            f"{tier} 档没有可用上游线路",
            hint="检查 ROUTES 里各条线的 tiers 是否覆盖这一档。",
        )

    async def _stream_on_route(
        self, route: Route, messages: list[Message], params: dict[str, Any], *,
        tools: list[dict[str, Any]] | None, sink: dict[str, Any] | None, has_images: bool,
        patience: float, visible_limit: float, hedge_after: float, chances: int,
        rival: Route | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """在这一条线上把一趟流跑完：吐 ("delta", 正文)，坏了吐 ("problem", BotError)。

        返回问题而不是抛出，是为了让调用方换下一条线接着答**同一句话**——
        上游挂了不该变成「请再说一遍」。`rival` 是对冲要打的另一条线。
        """
        def request_for(target: Route) -> tuple[Any, dict[str, Any]]:  # noqa: ANN401
            kwargs: dict[str, Any] = {
                "model": target.model_for(has_images=has_images),
                "messages": messages,
                "temperature": float(params["temperature"]),
                "top_p": float(params["top_p"]),
                "max_tokens": int(params["max_tokens"]),
                "frequency_penalty": float(params["frequency_penalty"]),
                "stream": True,
                "timeout": target.timeout or self._settings.request_timeout,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            return self._route_client(target), kwargs

        async def open_stream(target: Route) -> Any:  # noqa: ANN401 - SDK 的流对象没有公开类型
            client, kwargs = request_for(target)
            try:
                return await client.chat.completions.create(**kwargs)
            except APIStatusError as exc:
                raise self._translate_status_error(exc) from exc
            except (APIConnectionError, APITimeoutError) as exc:
                raise self._translate_connection_error(exc) from exc
            except OpenAIError as exc:
                raise BotError(f"SDK 错误：{type(exc).__name__}", cause=exc) from exc

        async def prime(stream: Any) -> tuple[Any, Any, list[Any], str]:  # noqa: ANN401
            """读到看见正文为止，带回 (流, 迭代器, 攒下的分片, 卡住的原因)。

            等正文这段时间里读到的分片一律先攒着：里面可能正是 tool_calls，
            当场丢掉就等于把下单吞了，模型那边会留下一条没人认领的调用。
            """
            iterator = stream.__aiter__()
            pending: list[Any] = []
            t0 = time.perf_counter()
            last_at = t0
            try:
                while True:
                    budget, why = self._wait_budget(patience, visible_limit,
                                                    time.perf_counter() - last_at,
                                                    time.perf_counter() - t0)
                    if budget == 0.0:
                        return stream, iterator, pending, f"上游 {why}"
                    try:
                        chunk = await (asyncio.wait_for(iterator.__anext__(), budget)
                                       if budget > 0 else iterator.__anext__())
                    except StopAsyncIteration:
                        return stream, iterator, pending, ""   # 本来就没了正文，交下面按原样吐
                    except TimeoutError:
                        return stream, iterator, pending, f"上游 {why}"
                    last_at = time.perf_counter()
                    if chunk is None:
                        return stream, iterator, pending, ""
                    # 只当拿到「能看的字」才算数：有的网关先甩一个空格再闷 80 秒，
                    # 按非空判定会让看门狗提前解除武装，正好放过最该撤的那一趟
                    pending.append(chunk)
                    if self._delta_text(chunk).strip():
                        return stream, iterator, pending, ""
            except APIStatusError as exc:
                raise self._translate_status_error(exc) from exc
            except (APIConnectionError, APITimeoutError) as exc:
                raise self._translate_connection_error(exc) from exc

        async def shoot(target: Route, must: bool,
                        opened: list[Any]) -> tuple[Any, Route, Any, list[Any], str]:
            """开一趟并读到看见正文，带回 (流, 这条线, 迭代器, 攒下的分片, 卡住的原因)。

            开流这一步本身就可能很慢（过载时响应头都要等八九秒），所以它必须算在
            看门狗与对冲的计时里——先 `await` 再计时等于把最该抢的那段时间放过。
            `must=False` 是对冲那把：开不起来就当没补过，不许把好的那把带崩。
            """
            try:
                fresh = await open_stream(target)
            except BotError as exc:
                if must:
                    raise
                logger.info("对冲那一趟没开起来（%s），这一把当作没补", exc.message)
                return None, target, None, [], f"{exc.message}"
            opened.append(fresh)
            stream, iterator, pending, stalled = await prime(fresh)
            return stream, target, iterator, pending, stalled

        for attempt in range(1, chances + 1):
            opened: list[Any] = []
            tasks: list[Any] = []
            winner: asyncio.Task | None = None
            stream: Any = None
            try:
                started = time.perf_counter()
                tasks.append(asyncio.create_task(shoot(route, True, opened)))
                # 到点还没见正文就再补一把同时等：推理长度是抽签（同一份请求实测
                # 269 字与 2900 字都出现过），多一把就多一次抽到短的可能。只补一把，不铺。
                deadline = started + hedge_after if hedge_after > 0 else None
                while True:
                    left = None if deadline is None else max(0.0, deadline - time.perf_counter())
                    # FIRST_COMPLETED 才是抢：`asyncio.wait` 默认等**全部**跑完，
                    # 那样补一把只会让慢的那把把等待一起拖满，等于没对冲
                    done, _ = await asyncio.wait(tasks, timeout=left,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if done:
                        picked = next(iter(done))
                        if picked.cancelled():
                            tasks.remove(picked)
                            continue
                        failure = picked.exception()
                        if failure is not None:
                            # 开流就翻车（密钥不吃这个模型、404、连不上）也算这条线的账：
                            # 换下一条线，而不是让这一句白问
                            problem = failure if isinstance(failure, BotError) else BotError(
                                str(failure) or type(failure).__name__, cause=failure)
                            self.routes.report_bad(route.name, problem.message)
                            logger.warning("%s 没开起来：%s", route.name, problem.message)
                            yield ("problem", problem)
                            return
                        if picked.result()[0] is None and len(tasks) > 1:
                            tasks.remove(picked)      # 对冲那把没开起来：不算赢，等真那把
                            continue
                        winner = picked
                        for task in tasks:
                            if task is not winner:
                                task.cancel()          # 输的立刻撤，别让它继续占着上游那条连接
                        break
                    if deadline is None:
                        continue                       # 不对冲：让这一把的看门狗自己撤
                    deadline = None
                    logger.info("%g 秒还没见正文，补一把同时等", hedge_after)
                    tasks.append(asyncio.create_task(
                        shoot(rival or route, False, opened)))
                stream, answered, iterator, pending, stalled = await winner
                if stalled:
                    self.routes.report_bad(answered.name, stalled, stalled=True)
                    if attempt < chances:
                        logger.info("%s：%s，撤了重发（第 %d/%d 趟）",
                                    answered.name, stalled, attempt, chances)
                        continue
                    logger.warning("%s 这趟白等（%s）", answered.name, stalled)
                    yield ("problem", BotError(
                        stalled,
                        hint=f"{answered.name} 连着 {chances} 趟都不落正文。"))
                    return
                if answered.name != route.name:
                    logger.info("对冲那把（%s）先见正文，用了它", answered.name)
                self.routes.report_ok(answered.name, time.perf_counter() - started)
                async for chunk in self._prefixed(pending, iterator):
                    if sink is not None:
                        self._absorb_tool_calls(chunk, sink)
                    delta = self._delta_text(chunk)
                    if delta:
                        yield ("delta", delta)
                return
            except APIStatusError as exc:
                problem = self._translate_status_error(exc)
                self.routes.report_bad(route.name, problem.message)
                logger.warning("%s 回了错误：%s", route.name, problem.message)
                yield ("problem", problem)
                return
            except (APIConnectionError, APITimeoutError) as exc:
                problem = self._translate_connection_error(exc)
                self.routes.report_bad(route.name, problem.message)
                logger.warning("%s 连不上：%s", route.name, problem.message)
                yield ("problem", problem)
                return
            finally:
                await self._retire(tasks, winner, opened)

    @staticmethod
    async def _retire(tasks: list[Any], winner: asyncio.Task | None,  # noqa: ANN401
                      opened: list[Any]) -> None:
        """一把见正文，其余的当场收摊：撤任务、等它撤干净、每一把的流都关掉。

        赢的那把也在这里关：走到 finally 就是这一趟用完了（读完、卡住、或出异常），
        留着只会让网关那侧挂一条没人读的流。
        """
        stragglers = [task for task in tasks if task is not winner]
        for task in stragglers:
            task.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        for stream in opened:
            await MySoulBot._close_stream(stream)

    @staticmethod
    async def _prefixed(head: list[Any], iterator: Any) -> AsyncIterator[Any]:  # noqa: ANN401
        """先吐攒下的分片，再接着读剩下的：一个字节、一次下单都不丢。"""
        for chunk in head:
            yield chunk
        async for chunk in iterator:
            yield chunk

    @staticmethod
    def _wait_budget(patience: float, visible_limit: float, since_last: float,
                     since_start: float) -> tuple[float, str]:
        """这一口还能等多久，以及是谁在催：预算 0 = 已超时，负数 = 不设限。

        两条看门狗取更紧的那条——「多久没来下一个分片」按上一口算，
        「多久没见正文」从发请求算。两回事不能混成一个计时器，否则思考刷得越勤
        反而越不容易超时，正好放过最该撤的那一趟。
        """
        options: list[tuple[float, str]] = []
        if patience > 0:
            options.append((patience - since_last, f"{patience:g} 秒没吐下一个分片"))
        if visible_limit > 0:
            options.append((visible_limit - since_start, f"{visible_limit:g} 秒只思考、不落正文"))
        if not options:
            return -1.0, ""
        tightest, reason = min(options)
        return (0.0, reason) if tightest <= 0 else (tightest, reason)

    @staticmethod
    def _absorb_tool_calls(chunk: Any, sink: dict[str, Any]) -> None:
        """把流式分片里的 tool_calls 按 index 拼回完整下单。"""
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        delta = getattr(choices[0], "delta", None)
        fragments = getattr(delta, "tool_calls", None) or []
        if not fragments:
            return
        bucket: dict[int, dict[str, Any]] = sink.setdefault("by_index", {})
        for position, fragment in enumerate(fragments):
            index = int(getattr(fragment, "index", position) or 0)
            slot = bucket.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = getattr(fragment, "id", None)
            if call_id:
                slot["id"] = str(call_id)
            function = getattr(fragment, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] = str(function.name)
                if getattr(function, "arguments", None):
                    slot["arguments"] += str(function.arguments)
        sink["tool_calls"] = [bucket[key] for key in sorted(bucket)]

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        closer = getattr(stream, "close", None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 流关闭失败不值得打断退出流程
                logger.debug("流式连接关闭异常", exc_info=True)

    @staticmethod
    def _delta_text(chunk: Any) -> str:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None) if delta else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # 少数兼容接口返回分段
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return ""

    async def _finalize(
        self,
        session: Session,
        user_text: str,
        reply: str,
        interrupted: bool,
        day: dt.date,
        attachments: list[str] | None = None,
        group_mode: bool = False,
    ) -> None:
        """把这一轮写入历史/日志，并提交后台抽取。

        落盘的只有字：图片记一个名字，不把 base64 塞进日志——上下文恢复时
        角色读到「他给看过一张 cat.png」，比重新吞 2MB 像素更像想起过这件事。
        """
        text = reply.strip()
        if not _has_substance(text):
            if interrupted:
                logger.info("%s 回复被中断且无内容，本回合不落盘", session.user_id)
            else:
                logger.warning("模型返回空内容")
            return

        suffix = "（回复被中断）" if interrupted else ""
        session.append("user", user_text, self._settings.context_max_turns)
        session.append("assistant", text + suffix, self._settings.context_max_turns)
        session.turns += 1
        self._fold_history(session)

        user_entry: dict[str, Any] = {"role": "user", "content": user_text}
        if attachments:
            user_entry["attachments"] = attachments
        await self._storage.append_transcript(
            session.user_id,
            [
                user_entry,
                {"role": "assistant", "content": text + suffix, "interrupted": interrupted},
            ],
        )
        await self._settle_emotion(session, user_text, text, interrupted)

        window = session.history[-self._settings.extractor_lookback_turns :]
        self._extractor.submit(session.user_id, window, today=day)
        # 慢环：攒够几轮就在后台复盘一次，把心得落进 MOOD.md，下一轮的提示词自然带上
        self.cognition.note_turn(session.user_id)
        # 判断回路：这一句用户消息就是上一句我们那话的「结果」——
        # 隔多久回的、回了多长、有没有反问，全在这儿量得出来，不交给模型回忆
        if session.last_reply_at:
            self.judgment.note(observe(
                user_id=session.user_id,
                our_text=session.last_reply_text,
                our_bubbles=session.last_bubbles,
                their_text=user_text,
                gap_seconds=max(0.0, time.time() - session.last_reply_at),
                replied=bool((user_text or "").strip()),
                group=group_mode,
            ))
        session.last_reply_at = time.time()
        session.last_reply_text = text
        # 气泡条数由网桥切完才知道，这里先按「一条长话」估：
        # 判断回路只关心「我们说多了没有」，字数那个信号已经够硬
        session.last_bubbles = 1

    async def _settle_emotion(
        self, session: Session, user_text: str, reply: str, interrupted: bool
    ) -> None:
        """把这一轮的情绪沉淀成「余温」，并按半衰期留着，不许下一轮立刻晴转多云。

        同时判定 repair（上一轮还不痛快、这一轮他软下来）——和好是要攒进温度的。
        """
        try:
            presence = session.presence or Presence(stamp=session.stamp, slot=SLOTS[6], patience=Patience())
            valence, cause = assess_mood(user_text, reply)
            before = presence.mood.current(session.stamp, self._settings.mood_half_life_minutes)
            repair = before <= -0.15 and valence >= 0.15
            mood = Mood(valence, cause or presence.mood.cause, session.stamp) if abs(valence) >= 0.05 else Mood()
            patience = presence.patience.spend(self._settings.patience_turn_limit)
            session.state.update(
                {
                    "mood": mood.to_dict(),
                    "patience": patience.to_dict(),
                    "last_seen": session.stamp.isoformat(timespec="seconds"),
                    "pending_repair": repair,
                }
            )
            if interrupted:
                session.state.setdefault("mood", mood.to_dict())
            await self._storage.write_state(session.user_id, session.state)
            if repair:
                logger.info("%s 争执后缓和：温度按和好计", session.user_id)
        except Exception as exc:  # noqa: BLE001 - 情绪沉淀失败不影响回复落盘
            logger.warning("%s 情绪沉淀失败: %s", session.user_id, exc)

    # ------------------------------------------------------------ 维护
    async def flush_extractions(self, timeout: float = 60.0) -> int:
        """等待后台抽取排空，返回剩余积压数（CLI 退出或 /sync 时使用）。"""
        return await self._extractor.wait_idle(timeout)

    def reset_history(self, user_id: str) -> None:
        session = self._sessions.get(user_id)
        if session is not None:
            session.history.clear()
            session.turns = 0
            logger.info("%s 上下文已清空（人格与记忆未受影响）", user_id)

    # ------------------------------------------------------------ 错误翻译
    @staticmethod
    def _translate_status_error(exc: APIStatusError) -> BotError:
        code = exc.status_code
        detail = str(exc)[:300]
        table: dict[int, tuple[str, str]] = {
            400: ("请求被接口拒绝", f"检查 MODEL / 参数是否被该接口支持：{detail}"),
            401: ("API_KEY 无效或未授权", "核对 .env 中的 API_KEY，注意有的服务用 sk- 前缀之外的格式"),
            402: ("账户余额不足", "到模型服务商处充值后重试"),
            403: ("该 Key 无权访问此模型", "确认服务商已开通目标模型"),
            404: ("接口路径或模型不存在", f"BASE_URL 应含 /v1 之类的版本路径，MODEL 需与服务商一致：{detail}"),
            410: ("该模型已下线", "中转站常按 end-of-life 直接返回 410，用 tests/probe_models.py 重新选模型"),
            429: ("触发限流", "稍后重试，或调低并发 / 更换 Key"),
            500: ("服务端内部错误", "稍后重试"),
            502: ("网关错误", "服务不可用，稍后重试"),
            503: ("服务过载或不可用", "稍后重试"),
        }
        message, hint = table.get(code, (f"接口返回 HTTP {code}", "查看日志中的原始错误"))
        return BotError(message, hint=hint, cause=exc)

    def _translate_connection_error(self, exc: BaseException) -> BotError:
        if isinstance(exc, APITimeoutError):
            return BotError(
                "请求超时",
                hint=f"可提高 REQUEST_TIMEOUT（当前 {int(self._settings.request_timeout)}s）或改用更快的服务",
                cause=exc,
            )
        return BotError(
            "无法连接到接口",
            hint=f"确认 {self._settings.base_url} 可访问、服务已启动、代理未拦截",
            cause=exc,
        )
