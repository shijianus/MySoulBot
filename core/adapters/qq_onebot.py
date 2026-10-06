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
from pathlib import Path
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from random import choice, uniform
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlparse

from config import PROJECT_ROOT, Settings
from core.bot import BotError, MySoulBot
from core.identity import PairingDesk, consume_pairing
from core.pair_phrase import make_greeting, short_ask
from core.secrecy import Leak, guard as guard_secrecy
from core.stickers import StickerBook
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
# 改头像要上传，比一般 API 动作慢：给到 30s，别卡在默认 20s 上误判成失败
_PROFILE_TIMEOUT: Final[float] = 30.0

# ---------------------------------------------------------------- 网桥策略
_PRIVATE_SPACE: Final[str] = "qq_private_"
_GROUP_SPACE: Final[str] = "qq_group_"
_ID_CHARS: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-z_.\-]")
_ID_MAX_LEN: Final[int] = 40
_MAX_OUTBOUND_CHARS: Final[int] = 800  # 企鹅一条消息吃不下太长，超了会被吞
# 长答案就是要多发几条：这条上限只拦病态输入，绝不为了「少刷屏」把内容吃掉
_MAX_OUTBOUND_PIECES: Final[int] = 24
_FLOOD_LIMIT: Final[int] = 5  # 同一个空间在这扇窗口里最多接几轮
_FLOOD_WINDOW: Final[float] = 60.0
_SEEN_LIMIT: Final[int] = 512
_LOOPBACK: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})
# 兜底句不许用括号：出站有一道「擦舞台腔」的红线，括号句会被擦成空串，
# 那样失败就成了彻底沉默——比说一句「没接住」更糟。措辞只说她自己的听感，
# 不报模型、卡死、报错这类系统词：那是把机房的动静端到对方面前。备选在 onebot_fail_lines。
_HINTS: Final[dict[str, str]] = {
    "record": "（他发来一段语音，我这边听不了）",
    "video": "（他发来一段视频）",
    "file": "（他发来一个文件）",
}
# QQ 自带小表情的 id → 中文名。不追求全表：认得出的报名字，认不出的报 id，不编。
_FACE_NAMES: Final[dict[str, str]] = {
    "1": "微笑", "2": "撇嘴", "3": "色", "4": "发呆", "5": "得意", "6": "流泪", "7": "害羞",
    "8": "闭嘴", "9": "睡", "10": "大哭", "11": "尴尬", "12": "发怒", "13": "调皮", "14": "呲牙",
    "15": "惊讶", "16": "难过", "17": "酷", "18": "冷汗", "19": "抓狂", "20": "吐", "21": "偷笑",
    "22": "爱", "23": "白眼", "24": "傲慢", "25": "饥饿", "26": "困", "27": "恐惧", "28": "跑",
    "29": "头顶遭雷", "30": "喝一杯", "31": "公交", "32": "无聊", "33": "倒挂", "34": "抠鼻",
    "35": "鼓掌", "36": "捂嘴", "37": "偷窥", "38": "笑哭", "39": "无语", "40": "嘿哈",
    "41": "捂脸", "42": "干杯", "43": "拳头", "44": "OK", "45": "爱心", "46": "心碎", "47": "玫瑰",
    "66": "裂开",
}
# 只带一个 id 回来、内容得再问一次协议端的段
_QUOTE_TYPES: Final[frozenset[str]] = frozenset({"reply", "forward"})
# 明确要语音的说法。宁可窄一点：只认「要一条声音」这几种，别把「听我说」也算进去
_VOICE_ASK: Final[re.Pattern[str]] = re.compile(
    r"(发|来|给|整|录|发条|一条|一段|说条)\s*(语音|条语音|段语音)|"
    r"(念|读|讲|说)一?(遍|一下|出来)|语音(说|讲|发)|用语音"
)
_QUOTE_TIMEOUT: Final[float] = 8.0
_QUEUE_MAX: Final[int] = 8  # 排队上限：再多也该一次说完，而不是攒成连珠炮  # 回查原文别把整条回复拖住：协议端不认这个接口时 8s 就放手
_SEMANTIC_MAX: Final[int] = 80  # 语义占位里引用的原文掐这么长
_SILENCE_MARKS: Final[frozenset[str]] = frozenset({"", "[[静默]]", "[[SILENT]]", "[[skip]]"})


def _short(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= _SEMANTIC_MAX else text[:_SEMANTIC_MAX].rstrip() + "…"


def _file_leaf(name: str) -> str:
    """`file://` 或 `cqhttp://` 前缀与目录都剥掉，只留她会说出口的那个文件名。"""
    tail = (name or "").replace("\\", "/").rstrip("/").split("/")[-1]
    return unquote(tail) or "未命名文件"


def describe_code(code: CqCode) -> str:
    """把一条非文本消息段翻成提示词里的一行人话。

    她看不见媒介本体时，至少要知道对方刚刚做了什么动作——甩了个表情、拍了张照、
    还是转了一整屏聊天记录。这一层不猜内容，只报形式。
    """
    ctype = code.type
    if ctype == "face":
        face_id = code.first("id")
        name = _FACE_NAMES.get(face_id)
        return f"[表情: {name}]" if name else f"[表情#{face_id or '?'}]"
    if ctype in {"mface", "marketface", "sticker", "animface", "jsonface"}:
        name = code.first("name", "desc", "summary")
        return f"[动画表情: {_short(name)}]" if name else "[动画表情]"
    if ctype == "dice":
        return f"[骰子: {code.first('result', 'value', 'text') or '?'}]"
    if ctype == "rock":
        return f"[猜拳: {code.first('result', 'value') or '?'}]"
    if ctype == "image":
        return "[图片]"
    if ctype == "file":
        return f"[文件: {_file_leaf(code.first('file', 'name'))}]"
    if ctype == "record":
        return "[语音]"
    if ctype == "video":
        return "[视频]"
    if ctype == "at":
        return ""  # @ 是结构不是内容：谁喊她已经由 mentioned 记下了
    if ctype == "reply":
        who = code.first("nick", "name")
        body = _short(code.first("text", "content"))
        if body:
            return f'[回复 @{who or "某人"}: "{body}"]'
        # 不带消息号：那是接口内部标识，递给她就只能回出一句「这条没取回来」这种机器话
        return "[有人引用了一条消息，那条的内容没跟着露出来]"
    if ctype == "forward":
        nodes = code.params.get("nodes") or code.params.get("content")
        if isinstance(nodes, list) and nodes:
            return describe_forward(nodes)
        return "[有人转来一屏聊天记录，内容没跟着露出来]"
    if ctype in {"xml", "json", "jsonarray", "miniapp", "location", "contact", "music", "video-str"}:
        return "[卡片消息]"
    return f"[{ctype}]"


def _inline_quote_text(code: CqCode) -> str:
    """消息段里已经带着原文就不必再问接口：转发的整屏在 `data.content`，引用的在 `text/content`。

    NapCat 合并转发常把节点数组直接放在事件里——那是最完整的一份，接口回查反而可能被拒。
    """
    nodes = code.thing("content", "nodes", "node_list")
    if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes)) and nodes:
        return describe_forward(nodes, limit=8)
    body = code.thing("text") or code.thing("content")
    if isinstance(body, str) and body.strip():
        return f'[被引用的一条: "{_short(body.strip())}"]'
    return ""


def describe_forward(nodes: Sequence[Any], *, limit: int = 4) -> str:
    """合并转发：报条数、报都有谁在说，再挑前几条原话——她把一整屏当一句话读会错。"""
    lines: list[str] = []
    who: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        sender = node.get("sender_name") or node.get("nickname") or node.get("card") or ""
        sender = str(sender).strip()
        if sender and sender not in who:
            who.append(sender)
        body = node.get("message")
        if isinstance(body, list):
            text = "".join(
                str((item.get("data") or {}).get("text", ""))
                for item in body
                if isinstance(item, Mapping) and item.get("type") == "text"
            )
        else:
            text = str(body or "")
        text = _short(text)
        if text and len(lines) < limit:
            lines.append(f"{sender + '：' if sender else ''}{text}")
    head = f"[聊天记录: 共 {len(nodes)} 条"
    if who:
        head += f"，{len(who)} 个人在说（{'、'.join(who[:5])}）"
    head += "]"
    if not lines:
        return head
    return f"{head}\n" + "\n".join(f"  · {line}" for line in lines) + ("…" if len(nodes) > limit else "")


_HOLD_MAX_WAIT: Final[float] = 45.0  # 她正说得长时，攒着的那批最多再等这么久
_LAST_SEEN_MAX: Final[int] = 512  # 「上一句什么时候来的」记到这么多空间为止，多了丢最旧的


class _Batch:
    """一个空间里攒着的一串话，以及它的防抖定时器。"""

    __slots__ = ("user_id", "opened_at", "items", "connection", "timer", "retries")

    def __init__(self, user_id: str, opened_at: float) -> None:
        self.user_id = user_id
        self.opened_at = opened_at
        self.items: list[Inbound] = []
        self.connection: _Connection | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.retries = 0

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class _Sampler:
    """三个数就够：平均、最坏、最后一次，再加样本条数。

    延时要看趋势与上限，不需要直方图；不存历史，连跑几天也不涨内存。
    """

    __slots__ = ("n", "total", "worst", "last")

    def __init__(self) -> None:
        self.n = 0
        self.total = 0.0
        self.worst = 0.0
        self.last = 0.0

    def add(self, value: float) -> None:
        self.n += 1
        self.total += value
        self.worst = max(self.worst, value)
        self.last = value

    def view(self) -> dict[str, Any]:
        if not self.n:
            return {"avg": None, "max": None, "last": None, "n": 0}
        return {
            "avg": round(self.total / self.n, 1),
            "max": round(self.worst, 1),
            "last": round(self.last, 1),
            "n": self.n,
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
    """一条消息段：`[CQ:image,file=a.jpg,url=https://…]` 或数组段 → type + params。

    `raw` 留着**原样**的 data：合并转发的 `content` 是一串节点、引用的 `seq` 是数字，
    把它们 `str()` 成字符串就只剩一坨 Python 表示——转发的聊天记录正是这样丢掉的。
    """

    type: str
    params: dict[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def first(self, *keys: str) -> str:
        for key in keys:
            value = self.params.get(key, "").strip()
            if value:
                return value
        return ""

    def thing(self, *keys: str) -> Any:  # noqa: ANN401 - 节点数组、数字 seq 都从这里取
        """按原始类型取 data 里的一个字段（列表就是列表，数字就是数字）。"""
        for key in keys:
            value = self.raw.get(key)
            if value not in (None, "", [], {}):
                return value
        return None


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


def plain_text(body: str) -> str:
    """把 markdown 洗成 QQ 里真看得懂的字。

    QQ 不渲染 markdown：`**加粗**` 发出去就是两颗星，`# 标题` 就是个字幕号，
    「- 列表」原样贴过去更像机器在写文档。代码块**保留**——那是给人复制的，
    洗掉就废了用户要的「能直接复制使用」。
    """
    text = body or ""
    text = re.sub(r"^#{1,6}[ \t]*", "", text, flags=re.M)          # 标题记号
    text = re.sub(r"\*\*(?=\S)(.+?)\*\*", r"\1", text)              # 加粗
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)\*(?![\w*])", r"\1", text)  # 斜体（不动 * 动作描写）
    text = re.sub(r"^([ \t]*)[-•●][ \t]+", r"\1", text, flags=re.M)  # 无序列表符号
    text = re.sub(r"^([ \t]*)\d{1,2}[.)、][ \t]+", r"\1", text, flags=re.M)  # 有序列表编号
    text = re.sub(r"^>[ \t]?", "", text, flags=re.M)                # 引用竖线
    # [文字](链接) → 文字 链接：QQ 里点不动那种包装，不如把网址直接摊出来
    text = re.sub(r"\[([^\]]*)\]\(([^)\s]+)\)",
                  lambda m: m.group(1).strip() + " " + m.group(2).strip() if m.group(1).strip()
                  else m.group(2).strip(), text)
    text = re.sub(r"~~(?=\S)(.+?)~~", r"\1", text)                  # 删除线
    # 行内反引号拆掉（QQ 不会变等宽），三引号的代码块整块留着给人复制
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


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
    mentioned_other: bool = False  # 只 @ 了别人：那是别人的对齐，她不插手
    quotes: tuple[tuple[str, str], ...] = ()  # 只拿到 id 的引用/转发，内容得再问一次协议端
    quoted: tuple[str, ...] = ()  # 已经成形的引用/转达上文，单独成块喂给模型
    # 这一句到达的那一刻（monotonic 刻度）。compare=False：它是读数，不是消息内容，
    # 不该让「同样一句话」因为到达时间不同而不相等。
    received_at: float = field(default=0.0, compare=False)
    # 他这一句里明确要语音（「念一遍」「发条语音」）：群聊那扇默认关着的门只为这句开
    voice_requested: bool = False

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
        """群聊必须标明是谁在说话，不然她会把甲的话接到乙头上。

        被引用、被转述的上文单独成块摆在前面：那才是这一问的真正题目，
        混进当前那句话里她就会只回后半句。
        """
        current = f"[{self.sender_name}]: {self.text}" if self.is_group else self.text
        if not self.quoted:
            return current
        # 有上文才分块：不然大白话一句也被套上格式，反而看不清谁在说
        lines = [f"【引用/转达上下文】{entry}" for entry in self.quoted]
        lines.append(f"【当前群聊发言】{current}" if self.is_group else current)
        return "\n".join(lines)

    @property
    def speakers(self) -> tuple[str, ...]:
        return (self.sender_name,) if self.is_group else ()

    @property
    def wake(self) -> bool:
        """私聊句句都接；群里只有被点名才开口——每句都插嘴是机器，不是人。"""
        return not self.is_group or self.mentioned or self.woke_by_name


def decide_wake(
    inbound: Inbound, *, always_reply: bool = False, discretion: bool = False
) -> tuple[bool, bool]:
    """这一句要不要接，以及接了之后允不允许她选择不说话。

    顺序是硬性的：被点名永远优先于任何静默规则；只 @ 了别人则优先于任何插话欲望——
    那是别人的对齐，她凑上去就是讨嫌。剩下没人点名的才有全量监听与自主裁决的余地。
    """
    if not inbound.is_group:
        return True, False
    if inbound.mentioned or inbound.woke_by_name:
        return True, False
    if inbound.mentioned_other:
        return False, False
    if always_reply:
        return True, False
    if discretion:
        return True, True
    return False, False


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
            data = data if isinstance(data, Mapping) else {}
            params = {str(key).strip().lower(): str(value)
                      for key, value in data.items()
                      if isinstance(value, (str, int, float, bool))}
            if stype == "text":
                texts.append(params.get("text", ""))
            elif stype:
                codes.append(CqCode(stype, params, data))
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
    quotes: list[tuple[str, str]] = []
    quote_lines: list[str] = []
    mentioned = False
    mentioned_other = False
    for code in codes:
        if code.type == "at":
            # @全体 是公告不是点她：只有明确点到她这个号才算被喊
            who = code.first("qq")
            if who == "all":
                continue
            if bool(bot_id) and who == str(bot_id):
                mentioned = True
            elif who:
                mentioned_other = True  # 只 @ 了别人：那是别人的对齐，不插手
            continue
        if code.type == "image":
            link = code.first("url", "path", "file")
            if link:
                images.append(link)
            # 协议端自己给的图片摘要（QQ 服务端认出来的文字/内容）：看不见图的时候至少有个影
            note = code.first("summary", "desc")
            if note:
                hints.append(f"[图片摘要: {_short(note)}]")
        if code.type in _QUOTE_TYPES:
            said = describe_code(code)
            if said:
                quote_lines.append(said)
            inline = _inline_quote_text(code)
            if inline:
                quote_lines.append(inline)
            else:
                # NapCat 的引用段是 {id?, seq?}，并写明「seq 优先使用」：
                # 以前只读 id，于是十次引用九次拿不到号，她永远不知道对方引用了什么
                token = code.first("seq", "id")
                if token:
                    quotes.append((code.type, token))
            continue
        said = describe_code(code)
        if said:
            hints.append(said)
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
        mentioned_other=mentioned_other,
        quotes=tuple(quotes),
        quoted=tuple(quote_lines),
        received_at=time.monotonic(),
        voice_requested=bool(_VOICE_ASK.search(text)),
    )


# ---------------------------------------------------------------- 纯函数：出站分段
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？!?…；;])")
_SOFT_BREAK: Final[re.Pattern[str]] = re.compile(r"[，,、；;：:\s]+")
# 代码块与连号列表是「拿去用」的内容：切成两半就废了。分片前先摘出来，切完再原样填回
_FENCE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.S)
_LIST_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"(?:^[ \t]*(?:[-*·]|\d+[.)])[ \t]+\S[^\n]*(?:\n|$)){2,}", re.M
)
_SLOT: Final[re.Pattern[str]] = re.compile(r"\x00(\d{1,4})\x00")


def _protect_atomic(text: str, slots: list[str]) -> str:
    """把不可切的内容换成占位符，让下面的句子切法别碰到它。"""

    def stash(match: re.Match[str]) -> str:
        slots.append(match.group(0))
        return f"\x00{len(slots) - 1}\x00"

    return _LIST_BLOCK.sub(stash, _FENCE.sub(stash, text or ""))


def _split_atomic(block: str, limit: int) -> list[str]:
    """原子块自己长过一条消息：按行下刀，绝不在半行处断。

    代码块还得每条**各自闭合**——切成两半的 ``` 谁都还原不出来，
    而每条都是完整围栏的话，对方一条一条复制回去就是能跑的东西。
    """
    if len(block) <= limit:
        return [block]
    fence = re.match(r"^```[ \t]*([^\n`]*)\n(.*)\n?```$", block, re.S)
    body, lang = (fence.group(2), fence.group(1).strip()) if fence else (block, "")
    cap = max(60, limit - (8 + len(lang)))
    parts: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        while len(line) > cap:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:cap])
            line = line[cap:]
        if current and len(current) + len(line) > cap:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    if not fence:
        return parts
    return [f"```{lang}\n{part.rstrip()}\n```" for part in parts]


def _restore_atomic(piece: str, slots: list[str]) -> list[str]:
    """把占位符换回原文，**顺序不变**：块前的话一条、代码块整条、块后的话再一条。

    代码块单独成一条是刻意的——对方要复制的是能跑的东西，夹在句子中间就没法复制。
    """
    if "\x00" not in piece:
        return [piece]
    out: list[str] = []
    buffer = ""
    cursor = 0
    for match in _SLOT.finditer(piece):
        buffer += piece[cursor:match.start()]
        index = int(match.group(1))
        block = slots[index] if 0 <= index < len(slots) else ""
        if buffer.strip():
            out.append(buffer.strip())
        buffer = ""
        out.extend(_split_atomic(block, _MAX_OUTBOUND_CHARS))
        cursor = match.end()
    buffer += piece[cursor:]
    if buffer.strip():
        out.append(buffer.strip())
    return out


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
    out = [piece.strip() for piece in pieces if piece.strip()]
    # 条数封顶是防刷屏，不是丢内容的理由：超出的并进最后一条，一个字都不许少
    if len(out) > _MAX_OUTBOUND_PIECES:
        head, tail = out[: _MAX_OUTBOUND_PIECES - 1], "\n".join(out[_MAX_OUTBOUND_PIECES - 1:])
        while tail and len(tail) > _MAX_OUTBOUND_CHARS:
            cut = _hard_cut(tail, _MAX_OUTBOUND_CHARS)
            head.append(cut)
            tail = tail[len(cut):].strip()
        if tail:
            head.append(tail)
        out = head
    return out


def merge_inbound(items: Sequence[Inbound]) -> Inbound:
    """把攒下来的一串消息并成一次发言：先说的话也在同一份上下文里。

    每行保留 `[谁]: ` 前缀（群聊），所以合并不丢发言人；图片去重合并；
    被 @ / 被喊到名字只要出现在任意一条里，整批就算被点名。
    """
    if not items:
        raise ValueError("空批次不该进来")
    if len(items) == 1:
        return items[0]
    last = items[-1]
    lines: list[str] = []
    for item in items:
        line = item.prompt_text.strip()
        if line:
            lines.append(line)
    images: list[str] = []
    quotes: list[tuple[str, str]] = []
    quoted: list[str] = []
    for item in items:
        for entry in item.quoted:
            if entry not in quoted:
                quoted.append(entry)
        for link in item.images:
            if link not in images:
                images.append(link)
        for quote in item.quotes:
            if quote not in quotes:
                quotes.append(quote)
    return replace(
        last,
        text="\n".join(lines),
        quoted=tuple(quoted),
        images=tuple(images),
        quotes=tuple(quotes),
        mentioned=any(item.mentioned for item in items),
        woke_by_name=any(item.woke_by_name for item in items),
        mentioned_other=any(item.mentioned_other for item in items),
        # 计时从最早那一句算起：对面是从小明发第一句开始等的，不是最后一句
        received_at=min((item.received_at for item in items if item.received_at),
                        default=last.received_at),
    )


_STAR_STAGE: Final[re.Pattern[str]] = re.compile(r"\*[^*\n]{1,40}\*")
_HEAD_STAGE: Final[re.Pattern[str]] = re.compile(r"^\s*[（(][^（）()\n]{1,16}[)）][\s,，。]*")
_TAIL_STAGE: Final[re.Pattern[str]] = re.compile(r"[,，]?\s*[（(][^（）()\n]{1,16}[)）]\s*$")
# 句内的说话神态：只认这一小撮动词，普通插入语（「不是骂你」「要是下雨」）一律不碰
_INLINE_STAGE: Final[re.Pattern[str]] = re.compile(
    r"[（(](?:抬眼|抬了抬眼|瞥|瞅|撇嘴|抿嘴|小声|嘟囔|嘀咕|叹气|叹了口气|翻[^（）()\n]{0,3}白眼|白眼|"
    r"想了想|顿了顿|歪头|歪了歪头|偷笑|笑了笑|冷哼|哼了一声|尾鳍|尾巴|摆尾|拍水|蹭|趴|炸毛)"
    r"[^（）()\n]{0,8}[)）][\s,，。]*"
)


def strip_stage_directions(text: str) -> str:
    """把小说腔的动作提示擦掉：`*尾巴摆了摆*`、`（抬眼）`、`（小声）`。

    只擦三种位置：星号包起来的动作、句首句尾的括号、句中含说话神态词的括号。
    句中正常的插入语（「明天（要是下雨）再说」）不动——宁可漏擦，不许把人家说的话啃掉一块。
    """
    cleaned = _STAR_STAGE.sub("", text or "")
    cleaned = _INLINE_STAGE.sub("", cleaned)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _HEAD_STAGE.sub("", cleaned)
        cleaned = _TAIL_STAGE.sub("", cleaned)
    return re.sub(r"[ 	]{2,}", " ", cleaned).strip()


# ---------------------------------------------------------------- 长句整段回复
# 「短促」管的是一条气泡的长度，不是内容的分量。要不要走长句，必须在**动笔之前**
# 就定下来，而不是等憋满五条短句才允许——所以让人格在开头挂一个声明标记。
# 标记只给网桥看，擦掉不发出去（和 `[表情: x]` 同一套道理）。
_LONG_FORM: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:〔\s*长句\s*〕|\[\[\s*长句\s*\]\]|\[\s*长句\s*\]|<\s*长句\s*>|/长句\s*)\s*",
)
# 判定「还没吐完」要拿这些完整标记比：只看开头符号会把 `〔长` 误判成已经定了
_LONG_FORM_MARKERS: Final[tuple[str, ...]] = ("〔长句〕", "[[长句]]", "[长句]", "<长句>", "/长句")
# 板块闭合 = 一个真正的空行。行尾单换行不算，否则「第二块\n\n第三块」会被当成第二块说完
_SECTION_END: Final[re.Pattern[str]] = re.compile(r"\n[ \t　]*\n[ \t　]*$")
_SECTION_MID: Final[re.Pattern[str]] = re.compile(r"\n[ \t　]*\n")


def is_long_form(text: str) -> bool:
    """开头挂了长句声明，就按整段处理。"""
    return bool(_LONG_FORM.match(text or ""))


def strip_long_form(text: str) -> str:
    return _LONG_FORM.sub("", text or "", count=1).strip()


def long_form_pending(text: str) -> bool:
    """流式还没法断定：现在这些字符仍可能是某个长句标记的前缀。"""
    head = (text or "").lstrip()
    if not head:
        return True
    return any(marker.startswith(head) for marker in _LONG_FORM_MARKERS)


def split_sections(
    text: str,
    *,
    hard_limit: int = _MAX_OUTBOUND_CHARS,
    ceiling: int = _MAX_OUTBOUND_PIECES,
) -> list[str]:
    """整段回复按**板块**切：空行是分界，板块内部不再拆句。

    长短句混排在这里是允许的——一个板块一句短话、下一个板块一整段，都可以。
    切板块只是为了把空间分开，不是为了把话说碎。只有单个板块超过 QQ 硬上限才硬切。
    """
    slots: list[str] = []
    guarded = _protect_atomic((text or "").strip(), slots)
    out: list[str] = []
    for block in (piece.strip() for piece in re.split(r"\n\s*\n", guarded) if piece.strip()):
        rest = re.sub(r"\n{2,}", "\n", block)
        while len(rest) > hard_limit:
            cut = _hard_cut(rest, hard_limit)
            out.append(cut)
            rest = rest[len(cut):].lstrip()
        if rest:
            out.extend(item for item in _restore_atomic(rest, slots) if item.strip())
    if ceiling > 0 and len(out) > ceiling:
        keep, tail = out[: ceiling - 1], out[ceiling - 1:]
        glued = tail[0] if tail else ""
        for piece in tail[1:]:
            while len(glued) + len(piece) + 2 > hard_limit:
                cut = _hard_cut(glued, hard_limit)
                keep.append(cut)
                glued = glued[len(cut):].lstrip()
            glued = f"{glued}\n\n{piece}" if glued else piece
        while len(glued) > hard_limit:
            cut = _hard_cut(glued, hard_limit)
            keep.append(cut)
            glued = glued[len(cut):].lstrip()
        if glued.strip():
            keep.append(glued)
        out = [piece for piece in keep if piece.strip()]
    return out


class BubbleStream:
    """把模型的增量流变成一条条气泡：**第一个句子一闭合就交出去**。

    等整段说完再发，是 QQ 上最伤的写法——模型侧 3 秒就吐出第一句，
    对方却要等到最后一条标点才看见任何东西。这里宁可多发一条，也不压着首句。

    三条保守规则：没闭合的括号/星号不发（免得把半截舞台提示甩出去）、
    短于 8 字的开头不发（「（抬」这种半截不配当一条气泡）、
    静默标记没判明前不发（否则她想说「[[静默]]」就收不回来了）。
    """

    _TERMINAL: Final[str] = "。！？!?…；;\n"

    def __init__(self, *, bubble_chars: int = 60, max_pieces: int = 4,
                 hard_limit: int = _MAX_OUTBOUND_CHARS, ceiling: int = _MAX_OUTBOUND_PIECES) -> None:
        self.bubble_chars = bubble_chars
        self.max_pieces = max_pieces
        self.hard_limit = hard_limit
        self.ceiling = ceiling
        self.raw = ""
        self.spoken: list[str] = []
        self.sent = 0
        # None = 还没判定要不要走长句；True/False 一旦定下就不回头
        self.long_form: bool | None = None

    @property
    def whole(self) -> str:
        return "".join(self.spoken) + self.raw

    def _decide_long_form(self) -> bool:
        """开头那个标记一到就判：是长句就整段走，不是就照旧一句一条。"""
        if self.long_form is not None:
            return self.long_form
        if is_long_form(self.raw):
            self.raw = strip_long_form(self.raw)
            self.long_form = True
        elif long_form_pending(self.raw):
            self.long_form = None
        else:
            self.long_form = False
        return self.long_form is True

    def _ready_sections(self) -> list[str]:
        """长句模式：攒到空行才算一个板块说完，板块内部不拆句。"""
        room = self.ceiling - self.sent
        if room <= 0:
            return []
        mid = _SECTION_MID.search(self.raw)
        boundary = mid.end() if mid else 0
        if not boundary:
            # 只有以空行收尾才算板块闭合；行尾一个换行不算，否则会把下一块提前发出去
            tail = _SECTION_END.search(self.raw)
            boundary = tail.start() if tail else 0
        if not boundary:
            return []
        head, self.raw = self.raw[:boundary], self.raw[boundary:].lstrip()
        pieces = split_sections(head, hard_limit=self.hard_limit, ceiling=room)
        if not pieces:
            return []
        self.sent += len(pieces)
        return pieces

    def _ready(self) -> list[str]:
        if self._decide_long_form():
            return self._ready_sections()
        # 切点一律在**原始流**上算：擦除会改变长度，拿擦后的下标去切原文就是吞字
        cut = max((self.raw.rfind(mark) + 1 for mark in self._TERMINAL if mark in self.raw), default=0)
        if not cut:
            return []
        head_raw, tail = self.raw[:cut], self.raw[cut:]
        for opener, closer in (("（", "）"), ("(", ")")):
            if head_raw.count(opener) > head_raw.count(closer):
                return []  # 半截括号：等它闭合再说
        if head_raw.count("*") % 2:
            return []
        head = strip_stage_directions(head_raw).strip()
        if len(head) < 8:
            return []
        # 只限条数、不设截断：装不下就合并加长，宁可一条长点也不丢字（ceiling 给足）
        room = self.ceiling - self.sent
        if room <= 0:
            return []
        pieces = split_bubbles(head, bubble_chars=self.bubble_chars, max_pieces=min(self.max_pieces, room),
                               hard_limit=self.hard_limit, ceiling=room * 4)
        self.raw = tail.lstrip()
        self.sent += len(pieces)
        return pieces

    def feed(self, delta: str) -> list[str]:
        self.raw += delta or ""
        return self._ready()

    def finish(self) -> list[str]:
        """收尾：尾巴再短也要发出去，一个字都不留。"""
        self._decide_long_form()
        head = strip_stage_directions(self.raw)
        self.raw = ""
        if not head.strip():
            return []
        room = max(1, self.ceiling - self.sent)
        if self.long_form:
            # 整段回复只剩最后一块：不再要求空行，直接把它当板块发完
            pieces = split_sections(head, hard_limit=self.hard_limit, ceiling=room)
        else:
            # 收尾必须把压着的尾巴全发出去：条数放开，字一个不丢
            pieces = split_bubbles(head.strip(), bubble_chars=self.bubble_chars,
                                   max_pieces=self.max_pieces, hard_limit=self.hard_limit,
                                   ceiling=room * 4)
        self.sent += len(pieces)
        return pieces


def split_bubbles(
    text: str,
    *,
    bubble_chars: int = 60,
    max_pieces: int = 4,
    hard_limit: int = _MAX_OUTBOUND_CHARS,
    ceiling: int = _MAX_OUTBOUND_PIECES,
) -> list[str]:
    """把一段回复切成人能连着发出去的几条短气泡，一个字都不丢。

    先看有没有 `〔长句〕` 声明：**有就整段走**（见 `split_sections`），不再一句一条。
    没有声明才按默认手感切——一句一条，先吐半句再补一句。只有句子多到装不进
    `max_pieces` 条时才合并。合并把每条拉长而不丢内容；单条超过硬上限再硬切。
    条数的天花板是 `ceiling`（企鹅嫌刷屏）。
    代码块与连号列表整块走：对方要复制的是能跑的东西，不是半截 ```。
    """
    if is_long_form(text):
        return split_sections(strip_long_form(text), hard_limit=hard_limit, ceiling=ceiling)

    slots: list[str] = []
    guarded = _protect_atomic(text, slots)
    units: list[str] = []
    for block in (piece.strip() for piece in guarded.strip().split("\n")):
        for piece in (unit.strip() for unit in _SENTENCE_END.split(block) if unit.strip()):
            while len(piece) > hard_limit:
                cut = _hard_cut(piece, hard_limit)
                units.append(cut)
                piece = piece[len(cut) :].lstrip()
            if piece:
                units.append(piece)
    if len(units) <= 1:
        return [item for unit in units for item in _restore_atomic(unit, slots)] or []

    _TERMINAL = re.compile(r"[。！？…；;：:]")
    # 小残片（舞台提示、半截前缀）向前贴到它修饰的那句话上：`（顿住）还是别说了。`
    stitched: list[str] = []
    index = 0
    while index < len(units):
        piece = units[index]
        fragment = len(piece) < 12 and not _TERMINAL.search(piece)
        following = units[index + 1] if index + 1 < len(units) else ""
        if (fragment and following and "\x00" not in following
                and len(piece) + len(following) + 1 <= bubble_chars):
            stitched.append(piece + following)
            index += 2
            continue
        stitched.append(piece)
        index += 1

    # 句子多到装不进 max_pieces 条：每次并掉相邻最短的一对（原子块不参与，粘一起就没法复制）
    merged = stitched
    while len(merged) > max_pieces:
        candidates = [i for i in range(len(merged) - 1)
                      if "\x00" not in merged[i] and "\x00" not in merged[i + 1]]
        if not candidates:
            break
        pair = min(candidates, key=lambda i: len(merged[i]) + len(merged[i + 1]))
        merged = merged[:pair] + [merged[pair] + merged[pair + 1]] + merged[pair + 2 :]

    # 合并可能把单条撑过硬上限：再切一次，字一个不丢
    out: list[str] = []
    for piece in merged:
        if "\x00" in piece:
            out.extend(item.strip() for item in _restore_atomic(piece, slots) if item.strip())
            continue
        rest = piece.strip()
        while len(rest) > hard_limit:
            cut = _hard_cut(rest, hard_limit)
            out.append(cut)
            rest = rest[len(cut) :].lstrip()
        if rest:
            out.append(rest)
    # 条数封顶只是「别刷屏」，不是丢掉内容的理由：超出的先并到前面几条里去，
    # 只有原子块（代码/列表）拦着实在并不动时，才宁可多发几条——少发一条就是丢字
    if ceiling > 0 and len(out) > ceiling:
        keep, tail = out[: ceiling - 1], out[ceiling - 1:]
        merged_tail = "\n".join(tail)
        while len(merged_tail) > hard_limit:
            cut = _hard_cut(merged_tail, hard_limit)
            keep.append(cut)
            merged_tail = merged_tail[len(cut):].lstrip("\n").lstrip()
        if merged_tail.strip():
            keep.append(merged_tail)
        out = [piece for piece in keep if piece.strip()]

    return out


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
        # 各家协议端的「正在输入」不一个叫法：试过哪一种认，就只发那一种
        self.typing_actions: dict[str, str] = {}  # 按「私聊/群聊」各记一个认的叫法：NapCat 那条只管单聊
        self.typing_supported = True  # 两种都不认就关掉试探，别每 18 秒白敲两次门

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

    @property
    def _debounce(self) -> float:
        return float(self._settings.onebot_debounce_seconds)

    def __init__(self, settings: Settings, bot: MySoulBot) -> None:
        self._settings = settings
        self._bot = bot
        self._server: asyncio.Server | None = None
        self._connections: set[_Connection] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._opened: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        # 每个空间「上一句什么时候到的」：只用来判这一句是不是新开的话头，攒够一批就得裁掉旧的
        self._last_seen: OrderedDict[str, float] = OrderedDict()
        self._pulses: dict[str, list[float]] = {}
        emoji_root = Path(settings.emoji_dir)
        if not emoji_root.is_absolute():
            emoji_root = PROJECT_ROOT / emoji_root
        self._stickers = StickerBook(emoji_root)
        self._buckets: dict[str, _Batch] = {}
        self._queues: dict[str, list[tuple[Inbound, bool]]] = {}
        self._typing_tasks: set[asyncio.Task[None]] = set()
        self._voice_tasks: set[asyncio.Task[None]] = set()
        self._typing_warned = False
        self._typing_reject = ""  # 协议端最后一次怎么拒的「正在输入」：不写进日志就该看得见
        self._last_voice: dict[str, str] = {}  # 上一句用过的垫话/兜底，同类里不许连说两遍
        self._last_text: dict[str, str] = {}
        self._flush_tasks: set[asyncio.Task[None]] = set()
        # 延时读数分两头：qq_* 是「我们发出去到协议端回话」那一趟来回（网路腿），
        # 回合两头是「对面那句话到达 → 第一个气泡落地 / 最后一个气泡落地」（用户等的时长）。
        self._qq_latency: dict[str, _Sampler] = {}
        self._turn_latency: dict[str, _Sampler] = {
            "first_bubble": _Sampler(),
            "turn_total": _Sampler(),
        }
        self._counts = {
            "events": 0,
            "replies": 0,
            "ignored": 0,
            "not_woken": 0,
            "flood": 0,
            "busy": 0,
            "duplicate": 0,
            "errors": 0,
            "silenced": 0,
            "requests": 0,
            "approved": 0,
            "batches": 0,
            "held": 0,
            "starved": 0,
            "typing_ok": 0,
            "typing_miss": 0,
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
        # 开机下发一次就够：每次重连都覆盖一遍资料，会把人手改的东西冲掉
        self._profile_pushed = False
        # 配对台账：网桥只负责把激活语/回填码截走，开挑战仍然只能在命令行上
        self._pairing = PairingDesk(settings)

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
        for batch in list(self._buckets.values()):
            batch.cancel()
        self._buckets.clear()
        self._queues.clear()
        for task in list(self._typing_tasks):
            task.cancel()
        self._typing_tasks.clear()
        for task in list(self._voice_tasks):
            task.cancel()
        self._voice_tasks.clear()
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
        if post_type == "request":
            self._bump("requests")
            if self._settings.onebot_auto_approve_friend:
                connection.track(self._on_request(connection, payload))
                return
            logger.info(
                "收到%s申请（来自 %s）：自动通过没开，等人点头",
                "好友" if str(payload.get("request_type") or "") == "friend" else "其它",
                payload.get("user_id") or "?",
            )
            self._bump("ignored")
            return
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
        if self._settings.onebot_apply_profile_on_boot and not self._profile_pushed:
            self._profile_pushed = True
            connection.track(self.apply_profile(reason="boot"))
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

    async def _owner_capability_line(self, inbound: Inbound) -> str:
        """配对成功后告诉她自己：现在手上多了哪些活。用真实注册表数，不写死。"""
        try:
            registry = self._bot.registry(inbound.engine_user_id)
        except Exception:  # noqa: BLE001 - 自述失败不影响配对本身成立
            return ""
        names = [name for name in registry.names if name.startswith("qq_")]
        if not names:
            return ""
        return ("我现在能动的：" + "、".join(sorted(names))
                + f"。这些只对管理者开放，别人叫我也不使。")

    def _live_connection(self) -> _Connection | None:
        for connection in list(self._connections):
            if connection.alive:
                return connection
        return None

    # ------------------------------------------------------------ 账号操作口
    @property
    def qq_online(self) -> bool:
        return self._live_connection() is not None

    async def account_call(self, action: str, params: Mapping[str, Any],
                           *, timeout: float = _API_TIMEOUT) -> Any:
        """工具层调这条走 QQ 账号级动作。返回 (data, 错误说明)，错误说明为空表示成功。

        刻意不抛异常：工具拿到的是「这次没办成，原因是 X」，
        由它决定怎么说，而不是把栈掀到对话界面上。
        """
        connection = self._live_connection()
        if connection is None:
            return None, "QQ 那头现在没连着"
        answered, reply = await self._request(connection, action, dict(params), timeout=timeout)
        if not answered or reply is None:
            return None, f"协议端对 {action} 没应答"
        try:
            retcode = int(reply.get("retcode") or 0)
        except (TypeError, ValueError):
            retcode = 0
        status = str(reply.get("status") or "")
        if status == "failed" or retcode:
            self._bump("errors")
            return None, f"协议端回了 retcode={retcode} {str(reply.get('message') or status)[:100]}"
        return reply.get("data"), ""

    async def _profile_call(self, connection: _Connection, action: str, params: Mapping[str, Any]) -> str:
        """资料类动作的专用调用：成功时 `data` 也是空的，只能看 status/retcode。

        所以不走 `_call`——它把 `data` 原样吐出来，成功和失败都是 None，分不出来。
        """
        answered, reply = await self._request(connection, action, params, timeout=_PROFILE_TIMEOUT)
        if not answered or reply is None:
            self._bump("errors")
            return "协议端没应答"
        try:
            retcode = int(reply.get("retcode") or 0)
        except (TypeError, ValueError):
            retcode = 0
        status = str(reply.get("status") or "")
        if status == "failed" or retcode:
            self._bump("errors")
            return f"retcode={retcode} {str(reply.get('message') or status)[:80]}"
        return "ok"

    async def apply_profile(self, *, reason: str = "manual") -> dict[str, str]:
        """把配置里写好的昵称/头像/签名推到协议端，改的是 QQ 账号本体。

        默认不在开机自动跑（`onebot_apply_profile_on_boot=false`）：换脸换名是人在做的事，
        不该每次重连都悄悄覆盖一遍。
        返回 `{项: ok|失败原因}`，没配的那几项直接跳过，不发空值去把现有资料抹掉。
        """
        settings = self._settings
        avatar = (settings.onebot_avatar or "").strip()
        avatar_path: Path | None = None
        if avatar:
            avatar_path = Path(avatar)
            if not avatar_path.is_absolute():
                avatar_path = PROJECT_ROOT / avatar_path
        # 路径写错这件事不等连接：没连上也该当场报出来，不然要等到有人去查才发现白配了
        if avatar and avatar_path is not None and not avatar_path.is_file():
            return {"avatar": f"文件不存在：{avatar}"}
        connection = self._live_connection()
        if connection is None:
            return {"connection": "没有在线的协议端"}
        outcome: dict[str, str] = {}

        if avatar and avatar_path is not None:
            outcome["avatar"] = await self._profile_call(
                connection, "set_qq_avatar", {"file": avatar_path.resolve().as_posix()})

        nickname = (settings.onebot_nickname or "").strip()
        signature = (settings.onebot_signature or "").strip()
        params: dict[str, Any] = {}
        if nickname:
            params["nickname"] = nickname
        if signature:
            # NapCat 的字段叫 personal_note，不是 OneBot 惯用的 signature：
            # 传错名字会被 schema 丢掉，动作照样回 ok，等于静悄悄什么都没改
            params["personal_note"] = signature
        if params:
            outcome["profile"] = await self._profile_call(connection, "set_qq_profile", params)
            if nickname and outcome["profile"] == "ok" and nickname not in self.bot_names:
                self.bot_names = (*self.bot_names, nickname)
        # 回读一次坐实：这两个动作失败也会回 ok，只有 get_login_info 说得出真话
        verify = await self._call(connection, "get_login_info", {}, timeout=_LOGIN_TIMEOUT)
        reported = str(verify.get("nickname") or "").strip() if isinstance(verify, Mapping) else ""
        if nickname:
            outcome["nickname_readback"] = reported or "读不到"
            if reported != nickname:
                outcome["nickname_effective"] = "no"
                logger.warning("昵称下发后回读仍是「%s」（要设的是「%s」）——"
                               "QQ 对改昵称有冷却，动作回了 ok 也没落地", reported, nickname)
        logger.info("下发 QQ 资料（%s）：%s", reason, outcome)
        return outcome

    # ------------------------------------------------------------ 一条消息
    async def _on_message(self, connection: _Connection, event: Mapping[str, Any]) -> None:
        inbound = parse_inbound(event, bot_id=self.bot_id, bot_names=self.bot_names)
        if inbound is None:
            self._bump("ignored")
            return
        if self.bot_id and inbound.sender_id == self.bot_id:
            self._bump("ignored")
            return  # 她自己的话再喂给自己，就是无限自 talk
        if not (inbound.text or inbound.images or inbound.quoted):
            # 只甩一张转发的记录、只回了一句引用而没打字——那也是内容，不是空消息
            self._bump("ignored")
            return
        if self._is_duplicate(inbound.message_id):
            self._bump("duplicate")
            return
        # 配对挂起时，激活语和回填码从 QQ 对话里截走，直接回给控制台那条私聊：
        # 这是人在认领这台机器，不是她在跟人聊天。群聊一律不截——群里喊这句的人太多
        note = consume_pairing(self._pairing, inbound.text, source="qq_private",
                               qq=inbound.sender_id, group=inbound.is_group)
        if note is not None:
            self._bump("paired_handled")
            logger.info("配对握手（%s）：%s", inbound.sender_id, note.splitlines()[0])
            # 回执锁外放行：回填码就长在那个形状上，锁不认识「这是发给机器主人的回执」
            await self._send_bubble(connection, inbound, note, lock=False)
            if "配对完成" in note:
                # 认出来之后先打招呼，再报她能干什么——这是「我认出你了」的实测证据。
                # 话是现生成的，不是模板；生成不出来就随机取一句人话兜底。
                greet = await make_greeting(
                    short_ask(self._bot.ask_once, self._settings),
                    deadline=self._settings.pair_phrase_deadline_seconds,
                    settings=self._settings)
                await self._type_pause()
                await self._send_bubble(connection, inbound, greet)
                tools = await self._owner_capability_line(inbound)
                if tools:
                    await self._type_pause()
                    await self._send_bubble(connection, inbound, tools)
            return
        if not inbound.is_group:
            # 私聊这一句她必定要接：先把「正在输入」挂上。等防抖窗口走完再挂，
            # 对面就有整整三秒看到的是「已读不回」
            connection.track(self._signal_typing(connection, inbound))
        if self._debounce <= 0 or self._fresh_topic(inbound):
            # 新开的话头不等窗口：那三秒除了让对面看见「已读不回」，什么也没攒到
            await self._dispatch(connection, inbound)
            return
        self._hold(connection, inbound)

    def _fresh_topic(self, inbound: Inbound) -> bool:
        """距上一句隔得够久，这一句就不是连着发的那一串——立刻送进队列。

        同时把这一空间「上一句什么时候到的」记下来：攒批只该为正在连发的人存在。
        从没见过的号按「可能正在连发」办——第一条等一个窗口，换来连发不答半截话。
        """
        gap = float(self._settings.onebot_debounce_burst_gap)
        key = inbound.engine_user_id
        now = time.monotonic()
        previous = self._last_seen.get(key)
        self._last_seen[key] = now
        if len(self._last_seen) > _LAST_SEEN_MAX:
            self._last_seen.pop(next(iter(self._last_seen)))
        if previous is None or gap <= 0:
            return gap <= 0
        return now - previous > gap

    def _hold(self, connection: _Connection, inbound: Inbound) -> None:
        """这一句先攒着：对方还在连着发的时候开口，回的就是半截话。

        窗口里每来一条就重新计时；攒到条数上限或等过封顶时间就不再等——
        话痨群不能因为一直有人说话就永远不开口。
        """
        key = inbound.engine_user_id
        now = time.monotonic()
        batch = self._buckets.get(key)
        if batch is None:
            batch = _Batch(user_id=key, opened_at=now)
            self._buckets[key] = batch
        batch.items.append(inbound)
        batch.connection = connection
        self._bump("held")
        settings = self._settings
        if inbound.mentioned or inbound.woke_by_name:
            # 被点名不等窗口：人家 @ 你了，还憋 3 秒凑上下文，那就是「@ 了不回」
            self._drop_and_fire(batch)
            return
        cap_left = settings.onebot_debounce_cap_seconds - (now - batch.opened_at)
        if len(batch.items) >= settings.onebot_debounce_max_items or cap_left <= 0:
            self._drop_and_fire(batch)
            return
        batch.cancel()
        batch.timer = asyncio.get_running_loop().call_later(
            max(0.05, min(self._debounce, cap_left)), self._drop_and_fire, batch
        )

    def _drop_and_fire(self, batch: _Batch) -> None:
        """到点了：先把桶摘下来（新句子开新桶），再异步去答这一桶。"""
        batch.cancel()
        if self._buckets.get(batch.user_id) is batch:
            self._buckets.pop(batch.user_id, None)
        task = asyncio.ensure_future(self._flush(batch))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush(self, batch: _Batch) -> None:
        """一桶话说完了：合并 → 判要不要接 → 补引用原文 → 答。"""
        if not batch.items or batch.connection is None:
            return
        lock = self._locks.get(batch.user_id)
        waited = time.monotonic() - batch.opened_at
        if batch.retries < 64 and waited < _HOLD_MAX_WAIT and lock is not None and lock.locked():
            # 上一回合还在说：这一桶整体往后挪一个窗口，不当 busy 丢掉
            batch.retries += 1
            self._buckets[batch.user_id] = batch
            batch.timer = asyncio.get_running_loop().call_later(self._debounce, self._drop_and_fire, batch)
            return
        inbound = merge_inbound(batch.items)
        await self._dispatch(batch.connection, inbound)

    def _enqueue(self, user_id: str, inbound: Inbound, discretion: bool) -> None:
        """挤不进当前轮的，攒到下一轮开头一起说；只留最近几条，免得积压变复读机。"""
        queue = self._queues.setdefault(user_id, [])
        queue.append((inbound, discretion))
        if len(queue) > _QUEUE_MAX:
            del queue[: len(queue) - _QUEUE_MAX]

    async def _dispatch_queued(self, connection: _Connection, queued: list[tuple[Inbound, bool]]) -> None:
        """刚说完一轮：把排队那些并成一句补上，唤醒口径按整批算。"""
        items = [inbound for inbound, _ in queued]
        discretion = any(flag for _, flag in queued)
        inbound = merge_inbound(items) if len(items) > 1 else items[0]
        await self._answer(connection, await self._enrich(connection, inbound), discretion)

    async def _dispatch(self, connection: _Connection, inbound: Inbound) -> None:
        wake, discretion = decide_wake(
            inbound,
            always_reply=self._settings.onebot_group_always_reply,
            discretion=self._settings.onebot_group_discretion,
        )
        if not wake:
            self._bump("not_woken")
            return
        if self._too_many(inbound.engine_user_id):
            self._bump("flood")
            logger.info("%s 这一分钟说得太密，这一句先不接", inbound.engine_user_id)
            return
        self._bump("batches")
        inbound = await self._enrich(connection, inbound)
        await self._answer(connection, inbound, discretion)

    @staticmethod
    def _nodes_of(data: Any) -> list[Any]:  # noqa: ANN401 - 协议端回的结构各家不同，取到列表为止
        """NapCat 的 `get_forward_msg` / `get_group_msg_history` 都回 `{messages: []}`。

        以前只认 `message`（单数）与 `nodes`，于是接口明明把记录给回来了也当没取到。
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, Mapping):
            return []
        for key in ("messages", "message", "nodes", "node_list", "data"):
            value = data.get(key)
            if isinstance(value, list) and value:
                return value
        return []

    async def _forward_nodes(self, connection: _Connection, inbound: Inbound,
                             quoted: str) -> Any:  # noqa: ANN401 - 协议端节点结构，取不到就 None
        """合并转发的取法各家不一样：NapCat 认 message_id，群里的还要带群号，LLOneBot 认 id。

        以前只发第一种写法，于是转进来的记录长期是一句「没取回来」——她只能回一句
        「这条是空的」，看着就像敷衍。三种都试一次，能捞回来就捞。
        """
        attempts: list[tuple[str, dict[str, Any]]] = [
            ("get_forward_msg", {"message_id": quoted})]
        if inbound.is_group:
            attempts.append(("get_forward_msg", {"group_id": inbound.target_id, "message_id": quoted}))
        attempts.append(("get_forward_msg", {"id": quoted}))
        for action, params in attempts:
            data = await self._call(connection, action, params,
                                    timeout=_QUOTE_TIMEOUT, best_effort=True)
            nodes = self._nodes_of(data)
            if nodes:
                logger.info("转发内容用 %s %s 取回来了（%d 条）", action, list(params), len(nodes))
                return nodes
        return None

    async def _quoted_message(self, connection: _Connection, inbound: Inbound,
                              quoted: str) -> Mapping[str, Any] | None:
        """被引用的那一条：先 get_msg(message_id)，群里再退到按序列号查历史。

        NapCat 的引用段可能只给 `seq`（消息序列号），`get_msg` 不一定认它——
        这时候只有 `get_group_msg_history` 能从群里把那一条捞回来。
        """
        data = await self._call(connection, "get_msg", {"message_id": _as_id(quoted) or quoted},
                                timeout=_QUOTE_TIMEOUT, best_effort=True)
        if isinstance(data, Mapping) and (data.get("message") or data.get("raw_message")):
            return data
        if inbound.is_group:
            history = await self._call(
                connection, "get_group_msg_history",
                {"group_id": inbound.target_id, "message_seq": _as_id(quoted) or quoted, "count": 1},
                timeout=_QUOTE_TIMEOUT, best_effort=True)
            rows = self._nodes_of(history)
            if rows and isinstance(rows[0], Mapping):
                logger.info("引用 #%s 用群历史取回来了", quoted)
                return rows[0]
        return None

    async def _enrich(self, connection: _Connection, inbound: Inbound) -> Inbound:
        """协议端只给 id 的引用与合并转发，补一次接口把原文取回来。

        取不回来不拦回复：她至少知道「有人回了某条消息」「有人转了一屏记录」这个动作，
        比把整段媒介当成空话丢掉强。
        """
        if not inbound.quotes or not connection.alive:
            return inbound
        adds: list[str] = []
        for kind, quoted in inbound.quotes:
            if kind == "forward":
                nodes = await self._forward_nodes(connection, inbound, quoted)
                if nodes:
                    adds.append(describe_forward(nodes))
                else:
                    # 不许写成「没取回来 #12345」：那是把接口故障端到 her 嘴边，
                    # 她就真的去吐槽系统，回的话比人的话更像机器人
                    adds.append("[有人转来一屏聊天记录，内容没跟着露出来]")
                continue
            data = await self._quoted_message(connection, inbound, quoted)
            if isinstance(data, Mapping):
                plain, codes = _segments_of(data)
                sender = data.get("sender")
                name = ""
                if isinstance(sender, Mapping):
                    name = str(sender.get("card") or sender.get("nickname") or "").strip()
                else:
                    name = str(data.get("sender_name") or "").strip()
                body = _short(plain.strip() or " ".join(describe_code(code) for code in codes))
                adds.append(f'[回复 @{name or "某人"}: "{body}"]' if body
                            else '[有人引用了一条消息，那条的内容没跟着露出来]')
            else:
                # 不许写成「没取回来 #12345」：那是把接口故障端到她嘴边，
                # 她就真的去吐槽系统，回的话比人的话更像机器人
                logger.debug("引用 #%s 的内容协议端没给回来", quoted)
                adds.append('[有人引用了一条消息，那条的内容没跟着露出来]')
        text = inbound.text
        for line in adds:
            if line not in text:
                text = f"{line}\n{text}".strip()
        return replace(inbound, text=text)

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
        window = self._settings.onebot_flood_window
        limit = self._settings.onebot_flood_limit
        stamps = [stamp for stamp in self._pulses.get(user_id, []) if now - stamp < window]
        if len(stamps) >= limit:
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

    @staticmethod
    def _is_silence(text: str) -> bool:
        """自主裁决那一档允许她交回一个静默标记；交回别的什么都算说了话。"""
        return text.strip() in _SILENCE_MARKS

    async def _on_request(self, connection: _Connection, event: Mapping[str, Any]) -> None:
        """好友申请：开关开了才代主人点头，且验证语一个词都不含时仍不通过。"""
        if str(event.get("request_type") or "").strip().lower() != "friend":
            self._bump("ignored")
            return
        flag = str(event.get("flag") or "").strip()
        seeker = _as_id(event.get("user_id"))
        comment = str(event.get("comment") or "").strip()
        wanted = [word.strip() for word in self._settings.onebot_friend_verify_words.split(",") if word.strip()]
        if wanted and not any(word in comment for word in wanted):
            self._bump("ignored")
            logger.info("好友申请（%s）验证语没过：不给过", seeker or "?")
            return
        if not flag:
            self._bump("errors")
            logger.warning("好友申请没带 flag，没法通过（%s）", seeker or "?")
            return
        await self._call(connection, "set_friend_add_request", {"flag": flag, "approve": True})
        self._bump("approved")
        logger.info("自动通过了好友申请（%s）", seeker or "?")
        greeting = self._settings.onebot_friend_greeting.strip()
        if greeting and seeker:
            await self._call(
                connection,
                "send_private_msg",
                {"user_id": seeker, "message": [{"type": "text", "data": {"text": greeting}}]},
            )

    async def _answer(self, connection: _Connection, inbound: Inbound, discretion: bool = False) -> None:
        user_id = inbound.engine_user_id
        if not await self._ensure(user_id):
            return
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            # 上一句还在说：绝不丢掉这一句。busy 只作读数，消息进队列，本轮一发完就补答——
            # 被 @ 的那一句尤其不能变成「稍后再说」，那就是他们说的「@ 了也不理人」
            self._bump("busy")
            self._enqueue(user_id, inbound, discretion)
            return
        try:
            async with lock:
                self.active_turns += 1
                said = ""
                # 计时锚在这一句到达的那一刻：防抖窗口、上一回合排队都算用户等的时长
                waited = inbound.received_at
                try:
                    typing = self._keep_typing(connection, inbound)
                    spoken_any = False
                    try:
                        async for bubble in self._stream_bubbles(user_id, inbound, discretion):
                            if await self._send_bubble(connection, inbound, bubble):
                                if not spoken_any and waited:
                                    self._sample(self._turn_latency, "first_bubble",
                                                 (time.monotonic() - waited) * 1000)
                                spoken_any = True
                            # 她已经开口，「正在输入」就该停：还挂着就是自问自答的假象
                            self._stop_typing(typing)
                    finally:
                        self._stop_typing(typing)
                    if spoken_any and waited:
                        self._sample(self._turn_latency, "turn_total",
                                     (time.monotonic() - waited) * 1000)
                    said = self._last_text.get(user_id, "")
                    if spoken_any:
                        self._bump("replies")
                    said = self._last_text.get(user_id, "") or said
                finally:
                    self.active_turns -= 1
                if discretion and self._is_silence(said):
                    self._bump("silenced")
                    logger.info("%s 这一句她决定不插话", user_id)
                elif said.strip():
                    # 语音放到后台去合成：它动辄一两秒，攥着这把锁就等于
                    # 让下一句话排在她「念完」后面——那正是语音拖慢回复的方式
                    self._spawn(self._speak(connection, inbound, said))
        finally:
            # 补答必须在锁放开之后：这一轮不管是说了、静默了还是没说出来，排队的句子都得有人接
            queued = self._queues.pop(user_id, [])
            if queued:
                connection.track(self._dispatch_queued(connection, queued))

    async def _stream_bubbles(
        self, user_id: str, inbound: Inbound, discretion: bool
    ) -> AsyncIterator[str]:
        """边收边吐气泡。整段文字留在 stream.whole 里，供静默判定与语音兜底使用。"""
        stream = BubbleStream(
            bubble_chars=self._settings.onebot_bubble_chars,
            max_pieces=self._settings.onebot_bubble_max,
        )
        try:
            async for delta in self._bot.stream_reply(
                user_id,
                inbound.prompt_text,
                today=dt.date.today(),
                images=list(inbound.images),
                speakers=list(inbound.speakers),
                group_mode=inbound.is_group,
                # QQ 进来的每一句都不是缔造者本人：外界可以被我听进去，但不许改写锚点
                external_origin=True,
                group_discretion=discretion,
            ):
                if self._settings.trim_stock_closers:
                    delta = trim_stock_closer(delta)
                for bubble in stream.feed(delta):
                    stream.spoken.append(bubble)
                    yield bubble
        except BotError as exc:
            logger.info("QQ 这一回合没说完：%s", exc.message)
            if not stream.whole.strip():
                # 引擎的错词（「模型没有返回可见内容」这类）一个字都不许递到 QQ 上：
                # 那her 变成了报错播报员，而且把系统本体暴露给了陌生人
                for bubble in self._last_resort(inbound):
                    stream.spoken.append(bubble)
                    yield bubble
                return
        except Exception as exc:  # noqa: BLE001 - 意外故障的细节不递到 QQ 上
            logger.warning("QQ 回合故障: %s", exc, exc_info=True)
            if not stream.whole.strip():
                for bubble in self._last_resort(inbound):
                    stream.spoken.append(bubble)
                    yield bubble
                return
        whole = stream.whole
        if not discretion or not self._is_silence(whole):
            for bubble in stream.finish():
                stream.spoken.append(bubble)
                yield bubble
        self._last_text[user_id] = whole

    async def _type_pause(self) -> None:
        """两条气泡之间留一段人类打字的时间。上下限都设 0 就等于关掉。"""
        low = self._settings.onebot_bubble_delay_min
        high = max(low, self._settings.onebot_bubble_delay_max)
        if high > 0:
            await asyncio.sleep(uniform(low, high))

    def _last_resort(self, inbound: Inbound) -> list[str]:
        """重问三轮还是空手时的最后一手。**默认不发话**——拿现成的句子顶替回答最像机器。

        想让她认一句「没接住」，把 ONEBOT_FAIL_LINES 配上即可；留空就是安静着，
        只记一笔 starved 读数，失败看得见但不糊弄人。
        """
        self._bump("starved")
        logger.info("%s 这一回合重问三轮还是空手", inbound.engine_user_id)
        lines = [line.strip() for line in self._settings.onebot_fail_lines.split("|") if line.strip()]
        if not lines:
            return []
        index = (self._last_voice.get("fail", -1) + 1) % len(lines)
        self._last_voice["fail"] = index
        return [lines[index]]

    def _keep_typing(self, connection: _Connection, inbound: Inbound) -> asyncio.Task[None] | None:
        """等待期只有一件事是该做的：让对面看见她在打字，而不是先蹦一句「我在想」。

        「正在输入」在协议端是按 typing_interval 过期的，上游慢就得有人续着；
        续到她把第一个字说出口为止。真发不出字也不替她编一句应付话。
        """
        if not self._settings.onebot_set_typing or not connection.alive:
            return None
        task = asyncio.create_task(self._keep_typing_loop(connection, inbound))
        self._typing_tasks.add(task)
        task.add_done_callback(self._typing_tasks.discard)
        return task

    async def _keep_typing_loop(self, connection: _Connection, inbound: Inbound) -> None:
        period = max(1.0, self._settings.onebot_typing_interval * 0.6)
        while connection.alive:
            await self._signal_typing(connection, inbound)
            await asyncio.sleep(period)

    @staticmethod
    def _stop_typing(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    def _typing_call(self, action: str, inbound: Inbound) -> dict[str, Any]:
        """各家的「正在输入」不一个叫法：OneBot 标准是 set_typing，NapCat 是 set_input_status。

        只发 set_typing 的话，NapCat 一声不答——等待期看着就等于没反应。
        """
        ms = int(self._settings.onebot_typing_interval * 1000)
        if action == "set_input_status":
            # NapCat 这条只走单聊（它内部按 C2C 找 uid）：event_type=1 就是「正在输入」
            return {"user_id": inbound.target_id, "event_type": 1}
        if action == "set_input_state":
            return {"user_id": 0 if inbound.is_group else inbound.target_id,
                    "group_id": inbound.target_id if inbound.is_group else 0,
                    "input_time": ms}
        key = "group_id" if inbound.is_group else "user_id"
        return {key: inbound.target_id, "typing_interval": ms}

    async def _signal_typing(self, connection: _Connection, inbound: Inbound) -> bool:
        """挂上「正在输入」：等待期唯一该发的东西。

        第一次两种叫法都试，认了哪一种就记在这条连接上（各家协议端可能同时连着，不能共用一个答案）；
        一种都不认时记 miss——这一声是现在唯一的等待反馈，它有没有生效必须看得见。
        """
        if not self._settings.onebot_set_typing or not connection.alive or not connection.typing_supported:
            return False
        kind = "group" if inbound.is_group else "private"
        known = connection.typing_actions.get(kind)
        order = [known] if known else (
            ["set_typing", "set_input_status", "set_input_state"] if kind == "private"
            else ["set_typing", "set_input_state"])
        last = ""
        for action in order:
            answered, reply = await self._request(
                connection, action, self._typing_call(action, inbound), timeout=3.0
            )
            if answered and reply is not None and not int(reply.get("retcode") or 0):
                if known != action:
                    logger.info("%s 的%s认「%s」这个叫法，往后只发它",
                                connection.peer, "群聊" if kind == "group" else "单聊", action)
                connection.typing_actions[kind] = action
                self._bump("typing_ok")
                return True
            retcode = (reply or {}).get("retcode")
            note = str((reply or {}).get("message") or "").strip()[:70]
            last = f"{action} → " + ("协议端没答" if not answered else f"retcode={retcode} {note}")
        self._typing_reject = last
        connection.typing_supported = False
        if not self._typing_warned:
            self._typing_warned = True
            logger.warning("协议端两种「正在输入」都不认（最后一手：%s）：等待期只剩安静，不再重试到刷屏",
                           self._typing_reject or "没回话")
        self._bump("typing_miss")
        return False

    def _outbound_filter(self, piece: str, *, lock: bool = True) -> str:
        """出站统一闸口：擦舞台提示 → 洗 markdown → 缴械假 CQ 码 → **上锁**。

        锁放在这一层而不是只写在提示词里，是因为提示词能被绕、这道不能：
        她现在能向一群好友公开广播，「模型自觉不说不该说的」不再是足够保证。

        `lock=False` 只给配对握手用：那条回执里必须出现回填码，而锁把
        「三位-三位」这个形状整个吃掉——真实事故：手机上永远收不到码，
        日志里躺着一条 `block/pairing_code`。放行的是**这段话的来源**而不是内容：
        回执整句由 core/identity 拼出来，没有一个字来自模型。
        """
        cleaned = neutralize_cq(strip_stage_directions(piece)).strip()
        if lock and self._settings.secrecy_guard_enabled:
            guarded, findings = guard_secrecy(cleaned)
            if findings:
                self._bump("secrecy_hits")
                worst = max(findings, key=lambda f: list(Leak).index(f.action))
                logger.warning("出站内容被锁拦下（%s/%s）：%s",
                               worst.action.value, worst.rule, worst.matched[:70])
            cleaned = guarded
        return cleaned

    async def _send_bubble(self, connection: _Connection, inbound: Inbound, piece: str,
                           *, lock: bool = True) -> bool:
        """发一条气泡：先擦小说腔动作（红线），再洗掉 markdown，再缴械假 CQ 码，最后换表情图。"""
        cleaned = self._outbound_filter(piece, lock=lock).strip()
        if self._settings.reply_plain_text:
            cleaned = plain_text(cleaned)
        if self._settings.onebot_emoji_enabled:
            spoken, tags = StickerBook.extract(cleaned)
        else:
            spoken, tags = cleaned, []
        sent = False
        if spoken:
            await self._call(
                connection,
                self._action(inbound),
                self._params(inbound, [{"type": "text", "data": {"text": spoken}}]),
            )
            sent = True
        for tag in tags:
            image = self._stickers.resolve(tag)
            if image is None:
                logger.info("表情标签 %s 在 %s 里没有对应图，这一张不发", tag, self._stickers.directory)
                continue
            await self._type_pause()
            await self._call(
                connection,
                self._action(inbound),
                self._params(inbound, [{"type": "image", "data": {"file": f"file://{image}"}}]),
            )
            sent = True
        return sent

    async def _deliver(self, connection: _Connection, inbound: Inbound, text: str) -> None:
        """整段投递：非流式调用方走这条，与流式共用同一套发气泡规则。"""
        cleaned = self._outbound_filter(text)
        if self._settings.onebot_bubble_enabled:
            pieces = split_bubbles(
                cleaned,
                bubble_chars=self._settings.onebot_bubble_chars,
                max_pieces=self._settings.onebot_bubble_max,
            )
        else:
            pieces = split_outbound(cleaned)
        for index, piece in enumerate(pieces):
            if index:
                await self._type_pause()
            if self._settings.onebot_emoji_enabled:
                spoken, tags = StickerBook.extract(piece)
            else:
                spoken, tags = piece, []
            if spoken:
                await self._call(
                    connection,
                    self._action(inbound),
                    self._params(inbound, [{"type": "text", "data": {"text": spoken}}]),
                )
            for tag in tags:
                image = self._stickers.resolve(tag)
                if image is None:
                    logger.info("表情标签 %s 在 %s 里没有对应图，这一张不发", tag, self._stickers.directory)
                    continue
                if spoken or len(pieces) > 1:
                    await self._type_pause()
                await self._call(
                    connection,
                    self._action(inbound),
                    self._params(inbound, [{"type": "image", "data": {"file": f"file://{image}"}}]),
                )

    async def _speak(self, connection: _Connection, inbound: Inbound, text: str) -> None:
        """说完顺手念一遍。

        群聊默认不出声：语音比文字慢，连着甩几条语音是骚扰，不是拟人。
        但**对方明确要语音**（「发条语音听听」「念一遍」）那一句例外——
        那是他点的东西，装作没听见才最像机器。
        """
        if not self._settings.tts_enabled or provider_of(self._settings) == "none":
            return
        if inbound.is_group and not self._settings.onebot_auto_record_groups:
            if not inbound.voice_requested:
                return
        if not self._settings.onebot_auto_record and not inbound.voice_requested:
            return
        try:
            clip = await synthesize(
                strip_stage_directions(text), self._settings, self._bot.storage, inbound.engine_user_id
            )
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

    async def _request(
        self, connection: _Connection, action: str, params: Mapping[str, Any], *,
        timeout: float = _API_TIMEOUT,
    ) -> tuple[bool, dict[str, Any] | None]:
        """发一个动作、等它回话，只报「有没有答」与原样回执。成败判定留给调用方。"""
        if not connection.alive:
            return False, None
        echo = connection.new_echo()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        connection._pending[echo] = future  # noqa: SLF001 - 同模块内的连接簿记
        started = time.perf_counter()
        try:
            await connection.send_json({"action": action, "params": dict(params), "echo": echo})
        except _ConnectionClosed:
            connection._pending.pop(echo, None)
            return False, None
        try:
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._bump("qq_slow")
            return False, None
        except asyncio.CancelledError:
            connection._pending.pop(echo, None)
            raise
        finally:
            connection._pending.pop(echo, None)
        self._sample(self._qq_latency, action, (time.perf_counter() - started) * 1000)
        return True, reply

    async def _call(
        self,
        connection: _Connection,
        action: str,
        params: Mapping[str, Any],
        *,
        timeout: float = _API_TIMEOUT,
        best_effort: bool = False,
    ) -> Any | None:  # noqa: ANN401 - 协议端给什么 data 就原样转出去
        """`best_effort`：只为「挂个输入状态」这类提示服务。协议端不认就算没发生过，
        不许把它记进 errors——不然每个回合都白挨一笔，读数就成了噪音。"""

        def miss() -> None:
            if not best_effort:
                self._bump("errors")

        answered, reply = await self._request(connection, action, params, timeout=timeout)
        if not answered or reply is None:
            miss()
            if not best_effort and not answered:
                logger.warning("协议端对 %s 没在 %ss 内应答", action, timeout)
            return None
        try:
            retcode = int(reply.get("retcode") or 0)
        except (TypeError, ValueError):
            retcode = 0
        status = str(reply.get("status") or "")
        if status == "failed" or retcode:
            miss()
            if not best_effort:
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
    def _spawn(self, coro: Any) -> None:  # noqa: ANN401 - 一个协程，交给后台
        """把不急着要的活儿丢到后台，别让当前这把锁等它。"""
        task = asyncio.ensure_future(coro)
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_tasks.discard)

    def _bump(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    def _sample(self, store: dict[str, _Sampler], group: str, ms: float) -> None:
        store.setdefault(group, _Sampler()).add(round(ms, 1))

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
            "typing_reject": self._typing_reject,
            "debounce_seconds": round(self._debounce, 2),
            "held_spaces": len(self._buckets),
            "bubble_max": self._settings.onebot_bubble_max,
            "active_turns": self.active_turns,
            "counts": dict(self._counts),
            # 网路腿（每个动作一趟来回）与用户腿（到达→气泡落地）分开摆：
            # 上游慢还是 QQ 慢，看这两组数就能分开赖账
            "qq_latency_ms": {name: s.view() for name, s in self._qq_latency.items()},
            "turn_latency_ms": {name: s.view() for name, s in self._turn_latency.items()},
        }
