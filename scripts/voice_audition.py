#!/usr/bin/env python3
"""把同一句话按不同音色与时段念出来，供人耳挑「她那把嗓子」。

跑法：
    .venv/bin/python scripts/voice_audition.py                     # 用 .env 里配好的通路
    .venv/bin/python scripts/voice_audition.py --voices zh-CN-XiaoyiNeural,zh-CN-XiaohanNeural
    .venv/bin/python scripts/voice_audition.py --slots deep_night,afternoon --text "本鲸不去"

为什么需要它：音色这件事不能靠文字描述决定。`TTS_RATE_BIAS` / `TTS_PITCH_BIAS_HZ`
调的是「同一个人的一天」，而「像她又有自己的特色」要的是先听到样本再挑——
本地 VoiceStudio（免 key、零样本克隆）能给一把别人没有的嗓子，前提是你得先听一轮。

产物落在 `storage/run/audition/`（`storage/run/` 不进版本库：那是本机运行痕迹）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import get_settings  # noqa: E402
from core.presence import resolve_now, slot_for  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402
from core.tools import voice  # noqa: E402

DEFAULT_TEXT = "啊？这个点你也没睡……本鲸尾巴都懒得抬，你要是没事就陪我躺一会儿。"
# 默认候选：微软中文女声里风格差得比较开的几个（都是公开音色名，不含任何凭据）
DEFAULT_VOICES = (
    "zh-CN-XiaoyiNeural",
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaomoNeural",
    "zh-CN-xiaorui",
    "zh-CN-YunxiNeural",
)
DEFAULT_SLOTS = ("morning", "afternoon", "night", "deep_night")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="念一遍候选音色，供人耳挑")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--voices", default=",".join(DEFAULT_VOICES))
    ap.add_argument("--slots", default=",".join(DEFAULT_SLOTS),
                    help="时段 key：morning/noon/afternoon/evening/night/dawn/midnight_zero/deep_night")
    ap.add_argument("--provider", default="", help="留空按 .env 走；或 edge / openai_compat / stub")
    ap.add_argument("--out", default="")
    ap.add_argument("--list-slots", action="store_true", help="列一时段就退出")
    return ap.parse_args()


def moment_for(slot: str, zone: ZoneInfo) -> dt.datetime:
    hour = {"dawn": 6, "morning": 9, "noon": 12, "afternoon": 15,
            "evening": 19, "night": 22, "midnight_zero": 0, "deep_night": 3}.get(slot, 14)
    return dt.datetime(2026, 10, 8, hour, 30, tzinfo=zone)


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    if args.list_slots:
        now = resolve_now(None, settings.user_timezone)
        for key in DEFAULT_SLOTS:
            probe = moment_for(key, ZoneInfo(settings.user_timezone or "Asia/Shanghai"))
            print(f"{key:14} {slot_for(probe).label}")
        print(f"（现在这一档：{slot_for(now).label}）")
        return 0

    settings = settings.model_copy(update={
        "tts_enabled": True,
        "tts_provider": (args.provider or settings.tts_provider),
    })
    provider = voice.provider_of(settings)
    out_dir = Path(args.out) if args.out else settings.storage_dir / "run" / "audition"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 试音是「本机跑一下就丢」的事：另开一个 storage 壳子放在 run/ 底下。
    # 直接拿真 storage 跑会在 data/users/ 里长出 audition_* 的资料夹——
    # 那是进版本库的路径，试音产物不该污染资产树。
    store = settings.model_copy(update={"storage_dir": out_dir / "store"})
    storage = StorageManager(store)
    zone = ZoneInfo(settings.user_timezone or "Asia/Shanghai")

    print(f"通路：{provider}｜地址：{settings.tts_speech_base_url or '(edge/stub)'}")
    if provider == "stub":
        print("⚠ 现在这条通路是标准库哼的那一段，只用来验流程——听不出音色好坏。"
              "装 edge-tts，或把 TTS_SPEECH_BASE_URL 指到本地 VoiceStudio / CosyVoice 网关再来。")
    print(f"输出目录：{out_dir}\n")

    names = [item.strip() for item in args.voices.split(",") if item.strip()]
    slots = [item.strip() for item in args.slots.split(",") if item.strip()]
    made, failed = 0, 0
    for name in names:
        for slot in slots:
            tuned = settings.model_copy(update={"tts_voice_day": name, "tts_voice_night": name})
            moment = moment_for(slot, zone)
            user = f"audition_{slot}"
            try:
                clip = await voice.synthesize(args.text, tuned, storage, user, now=moment)
                target = out_dir / f"{slot}__{name}__{clip.path.name}"
                target.write_bytes(clip.path.read_bytes())
                prosody = voice.prosody_for(moment, tuned)
                print(f"  {slot:14} {name:24} rate={prosody.rate:.2f} "
                      f"pitch={prosody.pitch_hz:.0f}Hz → {target.name}")
                made += 1
            except voice.VoiceError as exc:
                print(f"  {slot:14} {name:24} ✗ {exc}")
                failed += 1
    print(f"\n念了 {made} 条，失败 {failed} 条。挑一把顺耳的，"
          f"把名字填进 .env 的 TTS_VOICE_DAY / TTS_VOICE_NIGHT（日夜分成两套更像人）。")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
