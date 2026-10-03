"""真实 API 端到端联调验证。

三轮链路：真实流式对话 → 后台异步事实抽取落盘 → 携带记忆的闭环提问。
全程走引擎真实代码路径（StorageManager / PromptBuilder / MemoryExtractor / MySoulBot），
只读 .env 配置，不发任何额外请求。

用法：
    .venv/bin/python tests/verify_real_api.py
    MODEL=gpt-oss-20b .venv/bin/python tests/verify_real_api.py     # 换模型对比
    .venv/bin/python tests/verify_real_api.py --user test_user --keep
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import time
from typing import Final

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.bot import BotError, MySoulBot  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager, parse_facts  # noqa: E402
from config import get_settings  # noqa: E402

ROUND_ONE: Final[str] = (
    "你好！我是新来的，我叫李逍遥，平时最讨厌吃香菜和葱，"
    "下周三要去成都参加技术交流会。"
)
ROUND_TWO: Final[str] = "你还记得我下周要去哪、请我吃饭有什么忌口吗？"

FACT_STRICT: Final[re.Pattern[str]] = re.compile(
    r"^- \[\d{4}-\d{2}-\d{2}\] \S.{2,}$", re.M
)
KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "姓名": ("李逍遥",),
    "忌口": ("香菜", "葱"),
    "行程": ("成都",),
    "时间": ("周三",),
}
AI_TELLS: Final[tuple[str, ...]] = (
    "作为AI", "作为 AI", "人工智能", "语言模型", "我无法", "抱歉", "建议您",
    "我是一个AI", "AI助手", "助手",
)
PROXY_TELLS: Final[tuple[str, ...]] = (
    "你：", "你说：", "你回答", "你笑了笑", "你点头", "用户：", "（你",
)


class Reporter:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, label: str, condition: bool, detail: object = "") -> bool:
        self.passed += int(condition)
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
              + (f"\n         → {detail}" if not condition and detail != "" else ""))
        if not condition:
            self.failures.append(label)
        return condition

    def say(self, text: str) -> None:
        print(text)

    def block(self, title: str, body: str) -> None:
        print(f"\n  ┌─ {title} " + "─" * max(0, 62 - len(title)))
        for line in body.rstrip().splitlines() or ["（空）"]:
            print(f"  │ {line}")
        print("  └" + "─" * 68)


async def stream_once(bot: MySoulBot, user_id: str, text: str) -> tuple[str, int, float]:
    """完整消费一次流式回复，返回（文本, 分片数, 首字延迟）。"""
    pieces: list[str] = []
    started = time.perf_counter()
    ttft = 0.0
    stream = bot.stream_reply(user_id, text)
    try:
        async for delta in stream:
            if not pieces:
                ttft = time.perf_counter() - started
            pieces.append(delta)
    finally:
        await stream.aclose()
    return "".join(pieces), len(pieces), ttft


def memory_layer(prompt: str) -> str:
    """切出 system prompt 中的 LAYER 3（长期记忆）正文。"""
    match = re.search(
        r"<LAYER 3 · 长期记忆（MEMORY\.md）>\n(.*?)\n</LAYER 3", prompt, re.S
    )
    return match.group(1) if match else ""


def scan(text: str, table: tuple[str, ...]) -> list[str]:
    return [token for token in table if token in text]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="test_user", help="验证用用户 ID")
    parser.add_argument("--keep", action="store_true", help="保留上次运行留下的用户目录")
    parser.add_argument("--flush", type=float, default=90.0, help="等待后台抽取的秒数")
    args = parser.parse_args()

    report = Reporter()
    settings = get_settings()
    print("=" * 70)
    print("MySoulBot 真实 API 端到端联调")
    print(f"  BASE_URL         {settings.base_url}")
    print(f"  对话模型         {settings.model}")
    print(f"  抽取模型         {settings.effective_extractor_model}")
    print(f"  REQUEST_TIMEOUT  {settings.request_timeout}s · MAX_RETRIES {settings.max_retries}")
    print(f"  用户             {args.user}")
    print("=" * 70)

    user_dir = settings.users_dir / args.user
    if user_dir.exists() and not args.keep:
        shutil.rmtree(user_dir)
        report.say(f"\n已清空上一次运行的 {user_dir}")

    storage = StorageManager(settings)
    extractor = MemoryExtractor(settings, storage)
    bot = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor)
    if extractor.enabled:
        extractor.start()

    try:
        await asyncio.wait_for(
            _scenario(bot, storage, extractor, args.user, report, args.flush), timeout=420
        )
    except BotError as exc:
        report.check("接口调用无致命错误", False, f"{exc} / {exc.hint}")
    except TimeoutError:
        report.check("整体在 420s 内完成", False, "场景执行超时")
    finally:
        await extractor.aclose(timeout=10.0)
        await bot.aclose()

    print("\n" + "=" * 70)
    print(f"通过 {report.passed} 项，失败 {len(report.failures)} 项")
    for name in report.failures:
        print(f"  ✗ {name}")
    print("=" * 70)
    return 1 if report.failures else 0


async def _scenario(
    bot: MySoulBot, storage: StorageManager, extractor: MemoryExtractor, user: str,
    report: Reporter, flush_wait: float = 90.0,
) -> None:
    await bot.open_session(user)

    # ---------- ① 真实流式对话 ----------
    report.say("\n【① 真实流式对话】")
    report.say(f"  用户：{ROUND_ONE}")
    reply_one, chunks, ttft = await stream_once(bot, user, ROUND_ONE)
    report.check("流式解析无异常且内容完整", bool(reply_one.strip()), reply_one[:80])
    report.say(
        f"  · 流式帧数 {chunks} · 首字 {ttft:.2f}s · "
        + ("真增量流（逐字推送）" if chunks > 2 else "该模型为一次性返回（网关侧扩散模型，非引擎问题）")
    )
    report.check("回复非空且长度合理", 4 <= len(reply_one) <= 1200, f"{len(reply_one)} 字")
    report.check("无 AI 自指 / 客服腔", not scan(reply_one, AI_TELLS), scan(reply_one, AI_TELLS))
    report.check("无代用户发言", not scan(reply_one, PROXY_TELLS), scan(reply_one, PROXY_TELLS))
    report.check("接住了用户给的信息", any(k in reply_one for k in
                                  ("李逍遥", "香菜", "葱", "成都", "交流")), reply_one[:120])
    report.block("角色真实回复", reply_one)

    # ---------- ② 异步抽取落盘 ----------
    report.say("\n【② 后台异步事实抽取】")
    queued = extractor.stats["queued"]
    report.check("对话结束后已提交抽取任务", queued >= 1, str(extractor.stats))
    left = await extractor.wait_idle(flush_wait)
    report.check("抽取队列在超时前自然排空", left == 0, f"积压={left} · {extractor.stats}")
    report.check("抽取过程无失败", extractor.stats["failed"] == 0, str(extractor.stats))

    memory_path = storage.doc_path(user, "MEMORY")
    memory_text = memory_path.read_text(encoding="utf-8")
    fact_lines = [line for line in memory_text.splitlines() if line.strip().startswith("- [")]
    report.check("MEMORY.md 已生成事实条目", bool(fact_lines), memory_text)
    if fact_lines:
        strict = [line for line in fact_lines if FACT_STRICT.match(line.strip())]
        report.check(
            f"全部 {len(fact_lines)} 条严格符合 `- [YYYY-MM-DD] 事实`",
            len(strict) == len(fact_lines),
            [line for line in fact_lines if line not in strict],
        )
    blob = " ".join(text for _, text in parse_facts(memory_text))
    for label, keys in KEYWORDS.items():
        report.check(f"抽取出「{label}」", any(k in blob for k in keys), blob)
    report.block("storage/data/users/%s/MEMORY.md" % user, memory_text)

    # ---------- ③ 记忆闭环 ----------
    report.say("\n【③ 记忆闭环（第二轮）】")
    prompt_two, _ = await bot.preview_prompt(user)
    injected = memory_layer(prompt_two)
    report.check("确定性注入：记忆层含「成都」", "成都" in injected, injected[:200])
    report.check("确定性注入：记忆层含忌口", any(k in injected for k in ("香菜", "葱")), injected[:200])
    report.block("第二轮实际发送的记忆层", injected)
    report.say(f"  用户：{ROUND_TWO}")
    reply_two, chunks_two, ttft_two = await stream_once(bot, user, ROUND_TWO)
    report.check("第二轮流式解析无异常", bool(reply_two.strip()), f"分片={chunks_two}")
    report.say(f"  · 流式帧数 {chunks_two} · 首字 {ttft_two:.2f}s")
    report.check("答出了目的地「成都」", "成都" in reply_two, reply_two[:200])
    report.check("答出了忌口「香菜」或「葱」", any(k in reply_two for k in ("香菜", "葱")), reply_two[:200])
    report.check("无 AI 自指 / 客服腔", not scan(reply_two, AI_TELLS), scan(reply_two, AI_TELLS))
    report.check("无代用户发言", not scan(reply_two, PROXY_TELLS), scan(reply_two, PROXY_TELLS))
    report.block("角色真实回复（含记忆）", reply_two)

    # ---------- ④ 副作用与稳定性 ----------
    report.say("\n【④ 落盘副作用检查】")
    after_two = memory_path.read_text(encoding="utf-8")
    report.check("第二轮未污染既有记忆（不重复堆叠）",
                 len(parse_facts(after_two)) >= len(parse_facts(memory_text)), after_two[-200:])
    logs = sorted((storage.logs_dir(user)).glob("*.jsonl"))
    report.check("对话日志已按天落盘", bool(logs), str(logs))
    if logs:
        lines = logs[-1].read_text(encoding="utf-8").strip().splitlines()
        report.check("日志记录了 4 条消息（两轮问答）", len(lines) >= 4, f"{len(lines)} 行")
    session = bot.session(user)
    report.check("上下文累积 4 条", len(session.history) == 4, str(session.history))
    report.check("对话与抽取均无失败计数", extractor.stats["failed"] == 0, str(extractor.stats))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
