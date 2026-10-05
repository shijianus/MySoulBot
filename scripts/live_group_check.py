"""群聊现场回合：真上游 + 真网桥，验「点到了要接、没点到不接、谁说的别串」并掐时间。

    .venv/bin/python scripts/live_group_check.py

红线：只报时间、档位与字数，不落一句原文。
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
for path in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from live_bridge_check import build  # noqa: E402

LONG_ASK = "刚开完会，电脑没电，稿子还没存，同事在催，我人都是麻的"


def one_round(port: int, *, text: str, mention: bool, sender: str, uid: int,
              mid: int, wait: float = 150.0) -> dict[str, Any]:
    """自己连一条、发一句群话、收到气泡就收摊。全程阻塞 socket，所以跑在线程里。

    每轮都新开连接：反向 WS 的事件流是按连接的，攒在一条连接上等四句会互相踩时间。
    """
    client = T.QQ(port)
    client.handshake("live-check-token")
    t0 = time.perf_counter()
    client.event(**T.group_event(text, mention=mention, message_id=mid, user_id=uid,
                                 nickname=sender, card=sender))
    got: list[dict[str, Any]] = []
    first: float | None = None
    while time.perf_counter() - t0 < wait:
        frame = client.read_json(30.0)
        if not frame or not frame.get("action"):
            continue
        if frame.get("echo"):
            client.answer(frame, data={"message_id": mid + 1})
        if frame["action"] != "send_group_msg":
            continue
        now = round(time.perf_counter() - t0, 2)
        first = now if first is None else first
        got.append(frame)
        if time.perf_counter() - t0 > first + 6:
            break
    bodies = [str(seg.get("data", {}).get("text", ""))
              for f in got for seg in (f.get("params", {}).get("message") or [])
              if isinstance(seg, dict) and seg.get("type") == "text"]
    client.closes()
    return {"first": first, "bubbles": len(got), "chars": sum(len(b) for b in bodies),
            "texts": bodies}


CASES = (
    ("没点名（不该接）", dict(text="有人看到那只猫了吗", mention=False, sender="老哲",
                              uid=20011, mid=9001, wait=14.0)),
    ("被@短句", dict(text="今晚出来玩不", mention=True, sender="阿哲", uid=20012, mid=9002)),
    ("被@一串短句", dict(text=LONG_ASK, mention=True, sender="老周", uid=20013, mid=9003)),
    ("换人接话", dict(text="他说的不算，我的才算", mention=True, sender="小林",
                      uid=20014, mid=9004)),
)


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="live-group-"))
    rig, _settings = build(root)
    rig.settings.onebot_debounce_seconds = 0.6   # 群里连发还是攒一下，但别拖长现场时间
    port = await _start(rig)
    try:
        for label, kwargs in CASES:
            row = await asyncio.to_thread(one_round, port, **kwargs)
            print(f"  {label:<14} 首气泡 {row['first']}s · {row['bubbles']} 条 · "
                  f"共 {row['chars']} 字", flush=True)
        counts = rig.bridge.status()["counts"]
        print("读数：事件", counts["events"], "回复", counts["replies"],
              "没点名不接", counts["not_woken"], "出错", counts["errors"],
              "空手", counts["starved"], "攒批", counts["batches"], flush=True)
        print("用户腿", rig.bridge.status()["turn_latency_ms"], flush=True)
        print("线路", [(row["name"], row["ok"], row["fail"], row["stalled"],
                        row["first_visible_avg"]) for row in
                       (rig.bot.routes.view()["routes"])], flush=True)
    finally:
        await rig.stop()
        shutil.rmtree(root, ignore_errors=True)
    return 0


async def _start(rig: Any) -> int:
    await rig.clawd.ensure()
    _, port = await rig.bridge.start("127.0.0.1", 0)
    return port


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
