"""分档 × 模型 的现场测速矩阵：把「提示词大小」和「模型本身」这两个变量分开量。

同一句短对话，两种档位（快捷档 ≈ 450 字 / 全量档 ≈ 一万字），轮流打三档模型，
记「首个可见字」和「整体」。谁慢一目了然，别拿感觉当结论。

    .venv/bin/python scripts/tier_speed_matrix.py ["问句"] [每格趟数] [档位关键字]

MATRIX_MODEL=glm 可以只留某一档模型（想攒分布时用）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from core.card_loader import PersonaLibrary  # noqa: E402
from core.clawd_soul import ClawdSoul  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.prompt_builder import TIER_FULL, TIER_QUICK  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.recap import SessionRecap  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

USER = "qq_private_1937490685"
_WANTED = os.environ.get("MATRIX_MODEL") or ""
MODELS = tuple(model for model in ("glm-5.3-flash-free", "deepseek-v4.1-flash-free", "gpt-oss-20b")
               if not _WANTED or _WANTED in model)


async def prompts(settings: Settings) -> dict[str, str]:
    """把两档提示词各装一份出来（现场同一份装配代码，不手抄）。"""
    storage = StorageManager(settings)
    recap = SessionRecap(settings, storage)
    builder = PromptBuilder(settings, storage, ClawdSoul(settings), recap=recap)
    out: dict[str, str] = {}
    for label, tier in (("快捷档", TIER_QUICK), ("全量档", TIER_FULL)):
        prompt, _ = await builder.build_system_prompt(USER, [], tier=tier, group_mode=False)
        out[label] = prompt
    await recap.aclose()
    return out


async def measure(settings: Settings, model: str, system: str, question: str) -> dict[str, object]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=200)
    t0 = time.perf_counter()
    first_delta = first_view = None
    think = view = 0
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            stream=True,
        )
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                first_delta = now if first_delta is None else first_delta
                think += len(reasoning)
            body = getattr(delta, "content", None) or ""
            if body:
                first_view = now if first_view is None else first_view
                view += len(body)
    except Exception as exc:  # noqa: BLE001 - 挂掉的档也要留在表上
        await client.close()
        return {"model": model, "error": f"{type(exc).__name__} {str(exc)[:60]}"}
    await client.close()
    return {"model": model, "first_delta": round(first_delta or -1, 1),
            "first_visible": round(first_view or -1, 1), "total": round(time.perf_counter() - t0, 1),
            "think": think, "visible": view}


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    repeats = max(1, int(sys.argv[2])) if len(sys.argv) > 2 else 1
    only = sys.argv[3] if len(sys.argv) > 3 else ""
    settings = Settings()
    budget = os.environ.get("MATRIX_MAX_TOKENS")
    if budget:
        settings.max_tokens = int(budget)
    table = await prompts(settings)
    for label, prompt in table.items():
        print(f"{label}：{len(prompt)} 字", flush=True)
    print(f"问句「{question}」· max_tokens={settings.max_tokens} · 每格 {repeats} 趟\n", flush=True)
    for label, prompt in table.items():
        if only and only not in label:
            continue
        for model in MODELS:
            for index in range(1, repeats + 1):
                row = await measure(settings, model, prompt, question)
                row["tier"] = label
                print(json.dumps(row, ensure_ascii=False), flush=True)
                if row.get("error"):
                    print(f"  {label:<6}{model:<26}[{index}] 失败 {row['error']}", flush=True)
                else:
                    print(f"  {label:<6}{model:<26}[{index}] 首分片 {row['first_delta']:>6}s · "
                          f"首个可见字 {row['first_visible']:>6}s · 整体 {row['total']}s · "
                          f"思考 {row['think']} 字 · 正文 {row['visible']} 字", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
