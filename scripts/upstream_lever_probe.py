"""上游还能压不压得动：一次量四种写法，别的都不改。

    .venv/bin/python scripts/upstream_lever_probe.py [每种几趟]

看点只有「首个可见字」：那是屏幕上第一个气泡的全部来源。
  base        —— 现在就用的写法（system + user）
  prefill     —— 末尾挂一条半句的 assistant，逼模型接着写而不是从头盘算
  prefill_空  —— 末尾挂空 assistant（有些服务端认这个为「直接开始」）
  预算 400    —— 把 completion 掐短，看思考会不会跟着缩水
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from tier_speed_matrix import prompts  # noqa: E402

VARIANTS = ("base", "prefill", "prefill_empty", "budget400")


async def measure(client, model: str, system: str, question: str, *,
                  variant: str, max_tokens: int) -> dict[str, float]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    if variant == "prefill":
        messages.append({"role": "assistant", "content": "嗯"})
    elif variant == "prefill_empty":
        messages.append({"role": "assistant", "content": ""})
    budget = 400 if variant == "budget400" else max_tokens
    t0 = time.perf_counter()
    first_delta = first_view = None
    think = view = 0
    try:
        stream = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=budget,
            temperature=0.9, stream=True,
        )
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            think += len(getattr(delta, "reasoning_content", None) or "")
            body = getattr(delta, "content", None) or ""
            if body:
                first_delta = first_delta if first_delta is not None else now
                first_view = first_view if first_view is not None else now
                view += len(body)
    except Exception as exc:  # noqa: BLE001 - 挂掉的写法也要留在表上
        return {"variant": variant, "error": f"{type(exc).__name__}: {str(exc)[:70]}"}
    return {"variant": variant, "first_view": round(first_view or -1, 1),
            "total": round(time.perf_counter() - t0, 1), "think": think, "visible": view}


async def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    picked = (sys.argv[2].split(",") if len(sys.argv) > 2 else list(VARIANTS))
    variants = tuple(name for name in VARIANTS if name in picked) or VARIANTS
    settings = Settings()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=200)
    table = await prompts(settings)
    system = table["快捷档"]
    question = "在吗"
    print(f"快捷档 {len(system)} 字 · 模型 {settings.model} · max_tokens={settings.max_tokens} · "
          f"每种 {rounds} 趟 · 写法 {','.join(variants)}", flush=True)
    rows: dict[str, list[float]] = {name: [] for name in variants}
    for index in range(rounds):
        for variant in variants:
            row = await measure(client, settings.model, system, question,
                                variant=variant, max_tokens=settings.max_tokens)
            if row.get("error"):
                print(f"  [{index + 1}] {variant:<14} 失败 {row['error']}", flush=True)
                continue
            rows[variant].append(row["first_view"])
            print(f"  [{index + 1}] {variant:<14} 首字 {row['first_view']:>6.1f}s · "
                  f"整体 {row['total']:>6.1f}s · 思考 {row['think']:>5} 字 · 正文 {row['visible']} 字",
                  flush=True)
    print("\n=== 汇总（首字，越小越快）")
    for variant in variants:
        good = [x for x in rows[variant] if x > 0]
        if not good:
            print(f"  {variant:<14} 全空手 ×{len(rows[variant])}")
            continue
        print(f"  {variant:<14} 中位 {statistics.median(good):>5.1f}s · "
              f"最快 {min(good):>5.1f}s · 最慢 {max(good):>5.1f}s · "
              f"空手 {len(rows[variant]) - len(good)} 趟", flush=True)
    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
