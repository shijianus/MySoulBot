"""真机延迟台账：走引擎同一条流式路径，测「首字 / 首气泡 / 整体」。

  首字   = 第一个**可见** delta 到达的时刻。推理 delta 不计——QQ 上看不见它就等于没出字。
  首气泡 = 网桥真能发出第一条气泡的时刻（分句 + 舞台提示擦除之后）。
  上游   = 直连网关的裸流：第一个 chunk（含推理）与第一个可见 chunk，用来分清慢在谁身上。
  整体   = 这一回合最后一个 token 落地。

    .venv/bin/python scripts/latency_probe.py                      # 内置对照矩阵
    .venv/bin/python scripts/latency_probe.py glm-5.2-free 2000     # 单档复测
    .venv/bin/python scripts/latency_probe.py --prompt-only        # 只报提示词字数
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from core.adapters.qq_onebot import BubbleStream, strip_stage_directions  # noqa: E402
from core.bot import MySoulBot  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.card_loader import PersonaLibrary  # noqa: E402
from core.clawd_soul import ClawdSoul  # noqa: E402
from core.mood_soul import MoodSoul  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

USER_ID = "qq_group_950689514"
PROBE_TEXT = "@溟汐 今天累不累"
MATRIX = [
    ("glm-5.3-flash-free", 4000),
    ("glm-5.3-flash-free", 2000),
    ("glm-5.2-free", 4000),
    ("glm-5.2-free", 2000),
    ("glm-5.3-free", 4000),
]


async def prompt_size(settings: Settings) -> dict[str, int]:
    """真实群聊提示词的分层字数——瘦身前后各跑一次，差值就是省下的 prefill。"""
    storage = StorageManager(settings)
    builder = PromptBuilder(settings, storage, clawd=ClawdSoul(settings), mood=MoodSoul(settings))
    prompt, layers = await builder.build_system_prompt(
        USER_ID, [], group_mode=True, external_origin=True
    )
    return {
        "clawd": len(layers.clawd),
        "soul": len(layers.soul),
        "user": len(layers.user_profile),
        "total": len(prompt),
    }


async def raw_upstream(settings: Settings) -> dict[str, float]:
    """直连网关裸流：把「推理先占多久」与「正文几点开始出」分开量。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.api_key, base_url=settings.base_url, timeout=settings.request_timeout
    )
    storage = StorageManager(settings)
    builder = PromptBuilder(settings, storage, clawd=ClawdSoul(settings), mood=MoodSoul(settings))
    system, _ = await builder.build_system_prompt(USER_ID, [], group_mode=True, external_origin=True)
    t0 = time.perf_counter()
    first_any = first_view = None
    reasoning = visible = 0
    stream = await client.chat.completions.create(
        model=settings.model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": PROBE_TEXT}],
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
        stream=True,
    )
    try:
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if thought and first_any is None:
                first_any = now
            reasoning += len(thought or "")
            body = getattr(delta, "content", None)
            if body:
                if first_view is None:
                    first_view = now
                visible += len(body)
    finally:
        closer = getattr(stream, "close", None)
        if closer:
            await closer()
    return {
        "upstream_first_delta_s": round(first_any, 2) if first_any is not None else -1.0,
        "upstream_first_visible_s": round(first_view, 2) if first_view is not None else -1.0,
        "upstream_total_s": round(time.perf_counter() - t0, 2),
        "reasoning_chars": reasoning,
        "visible_chars": visible,
    }


async def engine_round(settings: Settings, label: str) -> dict:
    """走 MySoulBot.stream_reply —— 与网桥完全相同的 delta 源。"""
    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    bot = MySoulBot(
        settings,
        storage,
        PromptBuilder(settings, storage, clawd),
        MemoryExtractor(settings, storage),
        PersonaLibrary(settings),
        clawd,
    )
    started = time.perf_counter()
    try:
        await bot.open_session(USER_ID, restore=False)
        t0 = time.perf_counter()
        first_char = first_bubble = None
        deltas: list[str] = []
        stream = BubbleStream(
            bubble_chars=settings.onebot_bubble_chars, max_pieces=settings.onebot_bubble_max
        )
        error = ""
        try:
            async for delta in bot.stream_reply(
                USER_ID, PROBE_TEXT, group_mode=True, external_origin=True
            ):
                now = time.perf_counter() - t0
                deltas.append(delta)
                if first_char is None and delta.strip():
                    first_char = now
                if first_bubble is None:
                    # 与网桥同序：切分吃原始 delta，擦舞台提示发生在成条之后
                    for bubble in stream.feed(delta):
                        if strip_stage_directions(bubble).strip():
                            first_bubble = now
                            break
            total = time.perf_counter() - t0
            tail = [b for b in stream.finish() if b.strip()]
            if tail and first_bubble is None:
                first_bubble = total
        except Exception as exc:  # noqa: BLE001 - 台账要记下失败，不许把整轮炸掉
            total = time.perf_counter() - t0
            error = f"{type(exc).__name__}: {exc}"[:200]
        out = "".join(deltas)
        return {
            "label": label,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "setup_s": round(t0 - started, 2),
            "first_char_s": round(first_char, 2) if first_char else None,
            "first_bubble_s": round(first_bubble, 2) if first_bubble else None,
            "total_s": round(total, 2),
            "out_chars": len(out),
            "sample": out[:60].replace("\n", " "),
            "error": error,
        }
    finally:
        await bot.aclose()


async def main() -> None:
    args = sys.argv[1:]
    base = Settings()
    if args and args[0] == "--prompt-only":
        print(json.dumps(await prompt_size(base), ensure_ascii=False))
        return

    cases = (
        [(args[0], int(args[1]))]
        if len(args) >= 1
        else MATRIX
    )
    sizes = await prompt_size(base)
    print(f"提示词：深层灵魂 {sizes['clawd']} · 人格 {sizes['soul']} · "
          f"用户 {sizes['user']} · 合计 {sizes['total']} 字符\n")

    results = []
    for model, max_tokens in cases:
        s = Settings()
        s = s.model_copy(update={"model": model, "max_tokens": max_tokens})
        print(f"▶ {model} @ max_tokens={max_tokens}", flush=True)
        row = await engine_round(s, f"{model}@{max_tokens}")
        try:
            row.update(await raw_upstream(s))
        except Exception as exc:  # noqa: BLE001
            row["upstream_error"] = f"{type(exc).__name__}: {exc}"[:120]
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    print("\n=== 汇总（首字/首气泡/整体，单位秒）===")
    for r in results:
        if r.get("error"):
            print(f"{r['label']:28s} 失败 {r['error']}")
            continue
        print(
            f"{r['label']:28s} 首字 {r['first_char_s']} · 首气泡 {r['first_bubble_s']} · "
            f"整体 {r['total_s']} · 上游裸流首字 {r.get('upstream_first_visible_s')} · "
            f"推理 {r.get('reasoning_chars')} 字 · 正文 {r['out_chars']} 字"
        )
    totals = [r["total_s"] for r in results if not r.get("error")]
    bubbles = [r["first_bubble_s"] for r in results if r.get("first_bubble_s")]
    if totals:
        print(f"整体中位数 {statistics.median(totals):.1f}s · 首气泡中位数 {statistics.median(bubbles):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
