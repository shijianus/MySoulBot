"""线路测速：同一个问题，同时问所有线路，一条一条量出来。

为什么要有这个：配对口令这类短产出吃的是**延迟**，不是模型多聪明。
哪条形线只会思考不落正文、哪条形线 40 秒不回话、哪条形线 2 秒就落字，
靠 .env 里那行 ROUTES 是看不出来的，得真打一遍。

这里只量三件事，都不烧额度：
  · 正文什么时候露头（first）／整句什么时候说完（total）
  · 只思考不落正文（thinking_only）——这类线路不能拿去做短产出
  · 能不能按指令给一句 ≤10 字的话（follows），以及有没有夹带解释（leaks）

用法：
    .venv/bin/python scripts/model_sonde.py            # 生产线路池全量
    .venv/bin/python scripts/model_sonde.py --only sol # 只量某条（按名字子串）
    .venv/bin/python scripts/model_sonde.py --rounds 3 --deadline 20

`--json-out 路径` 会把这轮读数落一份，好跟上一轮比。密钥不外传：
线路的 key 来自 .env 与 data:<路径>（那些文件本来就被 git 挡住）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from core.model_sonde import Channel, probe_all, render  # noqa: E402
from core.upstream import UpstreamPool  # noqa: E402

# 一句短指令：口令/招呼这类短产出考的就是这个
PROBE_PROMPT: Final[str] = (
    "只回一句十个字以内的口语，像随口说的。别加标点、别解释、别给候选。\n"
    "从「{seed}」这个意象出发想，但别把它原样抄进来。\n")
SEEDS: Final[tuple[str, ...]] = ("半夜的厨房", "末班船", "天台的风", "结冰的湖面", "打烊的书店")


def parse_candidate(spec: str) -> Channel:
    """候选形线写成 `名字=模型@基址@密钥文件`——量完再决定要不要进 .env。

    密钥只从 0600 的文件里读（跟 ROUTES 的 `data:<路径>` 同一套），
    命令行与日志里都不出现它。
    """
    name, _, rest = spec.partition("=")
    model, _, base = rest.partition("@")
    base, _, keyfile = base.partition("@")
    key = Path(keyfile).read_text("utf8").strip() if keyfile else ""
    # 想给候选带关思考开关，就在名字后面写 `+关思考`：`名+关思考=模型@基址@密钥`
    name, _, flag = name.partition("+")
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if flag else None
    return Channel(name.strip(), model.strip(), base.strip(), key, extra)


def channels(settings: Settings, only: str, extra: list[str]) -> list[Channel]:
    pool = [Channel(route.name, route.model, route.base_url or settings.base_url,
                    route.api_key or settings.api_key, route.extra_body)
            for route in UpstreamPool(settings).routes]
    ex_key, ex_base = settings.extractor_credentials()
    if ex_base:
        pool.append(Channel("extractor", settings.extractor_model or settings.model,
                            ex_base, ex_key))
    pool.extend(parse_candidate(item) for item in extra)
    if only:
        # --candidate 是给「还没进 .env 的那条」用的，点名要量的就别再被 --only 滤掉
        wanted = {parse_candidate(item).name for item in extra}
        pool = [c for c in pool
                if c.name in wanted or only.lower() in f"{c.name}/{c.model}".lower()]
    return pool


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="线路测速对比")
    ap.add_argument("--only", default="", help="只量名字里含这个词的线路")
    ap.add_argument("--rounds", type=int, default=2, help="每条线问几轮（默认 2）")
    ap.add_argument("--deadline", type=float, default=12.0, help="单轮最多等几秒（默认 12）")
    ap.add_argument("--candidate", action="append", default=[], metavar="名=模型@基址@密钥文件",
                    help="临时量一条还没进 .env 的形线（可重复）")
    ap.add_argument("--json-out", default="", help="把这轮读数写成 JSON")
    args = ap.parse_args(argv)

    settings = Settings()
    todo = channels(settings, args.only, args.candidate)
    if not todo:
        print("没有匹配的线路（检查 .env 的 ROUTES / --only）")
        return 1
    print(f"同时问 {len(todo)} 条形线，每条 {args.rounds} 轮，单轮上限 {args.deadline} 秒：")
    for ch in todo:
        print(f"  · {ch.name:<10s} {ch.model:<24s} {ch.base}")

    async def run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for round_no in range(args.rounds):
            prompt = PROBE_PROMPT.replace("{seed}", SEEDS[round_no % len(SEEDS)])
            rows.extend(await probe_all(todo, prompt, deadline=args.deadline))
        return rows

    rows = asyncio.run(run())
    print("\n" + render(rows))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", "utf8")
        print(f"\n读数已写到 {args.json_out}")
    return 0 if any(not r.get("error") and r.get("body") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
