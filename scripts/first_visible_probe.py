"""同一份真实群聊提示词，横向比几个候选模型的「首个可见字」时刻。

只量一件事：把 10.4k 字的人格提示词原样递上去，对面多久能看见第一个**正文**字。
推理 delta 不计——QQ 上看不见它就等于没出字。

    .venv/bin/python scripts/first_visible_probe.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from core.clawd_soul import ClawdSoul  # noqa: E402
from core.mood_soul import MoodSoul  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

USER_ID = "qq_group_950689514"
TEXT = "@溟汐 今天累不累"
CANDIDATES = [
    "glm-5.3-flash-free",
    "glm-5.3-free",
    "deepseek-v4-flash-free",
    "deepseek-v4.1-flash-free",
    "step-3.7-flash-free",
    "kimi-k3-free",
    "minimax-m3-free",
    "gemma-4-31b-it-free",
    "gpt-oss-20b",
]


async def real_prompt(settings: Settings) -> str:
    storage = StorageManager(settings)
    builder = PromptBuilder(settings, storage, clawd=ClawdSoul(settings), mood=MoodSoul(settings))
    prompt, _ = await builder.build_system_prompt(USER_ID, [], group_mode=True, external_origin=True)
    return prompt


async def measure(settings: Settings, system: str, model: str, max_tokens: int) -> dict:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=200)
    t0 = time.perf_counter()
    first_delta = first_view = None
    think = view = ""
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": TEXT}],
            max_tokens=max_tokens,
            temperature=settings.temperature,
            stream=True,
        )
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                if first_delta is None:
                    first_delta = now
                think += reasoning
            content = getattr(delta, "content", None)
            if content:
                if first_view is None:
                    first_view = now
                view += content
    except Exception as exc:  # noqa: BLE001 - 台账要记下失败模型
        await client.close()
        return {"model": model, "error": f"{type(exc).__name__}: {exc}"[:120]}
    await client.close()
    return {
        "model": model,
        "max_tokens": max_tokens,
        "first_delta_s": round(first_delta, 1) if first_delta is not None else None,
        "first_visible_s": round(first_view, 1) if first_view is not None else None,
        "total_s": round(time.perf_counter() - t0, 1),
        "think_chars": len(think),
        "visible_chars": len(view),
        "sample": view[:48].replace("\n", " "),
    }


async def main() -> None:
    settings = Settings()
    system = await real_prompt(settings)
    max_tokens = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    print(f"提示词 {len(system)} 字符 · max_tokens={max_tokens} · 问句「{TEXT}」\n", flush=True)
    for model in CANDIDATES:
        row = await measure(settings, system, model, max_tokens)
        if row.get("error"):
            print(f"{model:26s} 失败 {row['error']}", flush=True)
        else:
            print(f"{model:26s} 首个 delta {str(row['first_delta_s']):>7s}s · "
                  f"首个可见字 {str(row['first_visible_s']):>7s}s · 整体 {row['total_s']}s · "
                  f"思考 {row['think_chars']} 字 · 正文 {row['visible_chars']} 字", flush=True)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
