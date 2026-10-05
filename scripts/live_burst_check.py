"""现场连发测试：三句连着进来，必须只答一轮、且答的是三句合起来的意思。

真网关、真网桥、真提示词。跑完自动清掉临时号。

    .venv/bin/python scripts/live_burst_check.py
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
for path in (ROOT, ROOT / "scripts", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import Settings  # noqa: E402
from live_bridge_check import _rig, _start  # noqa: E402
from turn_instrument import attach  # noqa: E402

TOKEN = "burst-check-token"
UID = 900077


async def run() -> int:
    real = Settings()
    root = Path(tempfile.mkdtemp(prefix="burst-check-"))
    settings = T.make_settings(
        root, real.base_url,
        api_key=real.api_key, model=real.model, max_tokens=real.max_tokens,
        empty_retry_max_tokens=real.empty_retry_max_tokens,
        first_token_timeout=real.first_token_timeout, first_token_retries=real.first_token_retries,
        request_timeout=real.request_timeout, temperature=real.temperature,
        onebot_access_token=TOKEN,
        onebot_debounce_seconds=real.onebot_debounce_seconds,
        onebot_bubble_delay_min=0.0, onebot_bubble_delay_max=0.0,
        onebot_set_typing=True, onebot_typing_interval=real.onebot_typing_interval,
        prompt_tiers_enabled=real.prompt_tiers_enabled,
        quick_prompt_max_chars=real.quick_prompt_max_chars,
        mood_max_chars=real.mood_max_chars,
        onebot_auto_record=False, extractor_enabled=False, cognition_enabled=False,
        tts_provider="stub",
    )
    rig = _rig(settings)
    port = await _start(rig)
    clock = [time.perf_counter()]
    spy = attach(rig.bot, clock)  # 每条线路各记一份账
    timeline: list[tuple[float, str, str]] = []

    def burst() -> list[dict[str, Any]]:
        client = T.QQ(port)
        client.handshake(TOKEN)
        t0 = time.perf_counter()
        for index, line in enumerate(("我周三要加班到很晚", "所以饭也别约了", "改成周四行不行")):
            client.event(**T.private_event(line, user_id=UID, message_id=90001 + index))
            time.sleep(0.6)
        got: list[dict[str, Any]] = []
        while time.perf_counter() - t0 < 220:
            frame = client.read_json(30.0)
            if not frame or not frame.get("action"):
                continue
            if frame.get("echo"):
                client.answer(frame, data={"message_id": 4242})
            now = round(time.perf_counter() - t0, 2)
            action = frame["action"]
            if action == "set_typing":
                timeline.append((now, "typing", ""))
                continue
            if action != "send_private_msg":
                continue
            seg = frame.get("params", {}).get("message")
            body = " ".join(s.get("data", {}).get("text", "") for s in seg if s.get("type") == "text") \
                if isinstance(seg, list) else str(seg)
            got.append(frame)
            timeline.append((now, "bubble", body))
            print(f"  {now:>7.2f}s · {body[:70]}", flush=True)
            if len(got) >= 6:
                break
        client.closes()
        return got

    got = await asyncio.to_thread(burst)
    calls = len(spy.calls)
    print(f"\n三句连着进来 → 模型被叫 {calls} 次（1 次才是打包接话，3 次就是各答各的）")
    print("递进去的原文里三句都在:",
          all(word in (spy.packed_user or "") for word in ("周三", "饭也别约", "周四")),
          "|", (spy.packed_user or "")[:140].replace("\n", " "))
    bubbles = [text for _at, kind, text in timeline if kind == "bubble"]
    print("发出的气泡数", len(bubbles))
    print("读数", rig.bridge.status()["counts"])
    await rig.stop()
    shutil.rmtree(root, ignore_errors=True)
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
