"""一把还是两把：同一个网关上，并发两把请求到底抢不抢得到时间。

对冲不是白拿的——多一把就多占一份额度，也可能把本来就排队的水位推得更高。
两个方案交替跑，同一时刻同条件，跑完直接比「首个可见字」的分布。

    .venv/bin/python scripts/hedge_ab.py ["问句"] [轮数]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from tier_speed_matrix import measure, prompts  # noqa: E402


async def race(settings: Settings, prompt: str, question: str, draws: int) -> dict[str, float]:
    """同时开 `draws` 把，取最快落正文的那一把的时间。"""
    rows = await asyncio.gather(*(measure(settings, settings.model, prompt, question)
                                  for _ in range(draws)))
    good = [row for row in rows if not row.get("error")]
    if not good:
        return {"visible": -1.0, "think": 0, "failed": len(rows)}
    return {
        "visible": min(float(row["first_visible"]) for row in good),
        "think": max(float(row["think"]) for row in good),
        "failed": len(rows) - len(good),
    }


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    settings = Settings()
    table = await prompts(settings)
    prompt = table["快捷档"]
    print(f"快捷档 {len(prompt)} 字 · 模型 {settings.model} · 每方案 {rounds} 轮"
          f"（两把方案每轮 2 次请求）\n", flush=True)
    solo: list[float] = []
    duo: list[float] = []
    for index in range(1, rounds + 1):
        one = await race(settings, prompt, question, 1)
        solo.append(one["visible"])
        print(f"  [{index}] 一把    首字 {one['visible']:>6.1f}s · 思考 {one['think']:.0f} 字", flush=True)
        two = await race(settings, prompt, question, 2)
        duo.append(two["visible"])
        print(f"  [{index}] 两把    首字 {two['visible']:>6.1f}s · 思考 {two['think']:.0f} 字"
              f"{' · 失败 ' + str(two['failed']) if two['failed'] else ''}", flush=True)

    def line(label: str, samples: list[float]) -> str:
        clean = [x for x in samples if x > 0]
        if not clean:
            return f"  {label:<6} 全败"
        return (f"  {label:<6} 中位 {statistics.median(clean):>6.1f}s · "
                f"最快 {min(clean):>6.1f}s · 最慢 {max(clean):>6.1f}s · 平均 "
                f"{statistics.fmean(clean):>6.1f}s ×{len(clean)}")

    print("\n=== 结论")
    print(line("一把", solo))
    print(line("两把", duo))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
