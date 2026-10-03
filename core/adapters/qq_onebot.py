"""OneBot v11 反向 WebSocket 网桥：QQ 那一头递过来的是一句人话。

**方向要说清楚**：不是我们去连协议端，而是 LLOneBot / NapCat 那类裸机实现端
反向连进来——我们是 WebSocket **服务端**，握完手之后它把事件（`post_type`）推给我们，
我们把动作（`action` + `echo`）发回去等它应答。所以 `onebot_port` 是「等人进来」的端口，
和酒馆那头的 11555 各管一头：一个给自己人看，一个给 QQ 那侧接进来。

**为什么手搓**：环境里没有任何 WebSocket 库，而这一层需要的只是 RFC 6455 的
握手 + 帧编解码 + 掩码，标准库 `asyncio` 就够。零新依赖是这套东西能 clone 即用的前提，
也符合「不套第三方机器人框架、MySoulBot 自己就是 OneBot 服务端」这条纪律。

**这一层不生产人格**：灵魂、双层记忆、熟络度、生理节律全在 `core.bot` 那一侧，
网桥只做四件事——认得出 CQ 码、分得清私聊与群聊、把话切成人能收到的长度、
以及在她想说的时候把声音带出去。

三条安全硬线都在这一层落地：
- **鉴权**：`Authorization: Bearer <token>`（或 URL 上的 `access_token`）常量时间比；
  没配 token 就不许绑非回环地址——QQ 的事不该被整个网段读到。
- **群聊只在被点名时接话**：`@机器人` 或开头喊到她名字；顺手把发言人标成 `[昵称]: `，
  否则她会把甲说的当成乙说的。整回合走 `group_mode`，群聊准则与工具群聊锁一起生效。
- **出去的话不含可执行 CQ 码**：模型写的 `[CQ:image,file=…]` 会被换成全角括号，
  文本走消息段而不是 CQ 串——注入的口子只在那对 ASCII 方括号上。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import struct
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from config import Settings
from core.bot import BotError, MySoulBot
from core.storage_manager import PathSafetyError
from core.tools.protocol import trim_stock_closer
from core.tools.voice import VoiceError, provider_of, synthesize

logger: Final = logging.getLogger("mysoulbot.onebot")

# ---------------------------------------------------------------- WebSocket 帧
_WS_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT: Final[int] = 0x0
_OP_TEXT: Final[int] = 0x1
_OP_BINARY: Final[int] = 0x2
_OP_CLOSE: Final[int] = 0x8
_OP_PING: Final[int] = 0x9
_OP_PONG: Final[int] = 0xA
_GOING_AWAY: Final[int] = 1001

_MAX_FRAME_BYTES: Final[int] = 1_048_576  # 单帧 1MB：事件 JSON 远不到这个数，超了就是乱发
_MAX_MESSAGE_BYTES: Final[int] = 4_194_304  # 分片攒齐后的整条上限
_MAX_HEADER_LINES: Final[int] = 64
_HANDSHAKE_TIMEOUT: Final[float] = 10.0
_API_TIMEOUT: Final[float] = 20.0
_LOGIN_TIMEOUT: Final[float] = 8.0

# ---------------------------------------------------------------- 网桥策略
_PRIVATE_SPACE: Final[str] = "qq_private_"
_GROUP_SPACE: Final[str] = "qq_group_"
_ID_CHARS: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-z_.\-]")
_ID_MAX_LEN: Final[int] = 40
_MAX_OUTBOUND_CHARS: Final[int] = 800  # 企鹅一条消息吃不下太长，超了会被吞
_MAX_OUTBOUND_PIECES: Final[int] = 6  # 一次吐太多条就像机器行为
_FLOOD_LIMIT: Final[int] = 5  # 同一个空间在这扇窗口里最多接几轮
_FLOOD_WINDOW: Final[float] = 60.0
_SEEN_LIMIT: Final[int] = 512
_LOOPBACK: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})
_GROUP_FALLBACK: Final[str] = "（刚才没接上话）"
_PRIVATE_FALLBACK: Final[str] = "（我这边卡了一下）"
_HINTS: Final[dict[str, str]] = {
    "record": "（他发来一段语音，我这边听不了）",
    "video": "（他发来一段视频）",
    "file": "（他发来一个文件）",
}


class ProtocolError(RuntimeError):
    """这一帧不成样子：断掉这条连接就够，不值得惊动整个服务。"""


class _ConnectionClosed(RuntimeError):
    """对端先收了手。"""


# ---------------------------------------------------------------- 纯函数：传输
def ws_accept_key(key: str) -> str:
    """RFC 6455 规定的握手应答值。这里的 SHA-1 是协议指纹，不是安全用途。"""
    digest = hashlib.sha1(f"{key}{_WS_GUID}".encode("ascii", errors="ignore")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """服务端→客户端的帧：不掩码，按长度选 7/16/64 位写法。"""
    first = (0x80 if fin else 0x00) | opcode
    size = len(payload)
    if size < 126:
        head = struct.pack("!BB", first, size)
    elif size < 0x10000:
        head = struct.pack("!BBH", first, 126, size)
    else:
        head = struct.pack("!BBQ", first, 127, size)
    return head + payload


def unmask(data: bytes, mask: bytes) -> bytes:
    """按 4 字节循环掩码解回原文：整段异或，不逐字节走 Python 循环。"""
    if not data:
        return b""
    size = len(data)
    padded = (size + 3) // 4 * 4
    if padded != size:
        data = data + bytes(padded - size)
    stream = int.from_bytes(data, "big") ^ int.from_bytes(mask * (padded // 4), "big")
    return stream.to_bytes(padded, "big")[:size]


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    """读一帧，返回 (opcode, fin, payload)。控制帧也走这里。"""
    head = await reader.readexactly(2)
    first, second = head[0], head[1]
    if first & 0x70:
        raise ProtocolError("RSV 位必须为 0：这一层没实现任何扩展")
    fin, opcode = bool(first & 0x80), first & 0x0F
    if not second & 0x80:
        raise ProtocolError("客户端发来的帧必须带掩码")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        wide = int.from_bytes(await reader.readexactly(8), "big")
        if wide & (1 << 63):
            raise ProtocolError("64 位长度域最高位不该置起")
        length = wide
    if opcode in (_OP_CLOSE, _OP_PING, _OP_PONG) and length > 125:
        raise ProtocolError("控制帧不该超过 125 字节")
    if length > _MAX_FRAME_BYTES:
        raise ProtocolError(f"这一帧太大（{length} 字节）")
    mask = await reader.readexactly(4)
    body = await reader.readexactly(length) if length else b""
    return opcode, fin, unmask(body, mask)


@dataclass(frozen=True)
class _Request:
    method: str
    target: str
    headers: dict[str, str]


async def _read_request(reader: asyncio.StreamReader) -> _Request:
    head = await reader.readline()
    if not head:
        raise _ConnectionClosed
    try:
        method, target, _version = head.decode("latin-1").rstrip("\r\n").split(" ", 2)
    except ValueError:
        raise ProtocolError("请求行读不懂") from None
    headers: dict[str, str] = {}
    for _ in range(_MAX_HEADER_LINES):
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            return _Request(method.strip().upper(), target, headers)
        key, sep, value = line.decode("latin-1").partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    raise ProtocolError("请求头超出行数上限")


def _token_of(request: _Request) -> str:
    """握手里的鉴权串：`Authorization: Bearer x` 优先，其次 URL 上的 `access_token`。"""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme and scheme.lower() not in {"bearer", "token"}:
        value = header
    if not value.strip():
        query = parse_qs(urlparse(request.target).query)
        value = (query.get("access_token") or [""])[0]
    return value.strip()


def check_handshake(request: _Request, settings: Settings) -> tuple[int, str]:
    """返回 (拒绝状态码, 理由)；状态码 0 表示可以升级。"""
    if request.method != "GET":
        return 405, "反向 WebSocket 只接 GET 升级请求"
    if "websocket" not in request.headers.get("upgrade", "").lower():
        return 400, "这不是 WebSocket 升级请求"
    if "upgrade" not in request.headers.get("connection", "").lower():
        return 400, "Connection 头里没有 Upgrade"
    if not request.headers.get("sec-websocket-key", "").strip():
        return 400, "缺 Sec-WebSocket-Key"
    if request.headers.get("sec-websocket-version", "").strip() != "13":
        return 426, "只认 WebSocket 版本 13"
    wanted = settings.onebot_access_token.strip()
    if wanted and not secrets.compare_digest(_token_of(request), wanted):
        return 401, "鉴权串不对"
    return 0, ""


def handshake_response(key: str) -> bytes:
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n"
        "\r\n"
    ).encode("ascii")


_REJECT_PHRASE: Final[dict[int, str]] = {
    400: "Bad Request",
    401: "Unauthorized",
    405: "Method Not Allowed",
    426: "Upgrade Required",
}


def reject_response(status: int, reason: str) -> bytes:
    """状态行只能走 ASCII，中文理由留在正文里——混一起会把整个响应写成一次编码异常。"""
    body = reason.encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {_REJECT_PHRASE.get(status, 'Rejected')}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


# ---------------------------------------------------------------- 纯函数：CQ 码
@dataclass(frozen=True)
class CqCode:
    """一条 CQ 码：`[CQ:image,file=a.jpg,url=https://…]` → type + params。"""

    type: str
    params: dict[str, str] = field(default_factory=dict)

    def first(self, *keys: str) -> str:
        for key in keys:
            value = self.params.get(key, "").strip()
            if value:
                return value
        return ""


_UNESCAPE: Final[re.Pattern[str]] = re.compile(r"\[CQ:(comma|lb|rb|colon)\]|&#(44|91|93|58|10|38);", re.I)
_UNESCAPE_MAP: Final[dict[str, str]] = {
    "comma": ",",
    "lb": "[",
    "rb": "]",
    "colon": ":",
    "44": ",",
    "91": "[",
    "93": "]",
    "58": ":",
    "10": "\n",
    "38": "&",
}


def unescape_cq(text: str) -> str:
    """把 `[CQ:comma]` / `&#44;` 这类转义还原成字面字符。一次过，不层层剥。"""
    return _UNESCAPE.sub(
        lambda match: _UNESCAPE_MAP.get((match.group(1) or match.group(2) or "").lower(), ","), text
    )


def _parse_cq_body(body: str) -> CqCode:
    chunks = body.split(",")
    params: dict[str, str] = {}
    for chunk in chunks[1:]:
        key, sep, value = chunk.partition("=")
        if sep:
            params[key.strip().lower()] = unescape_cq(value.strip())
    return CqCode(chunks[0].strip().lower(), params)


def scan_cq(text: str) -> tuple[str, list[CqCode]]:
    """把一句 `raw_message` 拆成「干净的人话」与「一串码」。

    值里合法的转义本身也是一对括号（`[CQ:comma]`），所以不能拿第一个 `]` 当终止符——
    嵌套那层要跳过去。没闭合的半截不当码处理，原样留在话里。
    """
    codes: list[CqCode] = []
    kept: list[str] = []
    index, size = 0, len(text)
    while True:
        start = text.find("[CQ:", index)
        if start < 0:
            kept.append(text[index:])
            break
        kept.append(text[index:start])
        cursor = start + 4
        while cursor < size:
            if text.startswith("[CQ:", cursor):
                inner = text.find("]", cursor)
                if inner < 0:
                    break
                cursor = inner + 1
                continue
            if text[cursor] == "]":
                break
            cursor += 1
        if cursor >= size:
            kept.append(text[start : start + 4])
            index = start + 4
            continue
        codes.append(_parse_cq_body(text[start + 4 : cursor]))
        index = cursor + 1
    plain = unescape_cq("".join(kept))
    return re.sub(r"[ \t]{2,}", " ", plain).strip(), codes


# 模型偶尔照着自己的语料写出假 CQ 码；方括号换成全角，它就只是括号里的字，不再可执行
_CQ_SHAPED: Final[re.Pattern[str]] = re.compile(
    r"\[\s*(?:CQ\s*[:：]\s*)?([A-Za-z_][A-Za-z0-9_]*)([^\]\n]{0,300}?)\]"
)


def neutralize_cq(text: str) -> str:
    """挡掉出站文本里的假冒 CQ 码：只动那些长得像指令的方括号，不碰 `[笑]` `[1]`。"""

    def soften(match: re.Match[str]) -> str:
        whole = match.group(0)
        if whole[1:3].upper() == "CQ" or "=" in match.group(2):
            return f"［{whole[1:-1]}］"
        return whole

    return _CQ_SHAPED.sub(soften, text)


_CQ_ESCAPES: Final[dict[str, str]] = {"[": "[CQ:lb]", "]": "[CQ:rb]", ",": "[CQ:comma]"}
_CQ_ESCAPE_CHARS: Final[re.Pattern[str]] = re.compile(r"[\[\],]")


def cq_value(value: str) -> str:
    """CQ 码的值里 `,` `[` `]` 要转义，否则会被当成参数分隔；冒号不转（`file://` 得留着）。

    一次过：先换成 `[CQ:lb]` 再换 `]` 会把刚补进去的那个括号又啃掉。
    """
    return _CQ_ESCAPE_CHARS.sub(lambda match: _CQ_ESCAPES[match.group(0)], value)


# ---------------------------------------------------------------- 纯函数：入站解析
@dataclass(frozen=True)
class Inbound:
    """一条已经认出来的聊天消息：够引擎回一句话的全部信息。"""

    kind: str  # private | group
    target_id: int  # 回给谁：私聊是 QQ 号，群聊是群号
    sender_id: int
    sender_name: str
    text: str  # 剥掉 CQ 码之后的纯话
    images: tuple[str, ...] = ()
    mentioned: bool = False
    woke_by_name: bool = False
    message_id: str = ""

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def engine_user_id(self) -> str:
        """群聊记在「这一个群」名下，私聊记在「这一个人」名下：两套记忆从不串门。"""
        space = _GROUP_SPACE if self.is_group else _PRIVATE_SPACE
        return space + _safe_id(self.target_id if self.is_group else self.sender_id)

    @property
    def prompt_text(self) -> str:
        """群聊必须标明是谁在说话，不然她会把甲的话接到乙头上。"""
        return f"[{self.sender_name}]: {self.text}" if self.is_group else self.text

    @property
    def speakers(self) -> tuple[str, ...]:
        return (self.sender_name,) if self.is_group else ()

    @property
    def wake(self) -> bool:
        """私聊句句都接；群里只有被点名才开口——每句都插嘴是机器，不是人。"""
        return not self.is_group or self.mentioned or self.woke_by_name


def _safe_id(value: object) -> str:
    """把协议端给的 id 收成能当目录名的一串字符——越界的 id 不该变成一个目录。"""
    return _ID_CHARS.sub("", str(value if value is not None else ""))[:_ID_MAX_LEN]


def _as_id(value: object) -> int:
    digits = re.sub(r"\D", "", str(value if value is not None else ""))
    return int(digits) if digits else 0


def _segments_of(event: Mapping[str, Any]) -> tuple[str, list[CqCode]]:
    """`message` 可能是消息段数组，也可能是带 CQ 码的字符串；两种都收成同一个形状。"""
    raw = event.get("message")
    if isinstance(raw, list):
        texts: list[str] = []
        codes: list[CqCode] = []
        for segment in raw:
            if not isinstance(segment, Mapping):
                continue
            stype = str(segment.get("type") or "").strip().lower()
            data = segment.get("data")
            params = {
                str(key).strip().lower(): str(value)
                for key, value in (data.items() if isinstance(data, Mapping) else ())
            }
            if stype == "text":
                texts.append(params.get("text", ""))
            elif stype:
                codes.append(CqCode(stype, params))
        plain, tail = scan_cq("".join(texts))  # 段里的 text 仍可能夹着 CQ 串
        return plain, codes + tail
    if isinstance(raw, str) and raw.strip():
        return scan_cq(raw)
    fallback = event.get("raw_message")
    return scan_cq(fallback) if isinstance(fallback, str) else ("", [])


def parse_inbound(
    event: Mapping[str, Any], *, bot_id: int = 0, bot_names: Sequence[str] = ()
) -> Inbound | None:
    """把一条消息事件翻成 `Inbound`；不是聊天（频道通知之类）就返回 None。"""
    kind = str(event.get("message_type") or "").strip().lower()
    if kind not in {"private", "group"}:
        return None
    plain, codes = _segments_of(event)
    images: list[str] = []
    hints: list[str] = []
    mentioned = False
    for code in codes:
        if code.type == "image":
            link = code.first("url", "file")
            if link:
                images.append(link)
        elif code.type == "at":
            # @全体 是公告不是点她：只有明确点到她这个号才算被喊
            who = code.first("qq")
            mentioned = bool(bot_id) and who == str(bot_id)
        elif code.type in _HINTS:
            hints.append(_HINTS[code.type])
    text = plain.strip()
    extra = " ".join(hint for hint in dict.fromkeys(hints) if hint not in text)
    text = f"{text} {extra}".strip() if text and extra else (text or extra)

    woke_by_name = False
    if kind == "group" and not mentioned and bot_names:
        head = text.lstrip()
        for name in bot_names:
            alias = (name or "").strip()
            if alias and head.casefold().startswith(alias.casefold()):
                woke_by_name = True
                text = re.sub(r"^[\s:：,，。!！?？\-—]*", "", head[len(alias) :]).strip()
                break

    sender = event.get("sender")
    sender = sender if isinstance(sender, Mapping) else {}
    # 群名片是这个人此刻在群里显示的样子，比注册昵称更该进 `[谁]: ` 前缀
    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("nickname") or "").strip()
    sender_name = (card or nickname) if kind == "group" else (nickname or card)

    group_id = _as_id(event.get("group_id"))
    user_id = _as_id(event.get("user_id") or sender.get("user_id"))
    target_id = group_id if kind == "group" else user_id
    if not target_id:
        return None
    return Inbound(
        kind=kind,
        target_id=target_id,
        sender_id=user_id or target_id,
        sender_name=sender_name or f"QQ{user_id or target_id}",
        text=text,
        images=tuple(images),
        mentioned=mentioned,
        woke_by_name=woke_by_name,
        message_id=str(event.get("message_id") or ""),
    )


# ---------------------------------------------------------------- 纯函数：出站分段
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？!?…；;])")
_SOFT_BREAK: Final[re.Pattern[str]] = re.compile(r"[，,、；;：:\s]+")


def _hard_cut(unit: str, limit: int) -> str:
    """单句长过一条消息：在最近的逗号处切，找不到就按上限硬切。"""
    head = unit[:limit]
    best = 0
    for match in _SOFT_BREAK.finditer(head):
        best = match.end()
    return head[:best].rstrip() or head


def split_outbound(text: str, limit: int = _MAX_OUTBOUND_CHARS) -> list[str]:
    """把一段长回复切成人能一条条收到的几条：先按换行，再按句号，不切碎词。"""
    units: list[str] = []
    for block in (piece.strip() for piece in (text or "").strip().split("\n")):
        if block:
            units.extend(piece for piece in _SENTENCE_END.split(block) if piece.strip())
    pieces: list[str] = []
    current = ""
    for unit in units:
        if len(current) + len(unit) <= limit:
            current += unit
            continue
        if current:
            pieces.append(current)
            current = ""
        while len(unit) > limit:
            cut = _hard_cut(unit, limit)
            pieces.append(cut)
            unit = unit[len(cut) :].lstrip()
        current = unit
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()][: _MAX_OUTBOUND_PIECES]


# ---------------------------------------------------------------- 连接
class _Connection:
    """一条反向 WebSocket：读循环只负责收帧，回合都交给任务去跑。

    读循环里绝不能 `await` 一个动作应答——应答正是这个循环读进来的，当场等就等于
    把自己锁死（心跳也会一起卡住）。
    """

    def __init__(
        self,
        bridge: OneBotBridge,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        self._bridge = bridge
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._echo = 0
        self.peer = peer
        self.alive = True

    # ------------------------------------------------------------ 簿记
    def new_echo(self) -> str:
        self._echo += 1
        return f"msb-{id(self) % 100000}-{self._echo}"

    def track(self, coro: Any) -> None:  # noqa: ANN401 - 协程对象，由事件循环接管
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def resolve(self, echo: str, payload: dict[str, Any]) -> bool:
        future = self._pending.get(echo)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    # ------------------------------------------------------------ 收发
    async def send_json(self, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        async with self._lock:
            if not self.alive:
                raise _ConnectionClosed
            try:
                self._writer.write(encode_frame(_OP_TEXT, body))
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                self.alive = False
                raise _ConnectionClosed from exc

    async def _control(self, opcode: int, payload: bytes) -> None:
        if opcode == _OP_PING:
            async with self._lock:
                with contextlib.suppress(_ConnectionClosed, OSError):
                    self._writer.write(encode_frame(_OP_PONG, payload))
                    await self._writer.drain()
            return
        async with self._lock:
            with contextlib.suppress(OSError):
                self._writer.write(encode_frame(_OP_CLOSE, payload[:2]))
                await self._writer.drain()
        self.alive = False
        raise _ConnectionClosed

    async def _send_close(self, code: int, reason: str = "") -> None:
        body = struct.pack("!H", code) + reason.encode("utf-8")[:60]
        async with self._lock:
            with contextlib.suppress(OSError):
                self._writer.write(encode_frame(_OP_CLOSE, body))
                await self._writer.drain()

    async def read_message(self) -> tuple[int, bytes]:
        """攒齐一条完整消息（含分片），控制帧就地处理掉。"""
        buffer = bytearray()
        opening = -1
        while True:
            opcode, fin, payload = await read_frame(self._reader)
            if opcode in (_OP_CLOSE, _OP_PING, _OP_PONG):
                await self._control(opcode, payload)
                continue
            if opcode == _OP_CONT:
                if opening < 0:
                    raise ProtocolError("分片没有开头")
            elif opening >= 0:
                raise ProtocolError("上一条分片还没收尾")
            else:
                opening = opcode
            if len(buffer) + len(payload) > _MAX_MESSAGE_BYTES:
                raise ProtocolError("这一条消息太大")
            buffer += payload
            if not fin:
                continue
            message, opcode = bytes(buffer), opening
            buffer = bytearray()
            opening = -1
            return opcode, message

    async def serve(self) -> None:
        while True:
            try:
                opcode, payload = await self.read_message()
            except (asyncio.IncompleteReadError, _ConnectionClosed) as exc:
                logger.info("协议端先结束了这条连接（%s）：%s", self.peer, exc)
                return
            except ProtocolError as exc:
                # 给一个像样的关闭码：对面是裸机协议端，RST 只会让它记一句「连接被重置」，
                # 而 1002/1009 能让运维一眼看出是帧的问题还是有人在乱发
                logger.info("协议端帧不成样子（%s）：%s", self.peer, exc)
                await self._send_close(1009 if "太大" in str(exc) else 1002, str(exc))
                return
            if opcode not in (_OP_TEXT, _OP_BINARY):
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                logger.info("协议端发来非 UTF-8 帧，已丢掉（%d 字节）", len(payload))
                continue
            await self._bridge.on_frame(self, text)

    async def close(self, code: int = _GOING_AWAY, reason: str = "mysoulbot 收工") -> None:
        """先等在手的回合说完，再把手挥出去——话说一半被掐断是最难看的收场。"""
        self.alive = False
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=self._bridge.drain_grace())
            for task in pending:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await self._send_close(code, reason)
        with contextlib.suppress(OSError):
            self._writer.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()


def _drop(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        writer.close()


# ---------------------------------------------------------------- 网桥
class OneBotBridge:
    """QQ 那侧的入口：认事件、叫醒她、把她说的话切好送回去。"""

    def __init__(self, settings: Settings, bot: MySoulBot) -> None:
        self._settings = settings
        self._bot = bot
        self._server: asyncio.Server | None = None
        self._connections: set[_Connection] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._opened: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pulses: dict[str, list[float]] = {}
        self._counts = {
            "events": 0,
            "replies": 0,
            "ignored": 0,
            "not_woken": 0,
            "flood": 0,
            "busy": 0,
            "duplicate": 0,
            "errors": 0,
        }
        self.bot_names: tuple[str, ...] = tuple(
            name.strip()
            for name in (settings.onebot_bot_name or "").replace("，", ",").split(",")
            if name.strip()
        )
        self.bot_id: int = 0
        self.last_heartbeat: float | None = None
        self.peer_online: bool | None = None
        self.active_turns = 0
        self._whoami_tries = 0

    # ------------------------------------------------------------ 生命周期
    async def start(self, host: str = "", port: int | None = None) -> tuple[str, int]:
        host = host or self._settings.onebot_host
        port = self._settings.onebot_port if port is None else port
        if not self._settings.onebot_access_token.strip() and not self._is_loopback(host):
            raise RuntimeError(
                "OneBot 网桥要绑在非回环地址上却没配 ONEBOT_ACCESS_TOKEN——那等于把 QQ 的进出"
                "整个摊给网段里任何人。配上鉴权串，或把 ONEBOT_HOST 收回 127.0.0.1。"
            )
        self._server = await asyncio.start_server(self._handle, host, port)
        bound = self._server.sockets[0].getsockname() if self._server.sockets else (host, port)
        logger.info("OneBot 反向 WS 已监听 ws://%s:%s（等协议端连进来）", bound[0], bound[1])
        return str(bound[0]), int(bound[1])

    async def close_listener(self) -> None:
        """不再接新的协议端连接；已经进来的那一条留着把话说完。"""
        if self._server is not None:
            # 这里不碰 `wait_closed()`：那条协程要等**所有**在途连接的 handler 收尾，
            # 而反向 WS 的 handler 是按小时计的——等它就等于把停机卡死。
            self._server.close()
            self._server = None

    async def stop(self) -> None:
        for connection in list(self._connections):
            await connection.close()
        self._connections.clear()
        await self.close_listener()
        self._locks.clear()
        self._opening.clear()

    def drain_grace(self) -> float:
        return max(1.0, min(self._settings.drain_timeout_seconds, 30.0))

    @staticmethod
    def _is_loopback(host: str) -> bool:
        return host in _LOOPBACK or host.startswith("127.")

    # ------------------------------------------------------------ 接线
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = "?"
        try:
            address = writer.get_extra_info("peername") or ()
            peer = ":".join(str(part) for part in address[:2]) or "?"
            request = await asyncio.wait_for(_read_request(reader), timeout=_HANDSHAKE_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError, ProtocolError, OSError) as exc:
            logger.info("握手没谈成（%s）：%s", peer, exc)
            _drop(writer)
            return
        status, reason = check_handshake(request, self._settings)
        if status:
            logger.warning("拒掉一次反向 WS 握手（%s · %s）：%s", peer, request.method, reason)
            try:
                writer.write(reject_response(status, reason))
                await writer.drain()
            except OSError:
                pass
            _drop(writer)
            return
        connection = _Connection(self, reader, writer, peer)
        self._connections.add(connection)
        try:
            writer.write(handshake_response(request.headers.get("sec-websocket-key", "").strip()))
            await writer.drain()
            logger.info("协议端已接入（%s）", peer)
            await connection.serve()
        except (ProtocolError, _ConnectionClosed, asyncio.IncompleteReadError) as exc:
            logger.info("协议端连接结束（%s）：%s", peer, exc)
        except Exception as exc:  # noqa: BLE001 - 一条连接不该带崩整个服务
            logger.warning("反向 WS 连接异常（%s）：%s", peer, exc, exc_info=True)
        finally:
            self._connections.discard(connection)
            connection.alive = False
            _drop(writer)

    # ------------------------------------------------------------ 事件分流
    async def on_frame(self, connection: _Connection, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self._bump("ignored")
            return
        if not isinstance(payload, dict):
            self._bump("ignored")
            return
        echo = str(payload.get("echo") or "")
        if echo and connection.resolve(echo, payload):
            return
        post_type = str(payload.get("post_type") or "")
        if post_type == "meta_event":
            self._on_meta(connection, payload)
            return
        if post_type == "message":
            self._bump("events")
            self.bot_id = _as_id(payload.get("self_id")) or self.bot_id
            connection.track(self._on_message(connection, payload))
            return
        # 加好友、进群申请这类一律不自动处置：那种事要人来点头
        self._bump("ignored")

    def _on_meta(self, connection: _Connection, event: Mapping[str, Any]) -> None:
        """心跳：保活读数记一下；顺手确认一次「我是谁」，群聊里认名字才有的依据。"""
        if str(event.get("meta_event_type") or "") != "heartbeat":
            return
        self.last_heartbeat = time.time()
        status = event.get("status")
        if isinstance(status, Mapping) and "online" in status:
            self.peer_online = bool(status.get("online"))
        self.bot_id = _as_id(event.get("self_id")) or self.bot_id
        if self.bot_names or self._whoami_tries >= 3:
            return
        self._whoami_tries += 1
        connection.track(self._whoami(connection))

    async def _whoami(self, connection: _Connection) -> None:
        data = await self._call(connection, "get_login_info", {}, timeout=_LOGIN_TIMEOUT)
        if not isinstance(data, Mapping):
            return
        self.bot_id = _as_id(data.get("user_id")) or self.bot_id
        nickname = str(data.get("nickname") or "").strip()
        if nickname and nickname not in self.bot_names:
            self.bot_names = (nickname, *self.bot_names)
        logger.info("协议端报来的身份：%s（%s）", nickname or "无名", self.bot_id or "未知号")

    # ------------------------------------------------------------ 一条消息
    async def _on_message(self, connection: _Connection, event: Mapping[str, Any]) -> None:
        inbound = parse_inbound(event, bot_id=self.bot_id, bot_names=self.bot_names)
        if inbound is None:
            self._bump("ignored")
            return
        if self.bot_id and inbound.sender_id == self.bot_id:
            self._bump("ignored")
            return  # 她自己的话再喂给自己，就是无限自 talk
        if not inbound.wake:
            self._bump("not_woken")
            return
        if not (inbound.text or inbound.images):
            self._bump("ignored")
            return
        if self._is_duplicate(inbound.message_id):
            self._bump("duplicate")
            return
        if self._too_many(inbound.engine_user_id):
            self._bump("flood")
            logger.info("%s 这一分钟说得太密，这一句先不接", inbound.engine_user_id)
            return
        await self._answer(connection, inbound)

    def _is_duplicate(self, message_id: str) -> bool:
        """同一个 message_id 只接一次：协议端重发、双端同连都不该让同一句话被答两遍。"""
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        if len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return False

    def _too_many(self, user_id: str) -> bool:
        now = time.monotonic()
        stamps = [stamp for stamp in self._pulses.get(user_id, []) if now - stamp < _FLOOD_WINDOW]
        if len(stamps) >= _FLOOD_LIMIT:
            self._pulses[user_id] = stamps
            return True
        stamps.append(now)
        self._pulses[user_id] = stamps
        return False

    async def _ensure(self, user_id: str) -> bool:
        if user_id in self._opened:
            return True
        lock = self._opening.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id in self._opened:
                return True
            try:
                await self._bot.open_session(user_id, restore=True)
            except PathSafetyError as exc:
                logger.warning("这个 id 越界了，消息丢掉：%s", exc)
                return False
            except Exception as exc:  # noqa: BLE001 - 会话开不出来就是接不了话
                logger.warning("%s 会话开不出来: %s", user_id, exc)
                return False
            self._opened.add(user_id)
            return True

    async def _answer(self, connection: _Connection, inbound: Inbound) -> None:
        user_id = inbound.engine_user_id
        if not await self._ensure(user_id):
            return
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            # 上一句还在说：插进来的只会让两条回复串在一起，不如丢掉
            self._bump("busy")
            return
        async with lock:
            self.active_turns += 1
            try:
                said = await self._generate(user_id, inbound)
                if not said:
                    return
                await self._deliver(connection, inbound, said)
                self._bump("replies")
                await self._speak(connection, inbound, said)
            finally:
                self.active_turns -= 1

    async def _generate(self, user_id: str, inbound: Inbound) -> str:
        pieces: list[str] = []
        try:
            async for delta in self._bot.stream_reply(
                user_id,
                inbound.prompt_text,
                today=dt.date.today(),
                images=list(inbound.images),
                speakers=list(inbound.speakers),
                group_mode=inbound.is_group,
            ):
                pieces.append(delta)
        except BotError as exc:
            logger.info("QQ 这一回合没说完：%s", exc.message)
            if not pieces:
                return _GROUP_FALLBACK if inbound.is_group else f"{_PRIVATE_FALLBACK}{exc.message}"
        except Exception as exc:  # noqa: BLE001 - 意外故障的细节不递到 QQ 上
            logger.warning("QQ 回合故障: %s", exc, exc_info=True)
            if not pieces:
                return _GROUP_FALLBACK if inbound.is_group else _PRIVATE_FALLBACK
        text = "".join(pieces).strip()
        if self._settings.trim_stock_closers:
            text = trim_stock_closer(text)
        return text

    async def _deliver(self, connection: _Connection, inbound: Inbound, text: str) -> None:
        """文本一律走消息段：CQ 串会把模型写的方括号当真指令执行，消息段不会。"""
        for piece in split_outbound(neutralize_cq(text)):
            await self._call(
                connection,
                self._action(inbound),
                self._params(inbound, [{"type": "text", "data": {"text": piece}}]),
            )

    async def _speak(self, connection: _Connection, inbound: Inbound, text: str) -> None:
        """说完顺手念一遍。群里不出声：连着甩六条语音那是骚扰，不是拟人。"""
        if not self._settings.onebot_auto_record or inbound.is_group:
            return
        if not self._settings.tts_enabled or provider_of(self._settings) == "none":
            return
        try:
            clip = await synthesize(text, self._settings, self._bot.storage, inbound.engine_user_id)
        except (VoiceError, PathSafetyError, OSError) as exc:
            logger.info("%s 没念出声: %s", inbound.engine_user_id, exc)
            return
        voice = f"[CQ:record,file={cq_value(f'file://{clip.path}')}]"
        await self._call(connection, self._action(inbound), self._params(inbound, voice))

    def _action(self, inbound: Inbound) -> str:
        return "send_group_msg" if inbound.is_group else "send_private_msg"

    def _params(self, inbound: Inbound, message: Any) -> dict[str, Any]:  # noqa: ANN401 - 段数组或 CQ 串
        key = "group_id" if inbound.is_group else "user_id"
        return {key: inbound.target_id, "message": message}

    async def _call(
        self,
        connection: _Connection,
        action: str,
        params: Mapping[str, Any],
        *,
        timeout: float = _API_TIMEOUT,
    ) -> Any | None:  # noqa: ANN401 - 协议端给什么 data 就原样转出去
        if not connection.alive:
            self._bump("errors")
            return None
        echo = connection.new_echo()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        connection._pending[echo] = future  # noqa: SLF001 - 同模块内的连接簿记
        try:
            await connection.send_json({"action": action, "params": dict(params), "echo": echo})
        except _ConnectionClosed:
            connection._pending.pop(echo, None)
            self._bump("errors")
            return None
        try:
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._bump("errors")
            logger.warning("协议端对 %s 没在 %ss 内应答", action, timeout)
            return None
        except asyncio.CancelledError:
            connection._pending.pop(echo, None)
            raise
        finally:
            connection._pending.pop(echo, None)
        try:
            retcode = int(reply.get("retcode") or 0)
        except (TypeError, ValueError):
            retcode = 0
        status = str(reply.get("status") or "")
        if status == "failed" or retcode:
            self._bump("errors")
            logger.warning(
                "协议端对 %s 回了 retcode=%s status=%s message=%s",
                action,
                retcode,
                status,
                str(reply.get("message") or "")[:120],
            )
            return None
        return reply.get("data")

    # ------------------------------------------------------------ 读数
    def _bump(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    def status(self) -> dict[str, Any]:
        bound = None
        if self._server is not None and self._server.sockets:
            address = self._server.sockets[0].getsockname()
            bound = {"host": str(address[0]), "port": int(address[1])}
        return {
            "enabled": True,
            "listening": self._server is not None,
            "bound": bound,
            "connections": len(self._connections),
            "self_id": str(self.bot_id) if self.bot_id else "",
            "bot_names": list(self.bot_names),
            "authenticated": bool(self._settings.onebot_access_token.strip()),
            "heartbeat_seconds_ago": (
                round(time.time() - self.last_heartbeat, 1) if self.last_heartbeat else None
            ),
            "peer_online": self.peer_online,
            "auto_record": self._settings.onebot_auto_record,
            "active_turns": self.active_turns,
            "counts": dict(self._counts),
        }
