"""多上游同台竞技：同一份快捷档提示词，打几家上游、几个模型，只比首字与写法。

    NEWAPI_KEY=... .venv/bin/python scripts/upstream_bench.py ["问句"] [每个几趟]

看点：首分片 / 首个可见字 / 整体 / 思考字数 / 正文，外加一条「会不会用工具」的探法。
不猜——一次跑完表就出来了。
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from tier_speed_matrix import prompts  # noqa: E402

NEW_BASE = os.environ.get("NEWAPI_BASE", "https://52ccl.net/v1")
NEW_MODELS = ("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol")
QUALITY_ASK = "我打算明天请假，把周报推迟到周一交。你觉得哪里不对？就一两句，直接说。"


def targets(settings: Settings) -> list[dict[str, str]]:
    key = os.environ.get("NEWAPI_KEY") or settings.api_key
    rows = [{"label": "现有/GLM", "base_url": settings.base_url,
             "api_key": settings.api_key, "model": settings.model}]
    if os.environ.get("NEWAPI_KEY"):
        for model in NEW_MODELS:
            rows.append({"label": f"52ccl/{model.split('-', 1)[1]}", "base_url": NEW_BASE,
                         "api_key": key, "model": model})
    return rows


async def run(client: Any, row: dict[str, str], system: str, ask: str,  # noqa: ANN401
              *, max_tokens: int, want_tools: bool) -> dict[str, Any]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ask}]
    kwargs: dict[str, Any] = {"model": row["model"], "messages": messages,
                              "max_tokens": max_tokens, "temperature": 0.9, "stream": True}
    if want_tools:
        kwargs["tools"] = [{
            "type": "function",
            "function": {
                "name": "peek_calendar",
                "description": "看一眼对方今天的安排",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }]
        kwargs["tool_choice"] = "auto"
    t0 = time.perf_counter()
    first_delta = first_view = None
    think = view = 0
    tool_calls = 0
    reply: list[str] = []
    try:
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                first_delta = first_delta if first_delta is not None else now
                think += len(reasoning)
            body = getattr(delta, "content", None) or ""
            if body:
                first_delta = first_delta if first_delta is not None else now
                first_view = first_view if first_view is not None else now
                view += len(body)
                reply.append(body)
            if getattr(delta, "tool_calls", None):
                tool_calls += 1
    except Exception as exc:  # noqa: BLE001 - 打不通的也要留在表上
        return {**row, "error": f"{type(exc).__name__}: {str(exc)[:90]}"}
    return {**row, "first_delta": round(first_delta or -1, 2),
            "first_visible": round(first_view or -1, 2),
            "total": round(time.perf_counter() - t0, 2), "think": think, "visible": view,
            "tool_calls": tool_calls, "text": "".join(reply)[:90]}


async def main() -> int:
    ask = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    settings = Settings()
    from openai import AsyncOpenAI

    table = await prompts(settings)
    system = table["快捷档"]
    print(f"快捷档 {len(system)} 字 · 每档 {rounds} 趟 · 问句「{ask}」", flush=True)
    clients: dict[str, Any] = {}
    rows: dict[str, list[float]] = {}
    for row in targets(settings):
        key = f"{row['base_url']}|{row['api_key']}"
        clients.setdefault(key, AsyncOpenAI(api_key=row["api_key"], base_url=row["base_url"],
                                            timeout=200))
    try:
        for index in range(rounds):
            for row in targets(settings):
                client = clients[f"{row['base_url']}|{row['api_key']}"]
                got = await run(client, row, system, ask, max_tokens=settings.max_tokens,
                                want_tools=False)
                if got.get("error"):
                    print(f"  [{index + 1}] {row['label']:<22} 失败 {got['error']}", flush=True)
                    continue
                rows.setdefault(row["label"], []).append(got["first_visible"])
                print(f"  [{index + 1}] {row['label']:<22} 首分片 {got['first_delta']:>6.1f}s · "
                      f"首字 {got['first_visible']:>6.1f}s · 整体 {got['total']:>6.1f}s · "
                      f"思考 {got['think']:>5} 字 · 正文 {got['visible']} 字 · 「{got['text']}」",
                      flush=True)

        print("\n=== 工具调用支不支持（native tool_calls）")
        for row in targets(settings):
            client = clients[f"{row['base_url']}|{row['api_key']}"]
            got = await run(client, row, system, "帮我瞄一眼我今天忙不忙，用工具查。",
                            max_tokens=600, want_tools=True)
            if got.get("error"):
                print(f"  {row['label']:<22} 报错 {got['error']}", flush=True)
            else:
                print(f"  {row['label']:<22} tool_calls 分片 {got['tool_calls']} · "
                      f"首字 {got['first_visible']}s · 「{got['text']}」", flush=True)

        print("\n=== 内容表现（人格与判断力）")
        for row in targets(settings):
            client = clients[f"{row['base_url']}|{row['api_key']}"]
            got = await run(client, row, system, QUALITY_ASK, max_tokens=600, want_tools=False)
            if got.get("error"):
                print(f"  {row['label']:<22} 报错 {got['error']}", flush=True)
            else:
                print(f"  {row['label']:<22} 首字 {got['first_visible']:>6.1f}s · "
                      f"{json.dumps(got['text'], ensure_ascii=False)}", flush=True)

        print("\n=== 首字汇总（越小越快）")
        for label, samples in rows.items():
            good = [x for x in samples if x > 0]
            if not good:
                print(f"  {label:<22} 全部空手")
                continue
            print(f"  {label:<22} 中位 {statistics.median(good):>6.1f}s · "
                  f"最快 {min(good):>6.1f}s · 最慢 {max(good):>6.1f}s · 平均 "
                  f"{statistics.fmean(good):>6.1f}s ×{len(good)}")
    finally:
        for client in clients.values():
            await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
