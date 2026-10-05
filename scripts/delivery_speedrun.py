"""交付前现场测速：真上游 + 真网桥 + **生产配置**（防抖窗口开着）。

    .venv/bin/python scripts/delivery_speedrun.py [回合数]

同一个空间连着问几轮，第一轮该挨防抖窗口，之后每一轮都该是「新开话头」直接进队列。
只报时间与长度，不落一句原文。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import Settings  # noqa: E402
from live_bridge_check import build  # noqa: E402
import qq_onebot_test as T  # noqa: E402

ASKS = ("在吗", "今天累不累", "吃饭了没", "晚安", "在忙吗")


async def one_round(port: int, text: str, uid: int, seq: int) -> dict[str, Any]:
    """发一句、掐三段：第一个气泡、最后一个气泡、收到几条。"""
    marks: dict[str, Any] = {"first": None, "last": None, "bubbles": 0, "typed": None}

    def round_trip() -> None:
        client = T.QQ(port)
        client.handshake("live-check-token")
        t0 = time.perf_counter()
        client.event(**T.private_event(text, user_id=uid, message_id=int(time.time()) % 10 ** 6 * 10 + seq))
        while time.perf_counter() - t0 < 150:
            frame = client.read_json(30.0)
            if not frame or not frame.get("action"):
                continue
            if frame.get("echo"):
                client.answer(frame, data={"message_id": 123})
            name = str(frame.get("action"))
            now = round(time.perf_counter() - t0, 2)
            if name in ("set_typing", "set_input_state", "set_input_status") and marks["typed"] is None:
                marks["typed"] = now
            if name == "send_private_msg":
                marks["bubbles"] += 1
                marks["first"] = now if marks["first"] is None else marks["first"]
                marks["last"] = now
                if marks["bubbles"] >= 4:
                    break
        client.closes()

    await asyncio.to_thread(round_trip)
    return {"问": text, "输入态": marks["typed"], "首字": marks["first"],
            "说完": marks["last"], "条数": marks["bubbles"]}


async def main() -> int:
    rounds = max(1, int(sys.argv[1]) if len(sys.argv) > 1 else 3)
    root = Path(tempfile.mkdtemp(prefix="delivery-"))
    rig, settings = build(root)
    port = await _start(rig)
    uid = 900009
    print(f"模型 {settings.model} · 防抖 {settings.onebot_debounce_seconds}s "
          f"· 新开话头绕行 {settings.onebot_debounce_burst_gap}s · 对冲 {settings.first_visible_hedge}s",
          flush=True)
    print(f"\n=== 同一空间连问 {rounds} 轮（生产配置，窗口开着）", flush=True)
    rows: list[dict[str, Any]] = []
    for index in range(rounds):
        row = await one_round(port, ASKS[index % len(ASKS)], uid, index)
        rows.append(row)
        print(f"  [{index + 1}] 「{row['问']}」 输入态 {row['输入态']}s · 首字 {row['首字']}s · "
              f"说完 {row['说完']}s · {row['条数']} 条", flush=True)
    state = rig.bridge.status()
    print("\n=== 两条腿读数")
    print("  网络腿（协议端来回）", state["qq_latency_ms"])
    print("  用户腿（到达→气泡落地）", state["turn_latency_ms"])
    print("  计数", {k: v for k, v in state["counts"].items() if v})
    await rig.stop()
    shutil.rmtree(root, ignore_errors=True)
    return 0


async def _start(rig: Any) -> int:
    await rig.clawd.ensure()
    _, port = await rig.bridge.start("127.0.0.1", 0)
    return port


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
