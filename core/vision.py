"""把图片接到灵魂的视神经上。

真人看图不是 OCR：先被吸引或被刺到，才注意到细节。所以这一层的产出是
**图片本体 + 一句「他在给你看东西」**，解释交给模型自己按人格做——
绝不在这儿先跑一遍「这是一张包含 X 和 Y 的图片」，那等于逼角色背清单。

三条来路都收成同一个东西：本地路径、`data:image/...;base64,` 粘贴、http(s) 链接。
`vision_enabled=false` 或接口不认多模态时，走**诚实退化**：图片仍然收下、仍然落盘，
但只告诉角色「他给你看了张图，你这边看不了」——不许编造看见的内容。

字节上限与魔数校验都在这里，因为一张 200MB 的图和一份 .docx 改后缀，
都不该被当成「能看的东西」送进请求体。
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import Settings
from core.storage_manager import StorageManager

_PNG: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_RIFF: Final[bytes] = b"RIFF"
_WEBP: Final[bytes] = b"WEBP"
_GIFS: Final[tuple[bytes, ...]] = (b"GIF87a", b"GIF89a")

_EXT_MEDIA: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MEDIA_EXT: Final[dict[str, str]] = {value: key for key, value in _EXT_MEDIA.items()}

_DATA_URL: Final[re.Pattern[str]] = re.compile(
    r"data:image/(p[nj]?g|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]{16,})", re.I
)
_HTTP_URL: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s<>\"]+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>\"]*)?", re.I
)
# 裸路径只认「带图片后缀且真的存在」的 token，所以中文句子里的 `.png` 不会误伤
_BARE_PATH: Final[re.Pattern[str]] = re.compile(
    r"(?<!\S)(~?[/.][^\s|，。！？；：]*|(?<![\w.])[^\s|，。！？；:]*?)\.(png|jpe?g|gif|webp)(?=\b|$)",
    re.I,
)
_NAME_SAFE: Final[re.Pattern[str]] = re.compile(r"[^\w.\-]+")


class VisionError(RuntimeError):
    """图收不下。消息本身就是一句可以说出去的话。"""


@dataclass(frozen=True)
class ImageRef:
    """一张已经落盘、可以递给模型的图。"""

    path: Path
    media_type: str
    bytes_len: int
    origin: str  # 文件 / 粘贴 / 链接——决定角色怎么说「从哪来的」
    name: str = ""

    def label(self) -> str:
        return self.name or self.path.name


def sniff(data: bytes) -> str:
    """按魔数认格式，不按后缀——后缀是任何人伪造得起的东西。"""
    if data.startswith(_PNG):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in _GIFS:
        return "image/gif"
    if data[:4] == _RIFF and data[8:12] == _WEBP:
        return "image/webp"
    return ""


def grab_bytes(url: str, settings: Settings, allow_private: bool | None = None) -> bytes:
    """下载图片；默认拒走内网与元数据地址，除非配置明说允许。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise VisionError("图片链接只认 http/https")
    from core.tools.webio import _assert_public_host  # 同一套 SSRF 闸门，不分叉实现

    if not (allow_private if allow_private is not None else settings.web_allow_private):
        _assert_public_host(parsed.hostname or "")
    limit = settings.vision_max_bytes
    try:
        with urlopen(  # noqa: S310 - 上面已限定 scheme 与主机
            Request(url, headers={"User-Agent": "Mozilla/5.0 (MySoulBot)"}),
            timeout=settings.web_timeout,
        ) as response:
            data = response.read(limit + 1)
    except Exception as exc:  # noqa: BLE001 - 网络错误统一收成一句人话
        raise VisionError(f"那张图没拿到（{type(exc).__name__}）") from exc
    if len(data) > limit:
        raise VisionError("那张图太大了，我收不下")
    return data


def _stamp_name(user_id: str, prefix: str, media_type: str) -> str:
    now = dt.datetime.now()
    tail = _NAME_SAFE.sub("-", user_id)[:24]
    return f"{prefix}-{now.strftime('%Y%m%d-%H%M%S')}-{tail}{_MEDIA_EXT[media_type]}"


def _write_artifact(storage: StorageManager, user_id: str, name: str, data: bytes) -> Path:
    directory = storage.artifacts_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def ingest(
    source: str,
    settings: Settings,
    storage: StorageManager,
    user_id: str,
    *,
    allow_private: bool | None = None,
) -> ImageRef:
    """把一路来源收成一张落盘的图。失败只抛 VisionError，不抛半成品。"""
    token = (source or "").strip()
    if not token:
        raise VisionError("没说要我看什么")

    data_match = _DATA_URL.search(token)
    if data_match:
        raw = re.sub(r"\s+", "", data_match.group(2))
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - base64 破损是常态输入
            raise VisionError("粘来的 base64 读不出图像") from exc
        return _adopt(data, "粘贴", settings, storage, user_id)

    if token.lower().startswith(("http://", "https://")):
        return _adopt(
            grab_bytes(token, settings, allow_private=allow_private),
            "链接",
            settings,
            storage,
            user_id,
        )

    path = _resolve_local(token)
    size = path.stat().st_size
    if size > settings.vision_max_bytes:
        raise VisionError(f"这张图太大了（{size // 1024} KB，收不下）")
    return _adopt(path.read_bytes(), "文件", settings, storage, user_id, label=path.name)


def _adopt(
    data: bytes,
    origin: str,
    settings: Settings,
    storage: StorageManager,
    user_id: str,
    *,
    label: str = "",
) -> ImageRef:
    if len(data) > settings.vision_max_bytes:
        raise VisionError("这张图太大了，我收不下")
    media_type = sniff(data)
    if not media_type:
        raise VisionError("这东西不是我能看的图（png/jpg/gif/webp）")
    name = _stamp_name(user_id, "in", media_type)
    path = _write_artifact(storage, user_id, name, data)
    return ImageRef(
        path=path, media_type=media_type, bytes_len=len(data), origin=origin, name=label or name
    )


def adopt_file(path: Path, settings: Settings) -> ImageRef:
    """工具产物（截图、生成的图）已经在盘上，包一层就递出去。"""
    data = path.read_bytes()
    media_type = sniff(data)
    if not media_type:
        raise VisionError(f"{path.name} 不是我能看的图")
    return ImageRef(
        path=path, media_type=media_type, bytes_len=len(data), origin="文件", name=path.name
    )


def _resolve_local(token: str) -> Path:
    raw = token.strip().strip("`\"'")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise VisionError(f"这个路径我打不开：{exc}") from exc
    if not resolved.is_file():
        raise VisionError(f"没找到这张图：{raw[:80]}")
    return resolved


def find_sources(text: str) -> tuple[str, list[str]]:
    """从一句话里摘出图片来源，剩下的还是他说的话。

    只摘 data URL、图片链接、以及「带图片后缀且磁盘上真存在」的裸路径——
    这样中文句子里出现 `.png` 也不会被他自己的话牵着乱读文件。
    """
    rest = text or ""
    found: list[str] = []

    for match in list(_DATA_URL.finditer(rest)):
        found.append(match.group(0))
        rest = rest.replace(match.group(0), " ", 1)
    for match in list(_HTTP_URL.finditer(rest)):
        found.append(match.group(0))
        rest = rest.replace(match.group(0), " ", 1)
    for match in list(_BARE_PATH.finditer(rest)):
        token = match.group(0)
        if _looks_like_file(token):
            found.append(token)
            rest = rest.replace(token, " ", 1)

    cleaned = re.sub(r"\s+", " ", rest).strip()
    return cleaned, found


def _looks_like_file(token: str) -> bool:
    try:
        return _resolve_local(token).is_file()
    except VisionError:
        return False


def data_url(ref: ImageRef) -> str:
    payload = base64.b64encode(ref.path.read_bytes()).decode("ascii")
    return f"data:{ref.media_type};base64,{payload}"


def content_parts(refs: list[ImageRef], limit: int = 0) -> list[dict[str, Any]]:
    """OpenAI 多模态 content 分段：图在前、话在后，模型先看再听。"""
    take = refs[:limit] if limit > 0 else refs
    return [{"type": "image_url", "image_url": {"url": data_url(ref)}} for ref in take]


def note(refs: list[ImageRef], *, seen: bool) -> str:
    """递给语境层的一句话：他给你看了东西。

    `seen=False` 时必须把话说死——看不看得到是能力问题，编出画面是人格事故。
    """
    if not refs:
        return ""
    count = len(refs)
    tails = {"文件": "从本地递过来的", "链接": "从网上拉的", "粘贴": "直接贴进来的"}
    origin = tails.get(refs[0].origin, "递过来的")
    if not seen:
        return (
            f"他给你看了 {count} 张图（{origin}），但你这边现在看不了图。"
            "别猜、别编、别把「我看这是一张……」当免责声明——直接照你自己的方式说看不到，"
            "让他讲给你听，或者岔到别的上去。"
        )
    return (
        f"他刚给你看了 {count} 张图（{origin}），图片就在下面。"
        "像真人那样看：先说它落在你心里是什么感觉，再说你注意到的是什么具体的东西；"
        "可以不喜欢，可以只回一句，可以追问来历。"
        "**严禁报菜名式描述**（「这是一张包含……的图片」），严禁逐项清点，"
        "严禁列「图中元素」。没看准的就说没看准，文字看不清就承认看不清。"
    )


def trim(refs: list[ImageRef], limit: int) -> tuple[list[ImageRef], int]:
    """按张数闸门裁，返回留下的与被挤掉的。"""
    if len(refs) <= limit:
        return refs, 0
    return refs[:limit], len(refs) - limit


def view_url(settings: Settings, user_id: str, path: Path) -> str:
    """本地产物的静态查看链接——服务起着才点得开，所以措辞留给角色自己说。"""
    host = settings.server_host or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    from urllib.parse import quote

    return f"http://{host}:{settings.server_port}/media/{quote(user_id)}/{quote(path.name)}"

