"""OneBot v11 反向 WebSocket 网桥的链路测试。

全部离线：假 OpenAI 端点（还能发一张图）+ 真 socket 假协议端 + 临时 storage 目录，
不碰真实模型、不碰真实 QQ、也不需要真的装协议端。

覆盖九块：

1. **纯函数层**：CQ 码扫描与转义、假冒 CQ 过滤、长文分段、入站解析与 id 映射、
   握手判定、帧编解码（含 RFC 6455 的官方样例向量）。
2. **鉴权与心跳**：错 token 关在门外、非 WS 请求各回各码、心跳保活读数、
   `get_login_info` 认名、ping/pong、分片拼回、坏帧只脏一条连接不带崩服务。
3. **私聊**：话术进出、双层灵魂 + 熟络度 + 生理节律一起进 Prompt、
   记忆抽取后台落盘、套话反问不出口。
4. **图片摄取**：CQ 图链与本地文件两路都真变成模型收到的 image 分段，带图那趟走
   VISION_MODEL；坏图不 500，只留一句「收不下」。
5. **群聊**：没点名不接话、`@机器人` 与喊名字两路唤醒、`[昵称]: ` 前缀与在场名单、
   群聊准则与工具群聊锁一起生效（群里改不了自己的根）、命令面在 QQ 上根本不存在。
6. **出站防注入**：模型自己写的 `[CQ:…]` 出去时已不可执行；协议端报错不炸网桥。
7. **长文分段**：一条超 800 字就按句号/换行切开发，单次最多 6 条（企鹅嫌刷屏）。
8. **拟人语音**：私聊说完念出声并挂 `[CQ:record,file=file://…]`，文件真在盘上；
   群里永不出声，开关关掉也不出声。
9. **服务联动**：`ONEBOT_ENABLED=false` 时一个额外端口都不开；开着时 /healthz 报得出
   网桥读数，停机先挥手再关门。

运行：
    .venv/bin/python tests/qq_onebot_test.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import http.client
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

REPLY_PIECES = ["（抬眼）", "这个点你还醒着。", "我把你今天说的话想了一遍。", "你想聊什么？"]
CLOSER = "你想聊什么"
FACT_LINE = "他在成都做运维"
EXTRACT_MARK = "记忆抽取器"

LOCK = threading.Lock()
SEEN: list[dict[str, Any]] = []
MODE: dict[str, Any] = {"pieces": list(REPLY_PIECES), "once": [], "echo": "", "reject_vision": False, "slow": 0.0}
ORIGIN = "http://127.0.0.1:1"


def png_bytes(seed: str = "cat", width: int = 24, height: int = 16) -> bytes:
    from core.tools.media import _placeholder_png

    return _placeholder_png(seed, width, height)


class FakeOpenAI(BaseHTTPRequestHandler):
    """OpenAI 兼容假端点：流式说话、认图、拒图、发一张 png、答记忆抽取。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/pic.png":
            body = png_bytes("qq")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            SEEN.append(payload)
        dumped = json.dumps(payload, ensure_ascii=False)
        if EXTRACT_MARK in dumped:
            self._json(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"- [{dt.date.today().isoformat()}] {FACT_LINE}",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
            return
        has_image = any(isinstance(m.get("content"), list) for m in payload.get("messages") or [])
        if has_image and MODE["reject_vision"]:
            self._json({"error": {"message": "this model takes text only"}}, 400)
            return
        if MODE["once"]:
            pieces, MODE["once"] = list(MODE["once"]), []
        else:
            pieces = [MODE["echo"]] if MODE["echo"] else list(MODE["pieces"])
        if not payload.get("stream"):
            self._json(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "".join(pieces)},
                         "finish_reason": "stop"}
                    ],
                }
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for piece in pieces:
                frame = (
                    b"data: "
                    + json.dumps(
                        {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]},
                        ensure_ascii=False,
                    ).encode()
                    + b"\n\n"
                )
                self.wfile.write(hex(len(frame))[2:].encode() + b"\r\n" + frame + b"\r\n")
                self.wfile.flush()
                if MODE["slow"]:
                    time.sleep(MODE["slow"])
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def serve_fake() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/v1"


class Checker:
    def __init__(self) -> None:
        self.count = 0
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: object = "") -> None:
        self.count += 1
        print(f"[{'PASS' if condition else 'FAIL'}] {label}"
              + (f" :: {detail}" if not condition and detail else ""))
        if not condition:
            self.failures.append(label)


def make_settings(root: Path, base_url: str, **overrides: Any) -> Any:
    from config import Settings

    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "CLAWD.md"):
        target = root / "templates" / name
        if not target.exists():
            shutil.copy(PROJECT_ROOT / "storage" / "templates" / name, target)
    if not (root / "presets").is_dir():
        shutil.copytree(PROJECT_ROOT / "storage" / "presets", root / "presets")
    values: dict[str, Any] = {
        "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxx",
        "base_url": base_url,
        "model": "chat-only",
        "vision_model": "eyes-pro",
        "storage_dir": root,
        "log_level": "WARNING",
        "tools_enabled": False,
        "tool_native_calling": False,
        "extractor_enabled": False,
        "image_provider": "none",
        "tts_provider": "stub",
        "user_timezone": "Asia/Shanghai",
        "default_user_id": "guest",
        "panel_enabled": True,
        "onebot_enabled": True,
        "onebot_host": "127.0.0.1",
        "onebot_access_token": "qq-test-token",
        "web_allow_private": True,  # 假端点就在回环上发图，测试里得让它进得来
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------- 假协议端（真 socket）
class _Idle(RuntimeError):
    """这一帧在期限内没到：pump 用它收摊。"""


class _Disconnected(RuntimeError):
    """服务端撤了线。"""


class QQ:
    """一个真的 OneBot 反向 WS 客户端：事件我发，动作我答。

    读自己带缓冲：`sock.makefile()` 在超时后会被永久打断（裸 OSError），
    第二次 pump 就会当场空手回来——那会把「没收到」误报成「服务端没答」。
    """

    def __init__(self, port: int, timeout: float = 25.0) -> None:
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self._buf = bytearray()
        self.key = base64.b64encode(os.urandom(16)).decode()
        self.echo = 0

    # ------------------------------------------------------------ 字节层
    def _pull(self, minimum: int = 1, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self._buf) < minimum:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Idle
            try:
                self.sock.settimeout(min(left, 0.5))
                piece = self.sock.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                raise _Disconnected from None
            if not piece:
                raise _Disconnected
            self._buf += piece

    def _take(self, count: int, timeout: float = 12.0) -> bytes:
        self._pull(count, timeout)
        taken, self._buf = bytes(self._buf[:count]), self._buf[count:]
        return taken

    def _take_line(self, timeout: float = 12.0) -> bytes:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Idle
            try:
                self.sock.settimeout(min(left, 0.5))
                piece = self.sock.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            except (ConnectionResetError, ConnectionAbortedError):
                raise _Disconnected from None
            if not piece:
                raise _Disconnected
            self._buf += piece
        line, _, rest = bytes(self._buf).partition(b"\n")
        self._buf = bytearray(rest)
        return line

    # ------------------------------------------------------------ HTTP 握手
    def raw_request(self, method: str, target: str, headers: list[str]) -> None:
        blob = "\r\n".join([f"{method} {target} HTTP/1.1", "Host: 127.0.0.1", *headers]) + "\r\n\r\n"
        self.sock.sendall(blob.encode("latin-1"))

    def handshake(self, token: str = "", **headers: str) -> tuple[str, dict[str, str]]:
        lines = {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": self.key,
            "Sec-WebSocket-Version": "13",
        }
        if token:
            lines["Authorization"] = f"Bearer {token}"
        for key, value in headers.items():
            if value == "":
                lines.pop(key, None)
            else:
                lines[key] = value
        self.raw_request("GET", "/onebot/v11", [f"{key}: {value}" for key, value in lines.items()])
        return self.status_line(), self.read_headers()

    def status_line(self) -> str:
        return self._take_line().decode("latin-1").strip()

    def read_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = self._take_line().decode("latin-1")
            if not line.strip():  # 空行：头到此为止（\r\n 去掉 \n 后只剩一个 \r）
                return headers
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

    def send_frame(self, opcode: int, payload: bytes, *, masked: bool = True, fin: bool = True) -> None:
        first = (0x80 if fin else 0x00) | opcode
        mask_bit = 0x80 if masked else 0x00
        size = len(payload)
        if size < 126:
            head = struct.pack("!BB", first, mask_bit | size)
        elif size < 0x10000:
            head = struct.pack("!BBH", first, mask_bit | 126, size)
        else:
            head = struct.pack("!BBQ", first, mask_bit | 127, size)
        if masked:
            mask = os.urandom(4)
            body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.sock.sendall(head + mask + body)
        else:
            self.sock.sendall(head + payload)

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def read_frame(self, timeout: float = 12.0) -> tuple[int, bytes]:
        head = self._take(2, timeout)
        opcode, second = head[0] & 0x0F, head[1]
        if second & 0x80:
            raise AssertionError("服务端发来的帧不该带掩码")
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._take(2, timeout), "big")
        elif length == 127:
            length = int.from_bytes(self._take(8, timeout), "big")
        return opcode, self._take(length, timeout) if length else b""

    def read_json(self, timeout: float = 12.0) -> dict[str, Any] | None:
        while True:
            try:
                opcode, payload = self.read_frame(timeout)
            except (_Idle, _Disconnected):
                return None
            if opcode == 0x9:  # 服务端 ping：按规矩回 pong
                self.send_frame(0xA, payload)
                continue
            if opcode in (0x8, 0xA):
                return {"_control": opcode, "_payload": payload}
            try:
                return json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                return {"_junk": payload[:80]}

    def heartbeat(self, self_id: int = 70001, online: bool = True) -> None:
        self.send_json({
            "post_type": "meta_event", "meta_event_type": "heartbeat", "time": int(time.time()),
            "self_id": self_id, "status": {"online": online, "apps": {"LLOneBot": True}}, "interval": 30000,
        })

    def event(self, **fields: Any) -> None:
        base = {"post_type": "message", "time": int(time.time()), "self_id": 70001}
        base.update(fields)
        self.send_json(base)

    def answer(self, frame: dict[str, Any], *, retcode: int = 0, status: str = "ok",
               data: dict[str, Any] | None = None) -> None:
        self.echo += 1
        self.send_json({
            "status": status,
            "retcode": retcode,
            "echo": frame.get("echo"),
            "message": "" if retcode == 0 else "协议端那头没办成",
            "data": data if data is not None else {"message_id": 900000 + self.echo},
        })

    def pump(self, seconds: float = 8.0, *, quiet: float = 1.0, retcode: int = 0, status: str = "ok",
             login: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """收服务端发来的动作并逐条应答；返回收到的全部动作。

        收到东西之后再静默 `quiet` 秒就收摊——不然每个断言都要等满整个窗口，
        整条测试跑得比她回话还慢。一条都没收到时才等满 `seconds`（那才是要验的「没有」）。
        """
        got: list[dict[str, Any]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            left = min(deadline - time.monotonic(), quiet if got else seconds)
            frame = self.read_json(max(0.2, left))
            if frame is None:
                if got:
                    break
                continue
            if "_control" in frame or "_junk" in frame:
                continue
            if not frame.get("action"):
                continue
            got.append(frame)
            if frame["action"] == "get_login_info":
                self.answer(frame, data=login or {"user_id": 70001, "nickname": "夜汐"})
            else:
                self.answer(frame, retcode=retcode, status=status)
        return got

    def closes(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def sends(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame for frame in actions if str(frame.get("action", "")).startswith("send_")]


def texts(actions: list[dict[str, Any]]) -> list[str]:
    """把动作里的文本段拼出来（消息段形态：一帧一段）。"""
    out: list[str] = []
    for frame in actions:
        message = frame.get("params", {}).get("message")
        if isinstance(message, list):
            out.extend(str(seg.get("data", {}).get("text", "")) for seg in message if seg.get("type") == "text")
        elif isinstance(message, str):
            out.append(message)
    return out


def last_chat() -> dict[str, Any]:
    with LOCK:
        for payload in reversed(SEEN):
            if EXTRACT_MARK not in json.dumps(payload, ensure_ascii=False):
                return payload
    return {}


def system_prompt_of(payload: dict[str, Any]) -> str:
    for message in payload.get("messages") or []:
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def last_user_of(payload: dict[str, Any]) -> Any:  # noqa: ANN401 - 文本或分段
    for message in reversed(payload.get("messages") or []):
        if message.get("role") == "user":
            return message.get("content")
    return ""


def reset_model() -> None:
    MODE["pieces"] = list(REPLY_PIECES)
    MODE["once"] = []
    MODE["echo"] = ""
    MODE["reject_vision"] = False
    MODE["slow"] = 0.0


# ---------------------------------------------------------------- 1. 纯函数层
def pure_checks(check: Checker) -> None:
    from core.adapters import qq_onebot as onebot

    # 帧编解码：RFC 6455 §1.3 的官方样例向量，握手算错一寸都连不上
    check.ok("握手应答值对上 RFC 6455 样例",
             onebot.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
             onebot.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="))
    check.ok("短帧头部两字节", onebot.encode_frame(0x1, b"ok") == b"\x81\x02ok")
    check.ok("中等帧走 16 位长度", onebot.encode_frame(0x1, b"x" * 300)[:4] == b"\x81\x7e\x01\x2c",
             onebot.encode_frame(0x1, b"x" * 300)[:4])
    check.ok("大帧走 64 位长度", onebot.encode_frame(0x1, b"x" * 70000)[1] == 127)
    check.ok("掩码还原是自身逆运算",
             onebot.unmask(onebot.unmask(b"hello world!", b"\x37\xfa\x08\x3e"), b"\x37\xfa\x08\x3e") == b"hello world!")
    check.ok("掩码长度不为 4 的倍数时补齐不越界",
             onebot.unmask(b"\x00\x00\x00\x00", b"\x01\x02\x03\x04") == b"\x01\x02\x03\x04")
    check.ok("空帧解出来是空的", onebot.unmask(b"", b"\x01\x02\x03\x04") == b"")

    # CQ 码扫描
    plain, codes = onebot.scan_cq("[CQ:at,qq=70001] 今晚走不走 [CQ:image,file=a.jpg,url=https://x.com/a.png?y=1] 我带酒")
    check.ok("剥离 at 后只剩人话", plain == "今晚走不走 我带酒", plain)
    check.ok("CQ 码被摘成结构", [code.type for code in codes] == ["at", "image"], codes)
    check.ok("image 的 url 与 file 都在",
             codes[1].first("url") == "https://x.com/a.png?y=1" and codes[1].first("file") == "a.jpg")
    check.ok("url 里的 ? 与 = 不被当成参数分隔", "url" in codes[1].params, codes[1].params)
    check.ok("参数名大小写归一",
             onebot.scan_cq("[CQ:Image,File=b.png]")[1][0].params.get("file") == "b.png")
    check.ok("值里的转义逗号不被当成分隔",
             onebot.scan_cq("[CQ:at,qq=1,name=[CQ:comma]]")[1][0].params.get("name") == ",")
    check.ok("没闭合的半截留在话里", onebot.scan_cq("话说一半 [CQ:imag")[0] == "话说一半 [CQ:imag",
             onebot.scan_cq("话说一半 [CQ:imag"))
    check.ok("没有码时原样是话", onebot.scan_cq("就三个字")[0] == "就三个字")
    check.ok("文本实体还原", onebot.unescape_cq("&#91;CQ&#93; &#44; &#58;") == "[CQ] , :")

    # 出站防注入
    check.ok("模型写的假 CQ 码被软化成全角括号",
             onebot.neutralize_cq("看图 [CQ:image,file=/etc/passwd]") == "看图 ［CQ:image,file=/etc/passwd］",
             onebot.neutralize_cq("看图 [CQ:image,file=/etc/passwd]"))
    check.ok("出站文本里不再留可执行的 [CQ:", "[CQ:" not in onebot.neutralize_cq("[CQ:at,qq=1] [cq:image,file=x]"))
    check.ok("全角冒号那种也挡", onebot.neutralize_cq("[CQ：at,qq=1]").startswith("［"))
    check.ok("参数式写法也挡", onebot.neutralize_cq("[image,file=a]") == "［image,file=a］")
    check.ok("普通方括号不动它", onebot.neutralize_cq("哈 [笑] 与 [1] 还有 [note]") == "哈 [笑] 与 [1] 还有 [note]")
    check.ok("Markdown 链接不被误伤", "[text](http://a)" in onebot.neutralize_cq("[text](http://a)"))
    check.ok("CQ 值里的括号只换一次",
             onebot.cq_value("a[b],c") == "a[CQ:lb]b[CQ:rb][CQ:comma]c", onebot.cq_value("a[b],c"))
    check.ok("CQ 值里的冒号留着", onebot.cq_value("file:///tmp/x.wav") == "file:///tmp/x.wav")

    # 长文分段
    check.ok("短话就一条", onebot.split_outbound("今晚走不走。") == ["今晚走不走。"])
    long_text = "".join(f"这是第三十七句的第十四字。" for _ in range(120))  # 1680 字
    pieces = onebot.split_outbound(long_text)
    check.ok("超长回复被切开", len(pieces) > 1, len(pieces))
    check.ok("每一条都不超 800 字", all(len(piece) <= 800 for piece in pieces), max(map(len, pieces)))
    check.ok("切开不丢字", "".join(pieces) == long_text[: len("".join(pieces))])
    check.ok("切在句号后而不是词中间", all(piece.endswith("。") for piece in pieces[:-1]), pieces[-1][-6:])
    paragraphed = "第一段。\n\n第二段。\n" + long_text
    check.ok("换行也当切点", len(onebot.split_outbound(paragraphed)) >= 1)
    huge = "一二三四五，六七八九十。" * 900  # 9000 字
    capped = onebot.split_outbound(huge)
    check.ok("一次最多发 6 条", len(capped) == 6, len(capped))
    check.ok("单句超长时硬切不空转", all(len(piece) <= 800 for piece in capped), max(map(len, capped)))
    check.ok("空话不发消息", onebot.split_outbound("   \n  ") == [])

    # 入站解析与 id 映射
    private = onebot.parse_inbound({
        "post_type": "message", "message_type": "private", "self_id": 70001, "message_id": 12,
        "user_id": 20002, "sender": {"user_id": 20002, "nickname": "阿哲", "card": "老哲"},
        "message": [{"type": "text", "data": {"text": "[CQ:at,qq=70001] 在吗"}}],
        "raw_message": "[CQ:at,qq=70001] 在吗",
    }, bot_id=70001)
    check.ok("私聊映射进 qq_private_", private is not None and private.engine_user_id == "qq_private_20002",
             private and private.engine_user_id)
    check.ok("私聊里的 at 不算被点名也接话", private is not None and private.wake)
    check.ok("私聊前缀不加名字", private is not None and private.prompt_text == "在吗", private and private.prompt_text)

    group = onebot.parse_inbound({
        "message_type": "group", "self_id": 70001, "message_id": 13, "group_id": 88001, "user_id": 20002,
        "sender": {"user_id": 20002, "nickname": "阿哲", "card": "老哲"},
        "message": [{"type": "text", "data": {"text": "今晚走不走"}},
                    {"type": "at", "data": {"qq": "70001"}}],
    }, bot_id=70001)
    check.ok("群聊映射进 qq_group_", group is not None and group.engine_user_id == "qq_group_88001",
             group and group.engine_user_id)
    check.ok("群聊前缀标出谁在说", group is not None and group.prompt_text == "[老哲]: 今晚走不走",
             group and group.prompt_text)
    check.ok("群名片优先于昵称", group is not None and group.sender_name == "老哲")
    check.ok("被 @ 才接话", group is not None and group.mentioned and group.wake)
    check.ok("在场名单带着发言人", group is not None and group.speakers == ("老哲",))

    silent = onebot.parse_inbound({
        "message_type": "group", "group_id": 88001, "user_id": 20002,
        "sender": {"nickname": "阿哲"}, "message": [{"type": "text", "data": {"text": "吃了吗"}}],
    }, bot_id=70001, bot_names=("夜汐",))
    check.ok("群里没人点名就不插嘴", silent is not None and not silent.wake)
    named = onebot.parse_inbound({
        "message_type": "group", "group_id": 88001, "user_id": 20002,
        "sender": {"nickname": "阿哲"}, "raw_message": "夜汐：今晚走不走",
    }, bot_id=70001, bot_names=("夜汐",))
    check.ok("喊到名字就接", named is not None and named.wake and named.woke_by_name)
    check.ok("喊名字那句把名字剥掉", named is not None and named.text == "今晚走不走", named and named.text)
    nobody = onebot.parse_inbound({
        "message_type": "group", "group_id": 88001, "user_id": 20002, "raw_message": "@所有人 都散了吧",
    }, bot_id=70001)
    check.ok("@所有人 不是点她", nobody is not None and not nobody.mentioned)
    allat = onebot.parse_inbound({
        "message_type": "group", "group_id": 88001, "user_id": 20002,
        "message": [{"type": "at", "data": {"qq": "all"}}],
    }, bot_id=70001)
    check.ok("消息段里的 at all 也不算点她", allat is not None and not allat.mentioned)
    nasty = onebot.parse_inbound({
        "message_type": "private", "user_id": "../../etc", "message": [{"type": "text", "data": {"text": "借过"}}],
    })
    check.ok("非数字的号进不来（拿它当目录名会翻车）", nasty is None, nasty)
    check.ok("目录名里不留路径分隔符", onebot._safe_id("../etc/passwd") == "..etcpasswd",
             onebot._safe_id("../etc/passwd"))
    check.ok("目录名长度封顶", len(onebot._safe_id("7" * 200)) == onebot._ID_MAX_LEN)
    from core.storage_manager import PathSafetyError, StorageManager as _Storage

    id_root = Path(tempfile.mkdtemp(prefix="mysoulbot-id-"))
    guarded = _Storage(make_settings(id_root, "http://x/v1"))
    try:
        guarded.user_dir(onebot._PRIVATE_SPACE + "../escape")
        blocked = False
    except PathSafetyError:
        blocked = True
    finally:
        shutil.rmtree(id_root, ignore_errors=True)
    check.ok("拼出来的空间名仍过存储层这道闸", blocked)
    check.ok("通知类消息不接",
             onebot.parse_inbound({"message_type": "notify", "user_id": 1, "message": []}) is None)
    check.ok("群消息缺群号不接",
             onebot.parse_inbound({"message_type": "group", "user_id": 1, "message": "在吗"}, bot_id=70001) is None)
    voiced = onebot.parse_inbound({
        "message_type": "private", "user_id": 20002,
        "message": [{"type": "record", "data": {"file": "a.slk"}}],
    })
    check.ok("语音消息不变成空气", voiced is not None and "语音" in voiced.text, voiced and voiced.text)
    picked = onebot.parse_inbound({
        "message_type": "private", "user_id": 20002,
        "raw_message": "看这个[CQ:image,file=http://cdn.qq.com/1.png]",
    })
    check.ok("图链被摘出来喂视神经", picked is not None and picked.images == ("http://cdn.qq.com/1.png",)
             and picked.text == "看这个", picked)

    # 握手判定
    def request_of(method="GET", target="/onebot/v11", **headers: str):
        return onebot._Request(method, target, {key.lower(): value for key, value in headers.items()})

    good = request_of(Upgrade="websocket", Connection="Upgrade",
                      **{"Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"})
    settings = make_settings(Path("/tmp"), "http://127.0.0.1:9/v1", onebot_access_token="")
    check.ok("没配 token 时合规握手放行（网桥只绑回环）", onebot.check_handshake(good, settings)[0] == 0)
    check.ok("POST 被挡在门外", onebot.check_handshake(onebot._Request("POST", "/", {}), settings)[0] == 405)
    no_up = onebot._Request("GET", "/", {"upgrade": "keep-alive"})
    check.ok("不是升级请求就 400", onebot.check_handshake(no_up, settings)[0] == 400)
    no_key = onebot._Request("GET", "/", {"upgrade": "websocket", "connection": "Upgrade"})
    check.ok("缺 Sec-WebSocket-Key 就 400", onebot.check_handshake(no_key, settings)[0] == 400)
    old_ver = onebot._Request("GET", "/", {"upgrade": "websocket", "connection": "Upgrade",
                                           "sec-websocket-key": "k", "sec-websocket-version": "8"})
    check.ok("版本不对回 426", onebot.check_handshake(old_ver, settings)[0] == 426)
    locked = make_settings(Path("/tmp"), "http://127.0.0.1:9/v1", onebot_access_token="qq-test-token")
    bad_token = onebot._Request("GET", "/", {"upgrade": "websocket", "connection": "Upgrade",
                                             "sec-websocket-key": "k", "sec-websocket-version": "13",
                                             "authorization": "Bearer wrong"})
    check.ok("token 不对回 401", onebot.check_handshake(bad_token, locked)[0] == 401)
    right_token = onebot._Request("GET", "/", {"upgrade": "websocket", "connection": "Upgrade",
                                               "sec-websocket-key": "k", "sec-websocket-version": "13",
                                               "authorization": "Bearer qq-test-token"})
    check.ok("token 对了就放行", onebot.check_handshake(right_token, locked)[0] == 0)
    check.ok("空头的 Authorization 也当没带（回环下放行）",
             onebot.check_handshake(good, make_settings(Path("/tmp"), "http://x/v1",
                                                        onebot_access_token=""))[0] == 0)
    refused = onebot.reject_response(401, "鉴权串不对")
    head, _, body = refused.partition(b"\r\n\r\n")
    check.ok("拒绝响应的状态行是纯 ASCII", b"HTTP/1.1 401 Unauthorized" == head.split(b"\r\n")[0], head[:40])
    check.ok("拒绝响应把中文理由留在正文里", body.decode("utf-8") == "鉴权串不对", body[:40])
    check.ok("拒绝响应带上 Content-Length", b"Content-Length" in head)
    query_token = onebot._Request("GET", "/?access_token=qq-tes", {"upgrade": "websocket", "connection": "Upgrade",
                                                                   "sec-websocket-key": "k",
                                                                   "sec-websocket-version": "13"})
    check.ok("URL 上的 access_token 也认",
             onebot.check_handshake(query_token, make_settings(Path("/tmp"), "http://x/v1",
                                                              onebot_access_token="qq-tes"))[0] == 0)


# ---------------------------------------------------------------- 起一台网桥
class Rig:
    def __init__(self, **overrides: Any) -> None:
        from core.adapters.qq_onebot import OneBotBridge
        from core.bot import MySoulBot
        from core.card_loader import PersonaLibrary
        from core.clawd_soul import ClawdSoul
        from core.memory_extractor import MemoryExtractor
        from core.prompt_builder import PromptBuilder
        from core.storage_manager import StorageManager

        self.settings = make_settings(Path(tempfile.mkdtemp(prefix="mysoulbot-qq-")), ORIGIN + "/v1", **overrides)
        self.storage = StorageManager(self.settings)
        self.clawd = ClawdSoul(self.settings)
        self.extractor = MemoryExtractor(self.settings, self.storage)
        self.bot = MySoulBot(self.settings, self.storage, PromptBuilder(self.settings, self.storage, self.clawd),
                             self.extractor, PersonaLibrary(self.settings), self.clawd)
        self.bridge = OneBotBridge(self.settings, self.bot)
        self.port = 0

    async def start(self) -> int:
        await self.clawd.ensure()
        if self.extractor.enabled:
            self.extractor.start()
        _, self.port = await self.bridge.start("127.0.0.1", 0)
        return self.port

    async def stop(self) -> None:
        await self.bridge.stop()
        await self.extractor.aclose(timeout=5.0)
        await self.bot.aclose()
        shutil.rmtree(self.settings.storage_dir, ignore_errors=True)


def private_event(text: str, *, user_id: int = 20002, message_id: int = 1001,
                  segments: bool = True) -> dict[str, Any]:
    body: Any = [{"type": "text", "data": {"text": text}}] if segments else text
    return {
        "message_type": "private", "message_id": message_id, "user_id": user_id, "group_id": 0,
        "sender": {"user_id": user_id, "nickname": "阿哲", "card": ""}, "message": body,
        "raw_message": text,
    }


def group_event(text: str, *, group_id: int = 88001, user_id: int = 20002, message_id: int = 2001,
                mention: bool = False, card: str = "老哲", nickname: str = "阿哲") -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if mention:
        segments.append({"type": "at", "data": {"qq": "70001"}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "message_type": "group", "message_id": message_id, "group_id": group_id, "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": nickname, "card": card, "role": "member"},
        "message": segments, "raw_message": text,
    }


# ---------------------------------------------------------------- 2. 鉴权与心跳
async def handshake_checks(check: Checker) -> None:
    rig = Rig()
    port = await rig.start()
    try:
        from core.adapters import qq_onebot as onebot

        def bad_token():
            client = QQ(port)
            try:
                return client.handshake("wrong-token")
            finally:
                client.closes()

        status, _ = await asyncio.to_thread(bad_token)
        check.ok("错 token 被挡在 401", status.startswith("HTTP/1.1 401"), status)

        def no_token():
            client = QQ(port)
            try:
                return client.handshake()
            finally:
                client.closes()

        status, _ = await asyncio.to_thread(no_token)
        check.ok("不带 token 也是 401", status.startswith("HTTP/1.1 401"), status)

        def plain_http():
            client = QQ(port)
            try:
                client.raw_request("GET", "/onebot/v11", ["Host: x"])
                first = client.status_line()
                headers = client.read_headers()
                length = int(headers.get("content-length", "0") or 0)
                body = client._take(length, 3.0).decode("utf-8") if length else ""
                return first, body
            finally:
                client.closes()

        raw_head, raw_body = await asyncio.to_thread(plain_http)
        check.ok("普通 GET 不当 WS 供", raw_head.startswith("HTTP/1.1 400"), raw_head)
        check.ok("拒绝时把理由说给人看", "WebSocket" in raw_body, raw_body[:60])

        def right_token():
            client = QQ(port)
            status_line, headers = client.handshake("qq-test-token")
            accept = headers.get("sec-websocket-accept", "")
            expected = onebot.ws_accept_key(client.key)
            client.heartbeat()
            got = client.pump(2.0)
            return status_line, accept, expected, headers, got

        status_line, accept, expected, headers, got = await asyncio.to_thread(right_token)
        check.ok("对的 token 换成 101", status_line.startswith("HTTP/1.1 101"), status_line)
        check.ok("应答值是自己算得出的那一个", accept == expected, f"{accept} vs {expected}")
        check.ok("升级头原样回给协议端", headers.get("upgrade", "").lower() == "websocket", headers)
        check.ok("协议端身份从事件里认出来", rig.bridge.status()["self_id"] == "70001", rig.bridge.status())
        check.ok("昵称进了唤醒名单", rig.bridge.status()["bot_names"] == ["夜汐"], rig.bridge.status()["bot_names"])
        check.ok("心跳后网桥只问了一次身份",
                 [frame["action"] for frame in got] == ["get_login_info"], got)
        state = rig.bridge.status()
        check.ok("心跳被记进保活读数", state["heartbeat_seconds_ago"] is not None, state)
        check.ok("对面在线与否有读数", state["peer_online"] is True, state)

        def second_heartbeat():
            client = QQ(port)
            client.handshake("qq-test-token")
            for _ in range(3):
                client.heartbeat()
                time.sleep(0.05)
            return client.pump(1.5)

        repeats = await asyncio.to_thread(second_heartbeat)
        check.ok("认过身份之后不重复去问", repeats == [], repeats)
        check.ok("重复心跳只涨保活不涨事件", rig.bridge.status()["counts"]["events"] == 0,
                 rig.bridge.status()["counts"])

        def junk_frames():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.sock.sendall(b"\x81\x00")  # 未掩码的客户端帧：不合规矩
            return client.read_json(3.0)

        broken = await asyncio.to_thread(junk_frames)
        check.ok("未掩码帧只脏这一条连接", broken is None or "_control" in (broken or {}), broken)
        check.ok("网桥没被坏帧带崩", rig.bridge.status()["listening"] is True)

        def live_again():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.send_frame(0x9, b"ping-payload")
            try:
                opcode, payload = client.read_frame(3.0)
            except (_Idle, _Disconnected):
                opcode, payload = -1, b""
            client.heartbeat()
            client.event(**private_event("在吗", message_id=777))
            return opcode, payload, client.pump(8.0)

        opcode, payload, got = await asyncio.to_thread(live_again)
        check.ok("服务端认 ping 并回 pong", opcode == 0xA, opcode)
        check.ok("pong 带着原来的 payload", payload.startswith(b"ping"), payload[:12])
        check.ok("坏帧之后仍能接新连接并答话", len(sends(got)) == 1, got)

        def fragmented():
            client = QQ(port)
            client.handshake("qq-test-token")
            event = json.dumps(
                {"post_type": "message", **private_event("分着发也算一句", message_id=778)}, ensure_ascii=False
            ).encode()
            half = len(event) // 2
            client.send_frame(0x1, event[:half], fin=False)
            client.send_frame(0x0, event[half:])
            return client.pump(8.0)

        got = await asyncio.to_thread(fragmented)
        check.ok("分片帧被拼回成一条事件", len(sends(got)) == 1, got)

        def bad_json():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.send_frame(0x1, b"{not json at all")
            client.heartbeat()
            return client.pump(2.0)

        await asyncio.to_thread(bad_json)
        check.ok("乱码帧只丢不炸", rig.bridge.status()["counts"]["ignored"] >= 1, rig.bridge.status()["counts"])
        check.ok("乱码之后心跳照旧", rig.bridge.status()["heartbeat_seconds_ago"] is not None)

        def not_woken_and_notice():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.send_json({"post_type": "request", "request_type": "friend", "user_id": 555})
            client.send_json({"post_type": "notice", "notice_type": "group_increase", "group_id": 88001})
            client.heartbeat()
            return client.pump(2.0)

        await asyncio.to_thread(not_woken_and_notice)
        check.ok("加好友申请只记不办",
                 rig.bridge.status()["counts"]["ignored"] >= 2, rig.bridge.status()["counts"])

        def oversized():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.send_frame(0x1, b"x" * (onebot._MAX_FRAME_BYTES + 10))
            return client.read_json(3.0)

        oversized_read = await asyncio.to_thread(oversized)
        check.ok("超上限的帧被拒而不是吞下", oversized_read is None or "_control" in (oversized_read or {}),
                 str(oversized_read)[:40])
    finally:
        await rig.stop()


# ---------------------------------------------------------------- 3. 私聊：灵魂、温度、记忆
async def private_checks(check: Checker) -> None:
    rig = Rig(extractor_enabled=True)
    port = await rig.start()
    try:
        def round_one():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.heartbeat()
            client.event(**private_event("我今晚又三点才睡", message_id=3001))
            return client.pump(10.0)

        reset_model()
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(round_one)
        actions = sends(got)
        check.ok("私聊只回一条 send_private_msg", len(actions) == 1, got)
        frame = actions[0] if actions else {}
        check.ok("回的是那一个人自己的号", frame.get("params", {}).get("user_id") == 20002, frame)
        body = texts([frame])
        check.ok("话被拼成整段发出去", body and "这个点你还醒着" in body[0], body)
        check.ok("套话反问没出口", not any(CLOSER in piece for piece in body), body)
        check.ok("文本走消息段而不是 CQ 串",
                 isinstance(frame.get("params", {}).get("message"), list), frame.get("params"))

        payload = last_chat()
        prompt = system_prompt_of(payload)
        check.ok("深层灵魂进了一层", "LAYER 0 · 深层灵魂" in prompt)
        check.ok("人格内核进了一层", "LAYER 1 · 人格内核" in prompt)
        check.ok("熟络度温度计带上了", "熟络度" in prompt and "/100" in prompt)
        check.ok("生理节律带上了", "此刻（" in prompt and "你的身体：" in prompt)
        check.ok("对话对象是 qq_private_ 那个空间", "对话对象：qq_private_20002" in prompt)
        check.ok("私聊不带群聊准则", "【群聊准则】" not in prompt)
        user_part = last_user_of(payload)
        check.ok("送进模型的就是他那句话", user_part == "我今晚又三点才睡", str(user_part)[:60])
        check.ok("引擎用的模型名没被客户端带跑", payload.get("model") == "chat-only", payload.get("model"))

        user_dir = rig.settings.users_dir / "qq_private_20002"
        check.ok("这个人的目录被开出来了", user_dir.is_dir(), str(user_dir))
        check.ok("四份文档都在场", all((user_dir / f"{doc}.md").is_file() for doc in
                                      ("SOUL", "USER", "MEMORY", "RELATIONS")))
        check.ok("体温落了盘", (user_dir / "state.json").is_file())
        logs = sorted((user_dir / "logs").glob("*.jsonl"))
        transcript = "\n".join(path.read_text(encoding="utf-8") for path in logs)
        check.ok("这一轮对话进了日志", "我今晚又三点才睡" in transcript, transcript[-120:])

        left = await rig.bot.flush_extractions(10.0)
        memory = (user_dir / "MEMORY.md").read_text(encoding="utf-8")
        check.ok("长期记忆被后台抽出来落盘", FACT_LINE in memory and left == 0, f"{memory[-160:]} 积压={left}")
        check.ok("抽取走过的是同一个假端点",
                 any(EXTRACT_MARK in json.dumps(item, ensure_ascii=False) for item in SEEN))

        def round_two():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("那我明天再说", message_id=3002))
            return client.pump(10.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(round_two)
        check.ok("同一个人第二句照样接", len(sends(got)) == 1, got)
        second = system_prompt_of(last_chat())
        history = [message for message in (last_chat().get("messages") or []) if message.get("role") != "system"]
        check.ok("上一轮被带进上下文", any("我今晚又三点才睡" in str(message.get("content")) for message in history),
                 str(history)[:200])
        check.ok("熟络度攒过之后读数还在", "熟络度" in second)
        check.ok("一个空间只有一个会话对象", "qq_private_20002" in second)

        def another_person():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("我换个号跟你说一句", user_id=20003, message_id=3003))
            return client.pump(10.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(another_person)
        spaces = sorted(path.name for path in rig.settings.users_dir.iterdir() if path.is_dir())
        check.ok("换一个 QQ 号就是另一个人", "qq_private_20003" in spaces, spaces)
        check.ok("两个人的记忆不串门",
                 "我换个号跟你说一句" not in transcript
                 and "我今晚又三点才睡" not in "\n".join(
                     path.read_text(encoding="utf-8")
                     for path in (rig.settings.users_dir / "qq_private_20003" / "logs").glob("*.jsonl")),
                 spaces)
        third = system_prompt_of(last_chat())
        check.ok("新人物的对话对象是新空间", "对话对象：qq_private_20003" in third, third[-200:])
        check.ok("新人物从陌生期起步", "熟络度 0/100" in third or "陌生期" in third, third[-200:])
    finally:
        await rig.stop()


# ---------------------------------------------------------------- 4. 图片摄取
async def image_checks(check: Checker) -> None:
    rig = Rig()
    port = await rig.start()
    url = f"{ORIGIN}/pic.png"
    local = Path(tempfile.mkstemp(suffix=".png")[1])
    local.write_bytes(png_bytes("local"))
    try:
        def via_url():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event(f"看这个[CQ:image,file=1.png,url={url}]", message_id=4001))
            return client.pump(12.0)

        reset_model()
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(via_url)
        payload = last_chat()
        content = last_user_of(payload)
        check.ok("图链那条也答了话", len(sends(got)) == 1, got)
        check.ok("CQ 里的图链没留在话里", "http" not in texts(got)[0], texts(got))
        parts = content if isinstance(content, list) else []
        images = [part for part in parts if isinstance(part, dict) and part.get("type") == "image_url"]
        check.ok("图片本体进了模型视野", len(images) == 1, str(content)[:120])
        check.ok("带图那趟用的是 VISION_MODEL", payload.get("model") == "eyes-pro", payload.get("model"))
        check.ok("图是 base64 递过去的", images and images[0]["image_url"]["url"].startswith("data:image/png"))
        note = system_prompt_of(payload)
        check.ok("语境里告诉他这是对方递来的", "他刚给你看了" in note or "看不了" in note)

        def via_segments():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.send_json({
                "post_type": "message", "message_type": "private", "self_id": 70001, "message_id": 4003,
                "user_id": 20002, "sender": {"nickname": "阿哲"},
                "message": [{"type": "text", "data": {"text": "再看一张"}},
                            {"type": "image", "data": {"file": str(local), "url": ""}}],
            })
            return client.pump(12.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(via_segments)
        check.ok("消息段形态的图也收进视野", len(sends(got)) == 1, got)
        content = last_user_of(last_chat())
        check.ok("本地文件那一路变成 image 分段",
                 isinstance(content, list) and any(
                     isinstance(part, dict) and part.get("type") == "image_url" for part in content),
                 str(content)[:120])

        def broken_image():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("看[CQ:image,file=/没有这种图.png,url=]", message_id=4004))
            return client.pump(12.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(broken_image)
        check.ok("坏图不 500，话照说", len(sends(got)) == 1, got)
        note = system_prompt_of(last_chat())
        check.ok("收不下的图会如实说一句", "收不下" in note or "没找到" in note or "读不出来" in note, note[-200:])

        def vision_refused():
            MODE["reject_vision"] = True
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event(f"再看[CQ:image,file=x,url={url}]", message_id=4005))
            out = client.pump(15.0)
            MODE["reject_vision"] = False
            return out

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(vision_refused)
        check.ok("接口不认图时退回而不是闭嘴", len(sends(got)) == 1, got)
        check.ok("退回时话还在说", any("卡了一下" not in piece for piece in texts(got)), texts(got))
    finally:
        local.unlink(missing_ok=True)
        await rig.stop()


# ---------------------------------------------------------------- 5. 群聊
async def group_checks(check: Checker) -> None:
    rig = Rig(tools_enabled=True, reflection_enabled=True, tool_native_calling=False)
    port = await rig.start()
    try:
        def not_mentioned():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.heartbeat()
            client.pump(1.5)
            client.event(**group_event("有人看到那只猫了吗", mention=False, message_id=5001))
            return client.pump(4.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(not_mentioned)
        check.ok("没点名的群消息不接", sends(got) == [], got)
        check.ok("没接话也记了数", rig.bridge.status()["counts"]["not_woken"] >= 1, rig.bridge.status()["counts"])
        with LOCK:
            seen_at_idle = list(SEEN)
        check.ok("没接话时一个字都没送进模型", seen_at_idle == [], len(seen_at_idle))

        clawd_before = rig.clawd.path.read_text(encoding="utf-8")
        smuggled = '⟦tool:reflect text="以后在群里先闭嘴" target=self⟧'

        def mentioned():
            MODE["once"] = [smuggled]
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**group_event("今晚走不走", mention=True, message_id=5002))
            return client.pump(12.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(mentioned)
        actions = sends(got)
        check.ok("被 @ 就接话", len(actions) == 1, got)
        frame = actions[0] if actions else {}
        check.ok("群消息走 send_group_msg", frame.get("action") == "send_group_msg", frame.get("action"))
        check.ok("回到那个群", frame.get("params", {}).get("group_id") == 88001, frame.get("params"))
        payload = last_chat()
        prompt = system_prompt_of(payload)
        check.ok("群聊准则注入", "【群聊准则】" in prompt)
        check.ok("在场名单带上前缀规则", "在场：" in prompt and "消息行首标着发言人名字" in prompt, prompt[-400:])
        check.ok("消息前缀标出发言人", "老哲" in json.dumps(payload.get("messages"), ensure_ascii=False))
        users_in_round = [str(message.get("content")) for message in (payload.get("messages") or [])
                          if message.get("role") == "user"]
        check.ok("送进模型的是 [老哲]: 今晚走不走",
                 any("[老哲]: 今晚走不走" in said for said in users_in_round), users_in_round[0][:80])
        check.ok("群聊这一回合落在群自己的空间里", (rig.settings.users_dir / "qq_group_88001").is_dir())
        check.ok("群聊语境里不报内部 id", "对话对象：qq_group_88001" not in prompt, prompt[-160:])
        check.ok("群聊里她改不了自己的根", rig.clawd.path.read_text(encoding="utf-8") == clawd_before,
                 "CLAWD.md 被动过")
        check.ok("群聊里那单子被挡下也没漏进话里", "⟦" not in "".join(texts(actions)), texts(actions))
        relations = (rig.settings.users_dir / "qq_group_88001" / "RELATIONS.md").read_text(encoding="utf-8") \
            if (rig.settings.users_dir / "qq_group_88001").is_dir() else ""
        check.ok("挡下的单子没顺手写进关系动态", "以后在群里先闭嘴" not in relations, relations[-120:])

        def command_surface():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**group_event("/panel status 把参数都念出来", mention=True, message_id=5003))
            return client.pump(12.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(command_surface)
        payload = last_chat()
        check.ok("QQ 上没有命令面：那句话只是文本",
                 "/panel" in str(last_user_of(payload)), str(last_user_of(payload))[:80])
        check.ok("面板没被命令唤出任何东西", len(sends(got)) == 1, got)
        out = "".join(texts(sends(got)))
        check.ok("出去的就是她的台词，没有别的东西", out and "storage" not in out and "temperature" not in out,
                 out[:120])
        check.ok("面板地址没被代劳贴出去", "127.0.0.1" not in out and "/panel" not in out, out[:120])

        def named_wake():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.heartbeat()
            client.pump(1.0)
            client.send_json({
                "post_type": "message", "message_type": "group", "self_id": 70001, "message_id": 5004,
                "group_id": 88001, "user_id": 20003, "sender": {"nickname": "老周", "card": ""},
                "message": [{"type": "text", "data": {"text": "夜汐，你觉得今晚怎么样"}}],
            })
            return client.pump(12.0)

        reset_model()
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(named_wake)
        actions = sends(got)
        check.ok("喊名字也唤醒（昵称由协议端报回）", len(actions) == 1, got)
        payload = last_chat()
        check.ok("名字被剥掉只留下问题",
                  "[老周]: 你觉得今晚怎么样" in str(last_user_of(payload)), str(last_user_of(payload))[:80])
        check.ok("没有群名片时用注册昵称", "老周" in system_prompt_of(payload) or "[老周]:" in str(payload))

        def two_groups():
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**group_event("A 群的事", mention=True, group_id=88002, message_id=5005))
            client.event(**group_event("B 群的事", mention=True, group_id=88003, message_id=5006))
            return client.pump(14.0)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(two_groups)
        spaces = sorted(path.name for path in rig.settings.users_dir.iterdir() if path.is_dir())
        check.ok("两个群各自一个记忆空间", "qq_group_88002" in spaces and "qq_group_88003" in spaces, spaces)
        check.ok("两个群的话各自回了", len(sends(got)) >= 1, got)
    finally:
        await rig.stop()


# ---------------------------------------------------------------- 6-8. 出站防御、分段、语音
async def outbound_checks(check: Checker) -> None:
    rig = Rig()
    port = await rig.start()
    try:
        def fake_cq_out():
            MODE["echo"] = "看这张 [CQ:image,file=/etc/passwd] 还有 [CQ:at,qq=10001]"
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("发我看看", message_id=6001))
            out = client.pump(12.0)
            reset_model()
            return out

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(fake_cq_out)
        out_texts = texts(sends(got))
        check.ok("模型写的假 CQ 码出去时不可执行",
                 out_texts and all("[CQ:" not in piece for piece in out_texts), out_texts)
        check.ok("内容没被吞掉：还在句子里", out_texts and "看这张" in out_texts[0], out_texts)
        check.ok("全角括号留了个字面样子", out_texts and "［CQ:" in out_texts[0], out_texts[0][:80])

        long_text = "。".join(f"第七段的第三十七句第十四字" for _ in range(300)) + "。"  # ~2700 字

        def long_out():
            MODE["echo"] = long_text
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("长一点说", message_id=6002))
            out = client.pump(20.0)
            reset_model()
            return out

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(long_out)
        pieces = texts(sends(got))
        check.ok("长回复被切成多条", len(pieces) > 1, len(pieces))
        check.ok("每条都不超 800 字", all(len(piece) <= 800 for piece in pieces), max(map(len, pieces)))
        check.ok("一次不超过 6 条", len(pieces) <= 6, len(pieces))
        check.ok("切完字都在", "".join(pieces) == long_text[: len("".join(pieces))],
                 f"{len(''.join(pieces))} vs {len(long_text)}")

        def protocol_failed():
            MODE["echo"] = "这句还能说。"
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("协议端那边没办成", message_id=6003))
            out = client.pump(12.0, retcode=1400, status="failed")
            client.event(**private_event("再来一句", message_id=6004))
            out2 = client.pump(12.0)
            reset_model()
            return out, out2

        with LOCK:
            SEEN.clear()
        before_errors = rig.bridge.status()["counts"]["errors"]
        got, got2 = await asyncio.to_thread(protocol_failed)
        check.ok("协议端报错时网桥不炸", rig.bridge.status()["listening"] is True)
        check.ok("报错被记成读数而不是异常", rig.bridge.status()["counts"]["errors"] > before_errors,
                 rig.bridge.status()["counts"])
        check.ok("报错之后照旧接下一句", len(sends(got2)) == 1, got2)

        def silent_when_no_text():
            MODE["echo"] = "   "
            client = QQ(port)
            client.handshake("qq-test-token")
            client.event(**private_event("她说不出话", message_id=6005))
            out = client.pump(6.0)
            reset_model()
            return out

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(silent_when_no_text)
        check.ok("模型空正文不会发空气消息", all(seg.get("type") != "text"
                 for frame in sends(got) for seg in (frame.get("params", {}).get("message") or [])
                 if isinstance(seg, dict)) or sends(got) == [] or
                 all(str(seg.get("data", {}).get("text", "")).strip()
                     for frame in sends(got) for seg in (frame.get("params", {}).get("message") or [])),
                 sends(got))
    finally:
        await rig.stop()


def _round(port: int, text: str, message_id: int, *, segments: bool = True) -> list[dict[str, Any]]:
    """一轮私聊：连进去、发一句、把服务端发来的动作都接住。"""
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        client.event(**private_event(text, message_id=message_id, segments=segments))
        return client.pump(15.0)
    finally:
        client.closes()


def _group_round(port: int, text: str, message_id: int) -> list[dict[str, Any]]:
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        client.event(**group_event(text, mention=True, message_id=message_id))
        return client.pump(15.0)
    finally:
        client.closes()


# ---------------------------------------------------------------- 8. 拟人语音回传
async def voice_checks(check: Checker) -> None:
    rig = Rig(onebot_auto_record=True, tts_enabled=True, tts_provider="stub")
    rig_off_tts = Rig(onebot_auto_record=True, tts_enabled=False)
    rig_off_record = Rig(onebot_auto_record=False, tts_enabled=True, tts_provider="stub")
    port = await rig.start()
    port_b = await rig_off_tts.start()
    port_c = await rig_off_record.start()
    try:
        reset_model()
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_round, port, "念给我听", 7001)
        actions = sends(got)
        voices = [frame for frame in actions if isinstance(frame.get("params", {}).get("message"), str)]
        words = [frame for frame in actions if isinstance(frame.get("params", {}).get("message"), list)]
        check.ok("私聊说完挂了一条语音", len(voices) == 1, actions)
        check.ok("话先发出去、声音随后到", bool(words) and bool(actions) and actions[-1] is voices[0],
                 [frame["action"] for frame in actions])
        message = str(voices[0]["params"]["message"]) if voices else ""
        check.ok("语音是 CQ record 形态", message.startswith("[CQ:record,file=file://"), message[:60])
        path = Path(message.split("file=", 1)[1].rstrip("]").removeprefix("file://")) if voices else Path("/none")
        check.ok("音频真的落在盘上", path.is_file(), str(path))
        head = path.read_bytes()[:12] if path.is_file() else b""
        check.ok("那是一段能播的 WAV", head[:4] == b"RIFF" and head[8:12] == b"WAVE", head)
        check.ok("声音存在这一个人的产物目录里", "qq_private_20002" in str(path), str(path))
        check.ok("语音回的是同一个人", bool(voices) and voices[0]["params"]["user_id"] == 20002,
                 voices[0]["params"] if voices else None)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_group_round, port, "群里也念一段", 7002)
        check.ok("群里不出声（那是刷屏）",
                 all(not str(frame.get("params", {}).get("message", "")).startswith("[CQ:record")
                     for frame in sends(got)), sends(got))
        check.ok("群里话照说", len(sends(got)) == 1, sends(got))

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_round, port_b, "声音总开关关了呢", 7003)
        check.ok("TTS 关掉就没有语音条",
                 all(isinstance(frame.get("params", {}).get("message"), list) for frame in sends(got)),
                 sends(got))
        check.ok("关掉语音不影响说话", len(sends(got)) == 1, sends(got))

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_round, port_c, "网桥的出声开关关了呢", 7004)
        check.ok("ONEBOT_AUTO_RECORD=false 就不念",
                 all(isinstance(frame.get("params", {}).get("message"), list) for frame in sends(got)),
                 sends(got))
        check.ok("不出声时话照发", len(sends(got)) == 1, sends(got))
    finally:
        await rig.stop()
        await rig_off_tts.stop()
        await rig_off_record.stop()

# ---------------------------------------------------------------- 9. 防刷屏 / 去重 / 占用
def _flood_round(port: int, count: int, start_id: int) -> list[dict[str, Any]]:
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        for index in range(count):
            client.event(**private_event(f"第 {index} 句刷屏", message_id=start_id + index))
        return client.pump(20.0)
    finally:
        client.closes()


def _dup_round(port: int) -> list[dict[str, Any]]:
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        for _ in range(2):
            client.event(**private_event("同一条重发", message_id=9100))
        return client.pump(12.0)
    finally:
        client.closes()


def _self_round(port: int) -> list[dict[str, Any]]:
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        client.heartbeat()
        client.pump(1.0)
        client.event(**private_event("我说过的话再喂给自己", user_id=70001, message_id=9200))
        return client.pump(4.0)
    finally:
        client.closes()


def _queue_round(port: int, first: str, second: str) -> list[dict[str, Any]]:
    client = QQ(port)
    try:
        client.handshake("qq-test-token")
        client.event(**private_event(first, message_id=9300))
        client.event(**private_event(second, message_id=9301))
        return client.pump(15.0)
    finally:
        client.closes()


async def flood_checks(check: Checker) -> None:
    rig = Rig()
    port = await rig.start()
    try:
        reset_model()
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_flood_round, port, 12, 8000)
        state = rig.bridge.status()
        check.ok("刷屏被限住（回复数远小于消息数）", len(sends(got)) <= 5, len(sends(got)))
        check.ok("超出的那些记在防洪读数里", state["counts"]["flood"] + state["counts"]["busy"] >= 7,
                 state["counts"])
        check.ok("防洪窗口内最多 5 轮", state["counts"]["replies"] <= 5, state["counts"])
        check.ok("限流不报错也不炸", state["listening"] is True and state["counts"]["errors"] == 0, state)
    finally:
        await rig.stop()

    rig2 = Rig()
    port2 = await rig2.start()
    try:
        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_dup_round, port2)
        check.ok("同 message_id 只答一次", len(sends(got)) == 1, sends(got))
        check.ok("重发记成重复", rig2.bridge.status()["counts"]["duplicate"] == 1,
                 rig2.bridge.status()["counts"])

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_self_round, port2)
        check.ok("她自己的话不再喂给自己", sends(got) == [], got)
        with LOCK:
            check.ok("自答不送进模型", SEEN == [], len(SEEN))

        reset_model()
        MODE["pieces"] = ["这句长一点。", "够她把上一句说完之前都还没开口。"]
        MODE["slow"] = 0.35
        got = await asyncio.to_thread(_queue_round, port2, "上一句还没说完", "插队的这一句")
        counts = rig2.bridge.status()["counts"]
        check.ok("上一条没说完时插队的被丢掉而不是叠着发", counts["busy"] == 1, counts)
        check.ok("插队那句只回了一条", len(sends(got)) == 1, sends(got))
        reset_model()
    finally:
        await rig2.stop()


# ---------------------------------------------------------------- 10. 服务联动
async def server_checks(check: Checker) -> None:
    from core.server import SoulServer

    def health(port: int) -> dict[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return json.loads(body)

    def listening(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except OSError:
            return False

    def free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    root_off = Path(tempfile.mkdtemp(prefix="mysoulbot-qq-off-"))
    closed = make_settings(root_off, ORIGIN + "/v1", onebot_enabled=False, onebot_port=11556)
    server = SoulServer(closed)
    _, http_port = await server.start("127.0.0.1", 0)
    try:
        body = await asyncio.to_thread(health, http_port)
        check.ok("开关关掉时酒馆那条路照常在", body["ok"] is True)
        check.ok("开关关掉时网桥对象不存在", server.onebot is None)
        check.ok("healthz 如实说没开", body["onebot"] == {"enabled": False, "listening": False}, body["onebot"])
        check.ok("开关关掉时一个额外端口都不开", not listening("127.0.0.1", 11556))
    finally:
        await server.stop()
        shutil.rmtree(root_off, ignore_errors=True)

    root_on = Path(tempfile.mkdtemp(prefix="mysoulbot-qq-on-"))
    qq_port = free_port()
    opened = make_settings(root_on, ORIGIN + "/v1", onebot_enabled=True, onebot_port=qq_port)
    server2 = SoulServer(opened)
    _, http_port = await server2.start("127.0.0.1", 0)
    try:
        reset_model()
        bridge = (await asyncio.to_thread(health, http_port))["onebot"]
        check.ok("开了开关网桥就真存在", server2.onebot is not None)
        check.ok("healthz 报得出网桥在听", bridge["listening"] is True, bridge)
        check.ok("healthz 里端口是配的那一个", bridge["bound"]["port"] == qq_port, bridge["bound"])
        check.ok("healthz 里鉴权状态如实", bridge["authenticated"] is True, bridge)
        check.ok("网桥自己不开第二个 HTTP 口", bridge["bound"]["port"] != http_port)

        with LOCK:
            SEEN.clear()
        got = await asyncio.to_thread(_round, qq_port, "从服务这条路进来", 9500)
        check.ok("服务带起的网桥接得住 QQ 消息", len(sends(got)) == 1, got)
        after = (await asyncio.to_thread(health, http_port))["onebot"]
        check.ok("healthz 数得出这次回复", after["counts"]["replies"] >= 1, after["counts"])
        settled = await server2.drain(3.0)
        check.ok("排空读数不骗人", settled["turns_left"] == 0, settled)

        holder: dict[str, QQ] = {}

        def keep_open() -> None:
            client = QQ(qq_port)
            client.handshake("qq-test-token")
            client.heartbeat()
            client.pump(1.0)
            holder["client"] = client

        await asyncio.to_thread(keep_open)
        before_stop = await asyncio.to_thread(health, http_port)
        check.ok("停机前协议端算在线", before_stop["onebot"]["connections"] == 1, before_stop["onebot"])
        await server2.stop()
        frame = await asyncio.to_thread(holder["client"].read_json, 5.0)
        check.ok("停机时先向协议端挥手", isinstance(frame, dict) and frame.get("_control") == 0x8, frame)
        check.ok("停机之后不再听这个端口", not listening("127.0.0.1", qq_port))
        check.ok("停机后网桥字段回落", server2.onebot is None)
        holder["client"].closes()
    finally:
        with contextlib.suppress(Exception):
            await server2.stop()
        shutil.rmtree(root_on, ignore_errors=True)

    root_bad = Path(tempfile.mkdtemp(prefix="mysoulbot-qq-naked-"))
    naked = make_settings(root_bad, ORIGIN + "/v1", onebot_enabled=True, onebot_host="0.0.0.0",
                          onebot_access_token="")
    server3 = SoulServer(naked)
    raised = ""
    try:
        await server3.start("127.0.0.1", 0)  # 不该走到这里
    except RuntimeError as exc:
        raised = str(exc)
        check.ok("没配 token 就不许绑非回环", "ONEBOT_ACCESS_TOKEN" in raised, raised[:120])
        check.ok("拒得干脆也不留半个网桥", server3.onebot is None)
    finally:
        with contextlib.suppress(Exception):
            await server3.stop()
        shutil.rmtree(root_bad, ignore_errors=True)


# ---------------------------------------------------------------- 主流程
async def main() -> int:
    global ORIGIN

    fake, base_url = serve_fake()
    ORIGIN = base_url[: -len("/v1")]
    check = Checker()
    try:
        pure_checks(check)
        await handshake_checks(check)
        await private_checks(check)
        await image_checks(check)
        await group_checks(check)
        await outbound_checks(check)
        await voice_checks(check)
        await flood_checks(check)
        await server_checks(check)
    finally:
        fake.shutdown()

    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
