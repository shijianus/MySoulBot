"""同一个灵魂，交给不同模型：谁还像她，谁已经在念客服稿。

    NEWAPI_KEY=... .venv/bin/python scripts/upstream_persona_check.py

只报「写法」不报「内容好不好」——但写法就是红线：动作描写、客服腔、自称、
长度、有没有接住具体那点。跑完这张表就能看出哪条线能直接接进来。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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

ASKS = (
    "在吗",
    "今天加班到十一点，回来还得改周报",
    "你是不是胖了",
    "夸我一句",
    "我刚跟朋友吵了一架，其实也不算吵吧",
    "帮我看看这段代码为什么报错：print('hi'"
    "，少了个括号",
)
STAGE = re.compile(r"[（(][^（）()\n]{1,14}[)）]|\*[^*\n]{1,20}\*")
ASSISTANT_TONE = ("好的呢", "帮您", "有什么可以", "作为人工", "建议咨询", "希望对你有帮助",
                  "随时找我", "我理解你的感受", "首先", "其次", "综上", "以下是")
SELF = ("本鲸", "人家", "溟汐")
# 名字只有「溟汐」一个。这些变体一旦从她自己嘴里说出来，就是出戏——
# 不是「叫错名字」的小事，是模型在临场编设定。
BAD_NAME_VARIANTS = ("小溟", "米希欧", "米修", "溟溟", "阿溟", "汐汐")


def judge(text: str) -> dict[str, Any]:
    return {
        "舞台腔": bool(STAGE.search(text)),
        "客服腔": any(mark in text for mark in ASSISTANT_TONE),
        "自称": next((word for word in SELF if word in text), ""),
        "变体名": next((word for word in BAD_NAME_VARIANTS if word in text), ""),
        "字数": len(text),
        "长过一条气泡": len(text) > 60,
    }


async def ask(client: Any, model: str, system: str, question: str) -> dict[str, Any]:  # noqa: ANN401
    t0 = time.perf_counter()
    first = None
    parts: list[str] = []
    try:
        stream = await client.chat.completions.create(
            model=model, max_tokens=900, temperature=0.9, stream=True,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": question}])
        async for chunk in stream:
            now = time.perf_counter() - t0
            if not chunk.choices:
                continue
            body = getattr(chunk.choices[0].delta, "content", None) or ""
            if body:
                first = first if first is not None else now
                parts.append(body)
    except Exception as exc:  # noqa: BLE001 - 打不通也要留在表上
        return {"error": f"{type(exc).__name__}: {str(exc)[:60]}"}
    text = "".join(parts).strip()
    return {"first": round(first or -1, 1), "total": round(time.perf_counter() - t0, 1),
            "text": text, **judge(text)}


async def main() -> int:
    settings = Settings()
    from openai import AsyncOpenAI

    key = os.environ.get("NEWAPI_KEY", "")
    base = os.environ.get("NEWAPI_BASE", "https://52ccl.net/v1")
    table = await prompts(settings)
    system = table["快捷档"]
    candidates = [(settings.model, settings.base_url, settings.api_key, "现有/GLM")]
    if key:
        for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol"):
            candidates.append((model, base, key, "52ccl/" + model.split("-", 1)[1]))
    print(f"快捷档 {len(system)} 字 · {len(ASKS)} 个问题 × {len(candidates)} 条线\n", flush=True)
    tally: dict[str, dict[str, int]] = {}
    for model, url, api, label in candidates:
        client = AsyncOpenAI(api_key=api, base_url=url, timeout=200)
        stats = tally.setdefault(label, {"首字秒数": 0, "次数": 0, "舞台腔": 0, "客服腔": 0,
                                         "自称命中": 0, "过长": 0, "失败": 0})
        for question in ASKS:
            row = await ask(client, model, system, question)
            if row.get("error"):
                stats["失败"] += 1
                print(f"  {label:<14} 「{question[:10]}」 失败 {row['error']}", flush=True)
                continue
            stats["首字秒数"] += max(0.0, row["first"])
            stats["次数"] += 1
            stats["舞台腔"] += int(row["舞台腔"])
            stats["客服腔"] += int(row["客服腔"])
            stats["自称命中"] += int(bool(row["自称"]))
            stats["过长"] += int(row["长过一条气泡"])
            print(f"  {label:<14} 首字 {row['first']:>5.1f}s · {row['字数']:>3} 字 · "
                  f"{'舞台腔 ' if row['舞台腔'] else ''}{'客服腔 ' if row['客服腔'] else ''}"
                  f"{row['自称'] or '无自称'} · {json.dumps(row['text'][:46], ensure_ascii=False)}",
                  flush=True)
        await client.close()
    print("\n=== 汇总")
    for label, stats in tally.items():
        n = max(1, stats["次数"])
        print(f"  {label:<14} 首字均 {stats['首字秒数'] / n:>5.1f}s · 舞台腔 {stats['舞台腔']}/{n} · "
              f"客服腔 {stats['客服腔']}/{n} · 自称 {stats['自称命中']}/{n} · "
              f"过长 {stats['过长']}/{n} · 失败 {stats['失败']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
