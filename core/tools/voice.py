"""把话按她此刻的身体状态念出来。

这一层的产物是**一段能播的音频**，不是又一个能力开关：台词说完才轮到声音，
所以它不注册成 `Tool`——工具会被 `native_specs()` 播报给酒馆与 CLI，每一轮凭空
多一趟模型往返，还会把音频碎语灌进共享的会话历史里（`tools/base.py` 那条
「工具永远不直接对用户说话」也一起破）。声音属于界面，不属于推理。

**节律绑定**：语速、基频、响度与气口由 `core.presence` 的时段决定，不是二值开关。
深夜（默认 01:00–05:00）自动慢下来、低下去、多喘一口气；零点档既不是白天也不是
深睡，走自己的一档。`RHYTHM_ENABLED=false` 时一律按常态念——身体感关了就不该
在声音里偷偷留着。

**零新依赖是底线**：默认走标准库 `wave` 合成（正弦音节 + 噪声换气，确定性、离线、
几百 KB），装没装东西都能立刻听见；`edge-tts` 只在探测得到时才用，它是可选增强，
不进 `requirements.txt`，也不许在这个手搓服务端里成为硬依赖。两条路都失败时抛
`VoiceError`——一句可以说出去的话，由服务层降级成「没有声音」，绝不冒到界面上
变成红叉或堆栈。

同一段话 + 同一份节律 = 同一串字节：`now` 显式传入，跟生理层一样可测可复现。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib.util
import itertools
import json
import math
import random
import re
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
import urllib.error
import urllib.request

from config import Settings
from core.presence import in_deep_night, resolve_now, slot_for
from core.storage_manager import StorageManager
from core.tools.protocol import strip_markers

_RATE: Final[int] = 16_000
_BASE_PITCH: Final[float] = 206.0
_SYLLABLE_MS: Final[float] = 118.0
_STUB_MAX_SECONDS: Final[float] = 24.0
_FADE_MS: Final[float] = 30.0

_CJK: Final[re.Pattern[str]] = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯]")
_LATIN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")
_CLAUSE_BREAK: Final[re.Pattern[str]] = re.compile(r"[。！？!?；;：:，,\n…—]+")
_ACTION_SPAN: Final[re.Pattern[str]] = re.compile(r"[（(\[【][^）)\]】]{0,240}[）)\]】]")
_STAR_SPAN: Final[re.Pattern[str]] = re.compile(r"\*[^*\n]{0,160}\*")
_STYLE_CHARS: Final[re.Pattern[str]] = re.compile(r"[*_`#>|]+")
_UNTIL_BREAK: Final[re.Pattern[str]] = re.compile(r"[（(\[【].*$", re.S)
_NAME_SAFE: Final[re.Pattern[str]] = re.compile(r"[^\w.\-]+")
_SERIAL: Final = itertools.count()
_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"auto", "edge", "stub", "openai_compat", "none"})

# 时段 → (语速倍率, 基频 Hz, 响度, 音节间隙 ms, 换气 ms)。深夜那一档最慢最低最轻。
_SLOT_PROSODY: Final[dict[str, tuple[float, float, float, int, int]]] = {
    "deep_night": (0.72, 168.0, 0.55, 74, 420),
    "midnight_zero": (0.84, 184.0, 0.72, 60, 330),
    "dawn": (0.9, 192.0, 0.82, 54, 280),
    "morning": (1.0, 206.0, 1.0, 40, 200),
    "noon": (0.97, 202.0, 0.95, 44, 215),
    "afternoon": (0.95, 200.0, 0.93, 46, 230),
    "evening": (1.02, 208.0, 1.0, 40, 200),
    "night": (0.9, 194.0, 0.85, 50, 265),
}
_NEUTRAL: Final[tuple[str, tuple[float, float, float, int, int]]] = ("常态", (1.0, _BASE_PITCH, 1.0, 40, 200))


class VoiceError(RuntimeError):
    """声音没出来。消息本身就是一句可以说出去的话。"""


@dataclass(frozen=True)
class VoiceProsody:
    """此刻怎么说话：慢到什么程度、低到什么程度、多久喘一口气。"""

    rate: float
    pitch_hz: float
    volume: float
    pause_ms: int
    breath_ms: int
    slot: str
    night: bool

    def edge_tuning(self) -> dict[str, str]:
        """edge-tts 吃的是百分比字符串，不是浮点倍率。"""
        return {
            "rate": f"{int(round((self.rate - 1.0) * 100)):+d}%",
            "volume": f"{int(round((self.volume - 1.0) * 100)):+d}%",
            "pitch": f"{int(round(self.pitch_hz - _BASE_PITCH)):+d}Hz",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate": round(self.rate, 3),
            "pitch_hz": round(self.pitch_hz, 1),
            "volume": round(self.volume, 3),
            "slot": self.slot,
            "night": self.night,
        }


@dataclass(frozen=True)
class VoiceClip:
    """一段已经落盘的声音。"""

    path: Path
    route: str
    url: str
    seconds: float
    bytes_len: int
    provider: str
    prosody: VoiceProsody

    def as_dict(self) -> dict[str, Any]:
        return {
            "audio": self.route,
            "seconds": round(self.seconds, 2),
            "provider": self.provider,
            "night": self.prosody.night,
            "slot": self.prosody.slot,
        }


def prosody_for(now: dt.datetime, settings: Settings) -> VoiceProsody:
    """把此刻换算成怎么念。显式吃 `now`，与生理层同一套可测纪律。"""
    if not settings.rhythm_enabled:
        label, values = _NEUTRAL
        rate, pitch = values[0] + settings.tts_rate_bias, values[1] + settings.tts_pitch_bias_hz
        return VoiceProsody(max(0.4, min(2.0, rate)), max(120.0, min(400.0, pitch)),
                            values[2], values[3], values[4], slot=label, night=False)
    moment = resolve_now(now, settings.user_timezone)
    slot = slot_for(moment)
    night = in_deep_night(moment, *settings.night_hours)
    rate, pitch, volume, pause_ms, breath_ms = _SLOT_PROSODY.get(slot.key, _NEUTRAL[1])
    # 时段决定基调，两条 bias 决定「像不像真人闲聊」：整体提一点语速、抬一点音调
    rate = max(0.4, min(2.0, rate + settings.tts_rate_bias))
    pitch = max(120.0, min(400.0, pitch + settings.tts_pitch_bias_hz))
    return VoiceProsody(rate, pitch, volume, pause_ms, breath_ms, slot=slot.label, night=night)


def spoken_text(text: str, settings: Settings) -> str:
    """只留下真要说出口的那部分。

    肢体动作与舞台提示不念——`（把下巴搁在膝盖上）` 念出来就成了报幕；
    `⟦…⟧` 是机器的声音，本来就不该出嘴（复用流层那一道剥离，不分叉实现）。
    """
    cleaned = strip_markers(text or "")
    cleaned = _STAR_SPAN.sub(" ", cleaned)
    cleaned = _ACTION_SPAN.sub(" ", cleaned)
    cleaned = _UNTIL_BREAK.sub(" ", cleaned)  # 没闭合的括号：后面整段都是动作描写
    cleaned = _STYLE_CHARS.sub(" ", cleaned)
    cleaned = _pauses_in(cleaned)
    flat = re.sub(r"[ \t]+", " ", cleaned).strip()
    return flat[: settings.tts_max_chars]


# 一句结尾有没有收口气的标点：没有的话补一个句号，念出来才有个停顿
_OPEN_END: Final[re.Pattern[str]] = re.compile(r"[。！？!?…；;，,：:．.—”』」）)]$")


def _pauses_in(text: str) -> str:
    """把「换行」翻译回「气口」。

    原来这里一把 `re.sub(r"\\s+", " ")` 把所有空白压平：她在屏幕上分成三条气泡、
    用空行隔开板块的那些停顿，到嘴里就成了一串没有断句的长句——
    「像机器翻的、中间不停顿」那一手投诉，根子就在这一行上。
    真人念话是靠断句喘气的：一行没说 complete 的，补个句号让它停一下。
    """
    pieces: list[str] = []
    for chunk in re.split(r"\n+", text or ""):
        line = chunk.strip()
        if not line:
            continue
        if not _OPEN_END.search(line):
            line = line + "。"
        pieces.append(line)
    return "".join(pieces)


def provider_of(settings: Settings) -> str:
    """`auto` 的落点：装了 edge-tts 就真人声，配了本地语音网关就用它，都没有就标准库哼一段。

    `openai_compat` 指的是**任何** OpenAI 兼容的 `/v1/audio/speech`：
    VoiceStudio（本地、免 key、可克隆音色）、CosyVoice 之类的网关、以及自建 sidecar
    都走这一条——接新引擎不该再改这个文件，改的是 .env 里那个 base_url。
    """
    wanted = (settings.tts_provider or "auto").strip().lower()
    if wanted not in _PROVIDERS:
        wanted = "auto"
    if wanted == "auto":
        if _edge_present():
            return "edge"
        return "openai_compat" if (settings.tts_speech_base_url or "").strip() else "stub"
    if wanted == "openai_compat" and not (settings.tts_speech_base_url or "").strip():
        return "stub"          # 没填地址就没有网关可问，别把一段话憋死在这里
    return wanted


def _edge_present() -> bool:
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def audio_route(user_id: str, path: Path) -> str:
    """同源相对路径。界面要的是「从我这个地址取」——手机从局域网 IP 打开面板时，
    回一个写死 127.0.0.1 的绝对地址等于让它去自己手机上找声音。"""
    return f"/media/audio/{quote(user_id)}/{quote(path.name)}"


def audio_url(settings: Settings, user_id: str, path: Path) -> str:
    """绝对地址：说给角色听，她可以原样念给他。"""
    host = settings.server_host or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{settings.server_port}{audio_route(user_id, path)}"


def _stamp_name(user_id: str, suffix: str) -> str:
    """毫秒 + 序号：连发两句落在同一秒内是常态，撞了名就是一条声音盖掉另一条。"""
    now = dt.datetime.now()
    tail = _NAME_SAFE.sub("-", user_id)[:24]
    return (
        f"say-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}"
        f"-{next(_SERIAL) % 1000:03d}-{tail}.{suffix}"
    )


async def synthesize(
    text: str,
    settings: Settings,
    storage: StorageManager,
    user_id: str,
    *,
    now: dt.datetime | None = None,
) -> VoiceClip:
    """念一段并落盘。失败只抛 `VoiceError`，不留半成品文件。"""
    if not settings.tts_enabled:
        raise VoiceError("现在不想出声")
    provider = provider_of(settings)
    if provider == "none":
        raise VoiceError("这台机器没接上声音")
    words = spoken_text(text, settings)
    if not words:
        raise VoiceError("这句里没有要说出口的话")
    prosody = prosody_for(now or resolve_now(None, settings.user_timezone), settings)
    directory = storage.audio_dir(user_id)
    suffix = "wav" if provider == "stub" else "mp3"
    path = directory / _stamp_name(user_id, suffix)
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    try:
        if provider == "edge":
            seconds = await _via_edge(words, settings, path, prosody)
        elif provider == "openai_compat":
            seconds = await _via_gateway(words, settings, path, prosody)
        else:
            seconds = await asyncio.to_thread(_render_wav, words, prosody, path)
    except VoiceError:
        await asyncio.to_thread(_discard, path)  # 失败不留半成品：界面上不该有点得响的空文件
        raise
    except OSError as exc:
        await asyncio.to_thread(_discard, path)
        raise VoiceError(f"声音落不下去：{type(exc).__name__}") from exc
    size = await asyncio.to_thread(_size_of, path)
    if size <= 0:
        await asyncio.to_thread(_discard, path)
        raise VoiceError("念出来是空的")
    return VoiceClip(
        path=path,
        route=audio_route(user_id, path),
        url=audio_url(settings, user_id, path),
        seconds=seconds,
        bytes_len=size,
        provider=provider,
        prosody=prosody,
    )


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _size_of(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


async def _via_edge(words: str, settings: Settings, path: Path, prosody: VoiceProsody) -> float:
    """edge-tts 走微软的在线合成：可选增强，装不上/网不通/超时都收敛成一句人话。"""
    try:
        import edge_tts
    except ImportError as exc:
        raise VoiceError("这台机器上没装 edge-tts") from exc
    voice = settings.tts_voice_night if prosody.night else settings.tts_voice_day
    try:
        communicate = edge_tts.Communicate(words, voice or "zh-CN-XiaoxiaoNeural", **prosody.edge_tuning())
        await asyncio.wait_for(communicate.save(str(path)), timeout=settings.tts_timeout)
    except TimeoutError as exc:
        raise VoiceError("那边迟迟没把声音还回来") from exc
    except Exception as exc:  # noqa: BLE001 - 在线合成失败不该惊动界面
        raise VoiceError(f"声音没念成（{type(exc).__name__}）") from exc
    # mp3 的时长不在标准库里猜：猜错比不说更容易骗到界面上的进度条
    return 0.0


def _speech_request(url: str, payload: dict[str, Any], key: str, timeout: float) -> bytes:
    """问一次语音服务，拿回音频字节。同步函数，交给 `to_thread` 跑。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("content-type") or "")
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", "ignore")[:160]
        raise VoiceError(f"语音服务回了 {exc.code}{('：' + detail) if detail else ''}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VoiceError(f"语音服务没接电话（{type(exc).__name__}）") from exc
    if "audio" not in content_type.lower() and "octet" not in content_type.lower():
        # 网关报错时常常回一段 JSON 200：那玩意儿存成 .mp3 就是一段能点的噪音
        raise VoiceError(f"语音服务回的不是音频（{content_type[:40] or '没写类型'}）")
    return data


async def _via_gateway(words: str, settings: Settings, path: Path,
                       prosody: VoiceProsody) -> float:
    """走 OpenAI 兼容的 /v1/audio/speech：VoiceStudio、CosyVoice 网关、自建 sidecar 都这条路。

    为什么只做 HTTP 而不引 SDK：这个专案刻意只有五个依赖（`requirements.txt`），
    接谁家引擎该改的是 .env 里那个地址，不是往环境里塞一个几百 MB 的 torch。
    """
    base = (settings.tts_speech_base_url or "").strip().rstrip("/")
    if not base:
        raise VoiceError("没填语音服务的地址（TTS_SPEECH_BASE_URL）")
    url = base if base.endswith("/audio/speech") else f"{base}/audio/speech"
    voice = settings.tts_voice_night if prosody.night else settings.tts_voice_day
    payload: dict[str, Any] = {
        "input": words,
        "response_format": "mp3",
        # 语速按时段走（深夜那一档本来就慢）：这是「像她今天这个状态」的一半
        "speed": round(max(0.5, min(2.0, prosody.rate)), 2),
    }
    if voice:
        payload["voice"] = voice
    if settings.tts_speech_model:
        payload["model"] = settings.tts_speech_model
    # 音调不在 OpenAI 的形状里，但支持的网关各自认这些键；一并带上，不认的就忽略
    if settings.tts_pitch_bias_hz:
        payload["pitch"] = round(prosody.pitch_hz, 1)
    data = await asyncio.to_thread(_speech_request, url, payload,
                                   (settings.tts_speech_api_key or "").strip(),
                                   settings.tts_timeout)
    if not data:
        raise VoiceError("语音服务回了个空")
    await asyncio.to_thread(path.write_bytes, data)
    return 0.0


def _clauses(text: str) -> list[str]:
    return [piece for piece in (part.strip() for part in _CLAUSE_BREAK.split(text)) if piece]


def _syllable_count(clause: str) -> int:
    count = len(_CJK.findall(clause)) + len(_LATIN.findall(clause))
    return max(1, count)


def _tone(freq: float, ms: float, amp: float, *, glide: float = 0.94) -> list[float]:
    """一个音节：音高在音节内轻轻往下掉，包络用正弦起落，不留硬切口。"""
    total = max(1, int(ms * _RATE / 1000))
    out: list[float] = []
    phase = 0.0
    for index in range(total):
        pos = index / total
        envelope = math.sin(math.pi * pos) ** 0.7
        phase += 2 * math.pi * freq * (1.0 + (glide - 1.0) * pos) / _RATE
        out.append(amp * envelope * math.sin(phase))
    return out


def _breath(ms: float, amp: float, rng: random.Random) -> list[float]:
    """换气：白噪声按 sin² 起落——是气，不是音。"""
    total = max(1, int(ms * _RATE / 1000))
    return [amp * rng.uniform(-1.0, 1.0) * math.sin(math.pi * index / total) ** 2 for index in range(total)]


def _silence(ms: float) -> list[float]:
    return [0.0] * max(0, int(ms * _RATE / 1000))


def _render_wav(words: str, prosody: VoiceProsody, path: Path) -> float:
    """标准库合成：确定性、离线、几百 KB，没装任何外部东西也立刻有声音。"""
    rng = random.Random(int(hashlib.sha256(words.encode("utf-8")).hexdigest()[:12], 16))
    syllable_ms = _SYLLABLE_MS / prosody.rate
    peak = 0.42 * prosody.volume
    samples: list[float] = []
    for clause in _clauses(words):
        for _ in range(_syllable_count(clause)):
            # 每个音节基频带一点抖动，不然听着像振荡器而不是人
            freq = prosody.pitch_hz * rng.uniform(0.97, 1.04)
            samples += _tone(freq, syllable_ms, peak)
            samples += _silence(prosody.pause_ms * 0.4)
        samples += _breath(prosody.breath_ms, peak * 0.5, rng)
        samples += _silence(prosody.pause_ms)
    budget = int(_STUB_MAX_SECONDS * _RATE)
    if len(samples) > budget:
        samples = samples[:budget]
        fade = int(_FADE_MS * _RATE / 1000)
        for index in range(fade):
            samples[-1 - index] *= index / fade
    if not samples:
        samples = _silence(120)
    frames = bytearray()
    for value in samples:
        frames += struct.pack("<h", max(-32768, min(32767, int(round(value * 32767)))))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_RATE)
        handle.writeframes(bytes(frames))
    return len(samples) / _RATE


def audio_sniff(data: bytes) -> str:
    """按魔数认音频——不能复用 `vision.sniff`：那函数见 RIFF 就认 WEBP，
    一张伪装成 .wav 的图照样能骗过去。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"FORM" and data[8:12] == b"AIFF":
        return "audio/aiff"
    return ""
