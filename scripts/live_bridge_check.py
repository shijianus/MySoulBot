"""真网关 + 真网桥：用测试套件里那套协议端客户端打一次现场回合，掐三段时间。

    .venv/bin/python scripts/live_bridge_check.py ["要问的话"]

看点：
  「正在输入」什么时候挂上、垫话什么时候落地、第一条真回复什么时候到。
  跑完把临时号删掉——那是探测号，不该留在 storage 里。
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from config import Settings  # noqa: E402
from turn_instrument import attach  # noqa: E402
import qq_onebot_test as T  # noqa: E402

FIRST_SPEECH_IS_REPLY = True  # 等待期不该有任何文字气泡


def build(root: Path) -> tuple[Any, Settings]:
    real = Settings()
    settings = T.make_settings(
        root,
        real.base_url,
        api_key=real.api_key,
        model=real.model,
        # 现场测速得用生产那套看图模型：留着测试档案里的 eyes-pro，图片那一路必然降级，
        # 量出来的「看不到图」就不是产品问题而是探针自己的坑
        vision_model=real.effective_vision_model,
        max_tokens=real.max_tokens,
        empty_retry_max_tokens=real.empty_retry_max_tokens,
        request_timeout=real.request_timeout,
        temperature=real.temperature,
        onebot_access_token="live-check-token",
        onebot_debounce_seconds=real.onebot_debounce_seconds,
        onebot_set_typing=True,
        onebot_typing_interval=real.onebot_typing_interval,
        onebot_bubble_delay_min=0.0,
        onebot_bubble_delay_max=0.0,
        prompt_tiers_enabled=real.prompt_tiers_enabled,
        quick_prompt_max_chars=real.quick_prompt_max_chars,
        mood_max_chars=real.mood_max_chars,
        onebot_auto_record=False,
        extractor_enabled=False,
        tts_provider="stub",
        # 现场测速要测的就是生产那套选路：路由表原样带过来，不然量的是单上游
        routes=real.routes,
        route_fail_threshold=real.route_fail_threshold,
        route_cooldown_seconds=real.route_cooldown_seconds,
        route_strict_order=real.route_strict_order,
        first_visible_hedge=real.first_visible_hedge,
    )
    return _rig(settings), settings


def _rig(settings: Settings) -> Any:
    rig = T.Rig.__new__(T.Rig)
    from core.adapters.qq_onebot import OneBotBridge
    from core.bot import MySoulBot
    from core.card_loader import PersonaLibrary
    from core.clawd_soul import ClawdSoul
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import StorageManager

    rig.settings = settings
    rig.storage = StorageManager(settings)
    rig.clawd = ClawdSoul(settings)
    rig.extractor = MemoryExtractor(settings, rig.storage)
    rig.bot = MySoulBot(settings, rig.storage, PromptBuilder(settings, rig.storage, rig.clawd),
                       rig.extractor, PersonaLibrary(settings), rig.clawd)
    rig.bridge = OneBotBridge(settings, rig.bot)
    rig.port = 0
    return rig


async def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "@溟汐 今天累不累"
    root = Path(tempfile.mkdtemp(prefix="live-check-"))
    rig, settings = build(root)
    port = await _start(rig)
    spy = attach(rig.bot, [time.perf_counter()])  # 每条线路各记一份账
    uid = 900009
    marks: dict[str, float | None] = {"typing": None, "typing_refreshes": None, "bubble": None}
    lines: list[str] = []

    def round_trip() -> list[dict[str, Any]]:
        client = T.QQ(port)
        client.handshake("live-check-token")
        t0 = time.perf_counter()
        client.event(**T.private_event(text, user_id=uid, message_id=int(time.time())))
        got: list[dict[str, Any]] = []
        while time.perf_counter() - t0 < 200:
            frame = client.read_json(30.0)
            if not frame or not frame.get("action"):
                continue
            if frame.get("echo"):
                client.answer(frame, data={"message_id": 123})
            action = frame["action"]
            now = round(time.perf_counter() - t0, 2)
            # 挂输入状态有两个动作名，探到哪个用哪个：只认 set_typing 会把现场看成「没挂」
            if action in ("set_typing", "set_input_state"):
                marks["typing"] = now if marks["typing"] is None else marks["typing"]
                marks["typing_refreshes"] = (marks["typing_refreshes"] or 0) + 1 if marks["typing"] is not None else 1
                continue
            if action != "send_private_msg":
                continue
            seg = frame.get("params", {}).get("message")
            body = " ".join(s.get("data", {}).get("text", "") for s in seg if s.get("type") == "text") \
                if isinstance(seg, list) else str(seg)
            got.append(frame)
            lines.append(f"  {now:>7.2f}s · {body[:64]}")
            if marks["bubble"] is None:
                marks["bubble"] = now
                # 第一条真回复之后再收几秒，看看是不是分条发
                deadline = time.perf_counter() + 6
                while time.perf_counter() < deadline:
                    extra = client.read_json(3.0)
                    if extra and extra.get("echo"):
                        client.answer(extra, data={"message_id": 124})
                break
        client.closes()
        return got

    await asyncio.to_thread(round_trip)
    print("\n".join(lines) or "  （一个气泡都没收到）", flush=True)
    tier = "快捷档" if spy.calls and (spy.calls[0].get("prompt_chars") or 9e9) < 2000 else "全量档"
    print(f"第一次「正在输入」{marks['typing']}s · 续 {marks['typing_refreshes']} 次 · "
          f"首条真回复 {marks['bubble']}s · 上游 {len(spy.calls)} 次 · "
          f"请求 {spy.calls[0].get('prompt_chars') if spy.calls else 0} 字 → {tier}")
    print("读数", rig.bridge.status()["counts"])
    state = rig.bridge.status()
    print("网络腿（协议端来回）", state["qq_latency_ms"])
    print("用户腿（到达→气泡落地）", state["turn_latency_ms"])
    await rig.stop()
    shutil.rmtree(root, ignore_errors=True)
    return 0


async def _start(rig: Any) -> int:
    await rig.clawd.ensure()
    _, port = await rig.bridge.start("127.0.0.1", 0)
    return port


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
