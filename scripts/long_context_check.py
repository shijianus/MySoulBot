"""长对话现场对照：同一段 30 轮历史，开着会话回看 vs 逐字全塞，量真实的首字与整体。

    .venv/bin/python scripts/long_context_check.py [历史轮数]

这是「长对话越聊越慢、越聊越蠢」的直接验法：比的是**递上去的请求有多大**，
不是提示词模板有多大——历史全塞时那几十条原文也在请求里。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import Settings  # noqa: E402
from turn_instrument import attach, size_of  # noqa: E402

TOPICS = ("加班", "饭没吃", "被我怼了", "想早睡", "水没喝", "点外卖", "改日程", "吵架")


def build(root: Path, recap_on: bool, real: Settings) -> Any:  # noqa: ANN401
    from core.bot import MySoulBot
    from core.card_loader import PersonaLibrary
    from core.clawd_soul import ClawdSoul
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.recap import SessionRecap
    from core.storage_manager import StorageManager

    settings = T.make_settings(
        root, real.base_url,
        api_key=real.api_key, model=real.model, max_tokens=real.max_tokens,
        empty_retry_max_tokens=real.empty_retry_max_tokens,
        first_token_timeout=real.first_token_timeout, first_token_retries=real.first_token_retries,
        request_timeout=real.request_timeout, temperature=real.temperature,
        recap_enabled=recap_on, recap_tail_turns=8, context_max_turns=40,
        extractor_enabled=False, cognition_enabled=False, tts_provider="stub",
    )
    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    recap = SessionRecap(settings, storage)
    prompts = PromptBuilder(settings, storage, clawd, recap=recap)
    bot = MySoulBot(settings, storage, prompts, MemoryExtractor(settings, storage),
                    PersonaLibrary(settings), clawd, recap=recap)
    return bot, prompts, recap, settings


async def run_case(real: Settings, turns: int, recap_on: bool, question: str) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="longctx-"))
    try:
        bot, prompts, recap, settings = build(root, recap_on, real)
        await bot.open_session("long", restore=False)
        session = bot.session("long")
        for index in range(turns):
            await bot._finalize(session, f"第{index}件：{TOPICS[index % 8]}这件事我也记着",
                                "行，那就按这个来。", False, date.today())
        await recap.wait_idle(4.0)
        spy = attach(bot, [time.perf_counter()])  # 每条线路各记一份账
        t0 = time.perf_counter()
        first: float | None = None
        out: list[str] = []
        failed = ""
        try:
            async for delta in bot.stream_reply("long", question):
                if first is None and delta.strip():
                    first = round(time.perf_counter() - t0, 2)
                out.append(delta)
        except Exception as exc:  # noqa: BLE001 - 空手也是结果，得记下来别把整场比对打断
            failed = str(exc)[:40]
        total = round(time.perf_counter() - t0, 2)
        request = spy.calls[0] if spy.calls else {}
        tail = [str(item.get("content") or "")[:14] for item in session.history][-2:]
        await bot.aclose()
        return {
            "case": "回看开" if recap_on else "逐字全塞",
            "turns": turns,
            "verbatim": len(session.history),
            "bullets": len(await recap.read("long")),
            "request_chars": request.get("prompt_chars", size_of(
                [{"role": "system", "content": await prompts.build_system_prompt("long")}])),
            "first_visible_s": first,
            "total_s": total,
            "calls": len(spy.calls),
            "out_chars": sum(len(piece) for piece in out),
            "failed": failed,
            "reply": "".join(out)[:40].replace("\n", " "),
            "tail": tail,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> int:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    question = "那今天这件事到底算谁的错"
    real = Settings()
    print(f"同一段 {turns} 轮历史，问同一句「{question}」\n", flush=True)
    rows = []
    for recap_on in (False, True):
        row = await run_case(real, turns, recap_on, question)
        rows.append(row)
        print(f"{row['case']}：逐字 {row['verbatim']} 条 · 要点 {row['bullets']} 条 · "
              f"请求 {row['request_chars']} 字 → 首字 {row['first_visible_s']}s · "
              f"整体 {row['total_s']}s · 上游 {row['calls']} 次 · 正文 {row['out_chars']} 字"
              + (f" · 【空手】{row['failed']}" if row["failed"] else ""), flush=True)
        print(f"    回话：{row['reply']}", flush=True)
    before, after = rows[0]["request_chars"], rows[1]["request_chars"]
    if before and after:
        print(f"\n请求体积：{before} → {after} 字（降 {(1 - after / before) * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
