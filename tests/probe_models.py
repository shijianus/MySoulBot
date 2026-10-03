"""模型探测：列出中转站支持的模型，并逐个真实调用测延迟与人格表现。

用法：
    .venv/bin/python tests/probe_models.py            # 探测候选聊天模型
    .venv/bin/python tests/probe_models.py --all      # 连同 embed/guard 类一起试
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import AsyncOpenAI  # noqa: E402

PROBE_PROMPT = (
    "用一句话介绍你自己，要求：像一个有性格的人在说话，不要说自己是AI或模型。"
)
SKIP_HINTS = ("embed", "translate", "guard", "parse", "safety", "calibration", "topic")


@dataclass
class ProbeResult:
    model: str
    ok: bool
    latency: float
    text: str
    error: str


def fetch_models(base_url: str, api_key: str) -> list[dict[str, Any]]:
    """注意：该网关会按 User-Agent 拦截默认 python-urllib 流量（返回 403），必须显式设置。"""
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "MySoulBot/1.0 model-probe",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return list(payload.get("data", []))


async def probe_one(
    client: AsyncOpenAI, model: str, semaphore: asyncio.Semaphore
) -> ProbeResult:
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
                max_tokens=80,
                temperature=0.9,
                timeout=45.0,
            )
        except Exception as exc:  # noqa: BLE001 - 探测需要把所有失败收进结果
            return ProbeResult(
                model=model,
                ok=False,
                latency=time.perf_counter() - started,
                text="",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        elapsed = time.perf_counter() - started
        message = response.choices[0].message if response.choices else None
        content = getattr(message, "content", None) or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        reasoning = str(getattr(message, "reasoning_content", "") or "")
        return ProbeResult(
            model=model,
            ok=bool(content.strip()),
            latency=elapsed,
            text=(content.strip() or reasoning.strip())[:220],
            error="" if content.strip() else "返回空 content",
        )


async def probe_stream(client: AsyncOpenAI, model: str) -> tuple[int, float, str]:
    """检查流式收尾：返回分片数、总耗时、末片内容。"""
    started = time.perf_counter()
    pieces: list[str] = []
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "从一数到五，只输出数字。"}],
        max_tokens=60,
        stream=True,
        timeout=45.0,
    )
    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            pieces.append("<无choices>")
            continue
        delta = getattr(choices[0], "delta", None)
        text = getattr(delta, "content", None) if delta else None
        pieces.append(text if isinstance(text, str) else "<非文本>")
    await stream.aclose()
    return len(pieces), time.perf_counter() - started, "".join(
        p for p in pieces if not p.startswith("<")
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="连 embed/guard/translate 一起探测")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    base_url = os.environ.get("BASE_URL", "https://ai.121628.xyz/v1")
    api_key = os.environ["API_KEY"]
    models = fetch_models(base_url, api_key)
    ids = [str(item["id"]) for item in models]
    endpoints = {
        str(item["id"]): item.get("supported_endpoint_types") or [] for item in models
    }
    print(f"接口 {base_url}/models 返回 {len(ids)} 个模型\n")

    if args.all:
        candidates = ids
    else:
        candidates = [m for m in ids if not any(h in m.lower() for h in SKIP_HINTS)]
    skipped = [m for m in ids if m not in candidates]
    if skipped:
        print("按名称跳过的非聊天模型：")
        print("  " + "、".join(skipped) + "\n")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=0)
    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(probe_one(client, m, semaphore) for m in candidates))
    ok = sorted((r for r in results if r.ok), key=lambda r: r.latency)
    bad = [r for r in results if not r.ok]

    print(f"=== 可用 {len(ok)} / 失败 {len(bad)} ===\n")
    for r in ok:
        ep = ",".join(endpoints.get(r.model, []) or ["?"])
        print(f"[{r.latency:5.2f}s] {r.model}  ({ep})")
        print(f"          {r.text}\n")
    if bad:
        print("=== 失败清单 ===")
        for r in sorted(bad, key=lambda item: item.model):
            print(f"{r.model}: {r.error}")

    top = os.environ.get("PROBE_STREAM_MODEL") or (ok[0].model if ok else "")
    if top:
        print(f"\n=== 流式收尾检查 · {top} ===")
        try:
            count, elapsed, text = await probe_stream(client, top)
            print(f"分片 {count} 个 · 耗时 {elapsed:.2f}s · 内容 {text!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"流式失败：{type(exc).__name__}: {str(exc)[:200]}")

    await client.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
