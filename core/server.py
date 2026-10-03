"""OpenAI 兼容的本地服务：让酒馆（SillyTavern）直连本机 MySoulBot。

为什么手搓 HTTP：本环境没有 aiohttp/fastapi，而这一层只需要三个路由。
标准库 `asyncio.start_server` 就够，且 clone 即用、零新依赖。

路由：
- `POST /v1/chat/completions` —— 流式（SSE）与非流式都支持，酒馆 Chat Completion 直接可用
- `POST /v1/completions`      —— 老式 Text Completion 模式
- `GET  /v1/models`           —— 酒馆探活要看的模型列表
- `GET  /healthz`             —— 引擎状态（模型、存储目录、是否挂工具）

**多端同一套灵魂与记忆**：服务与 CLI 用同一个 `storage/`，同一个用户目录下的
SOUL / USER / MEMORY / RELATIONS / state.json 完全共享。酒馆那边发的 messages 里的历史
一律丢弃——上下文由引擎自己管（含重启后从日志恢复），所以从 CLI 换到酒馆不会「失忆」，
也不会把两边历史叠成两份。

**客户端不能调引擎**：酒馆表单里的 temperature / max_tokens / 停止词、以及任何
「忽略设定」的文字，都只当作对话内容，不改变模型参数与人格。默认只绑 127.0.0.1——
这套灵魂和记忆不该裸露在局域网里。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from config import Settings
from core.bot import BotError, MySoulBot
from core.card_loader import PersonaLibrary
from core.clawd_soul import ClawdSoul
from core.memory_extractor import MemoryExtractor
from core.prompt_builder import PromptBuilder
from core.storage_manager import PathSafetyError, StorageManager

logger: Final = logging.getLogger("mysoulbot.server")

_MAX_BODY_BYTES: Final[int] = 4_000_000
_STREAM_MEDIA: Final[str] = "text/event-stream"
_CORS_HEADERS: Final[dict[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type,authorization,x-mysoulbot-user",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SoulServer:
    """最小可用的 OpenAI 兼容前端，背后是同一个 MySoulBot 引擎。"""

    def __init__(self, settings: Settings, bot: MySoulBot | None = None) -> None:
        self._settings = settings
        self.storage = StorageManager(settings)
        self.clawd = ClawdSoul(settings)
        self.prompts = PromptBuilder(settings, self.storage, self.clawd)
        self.library = PersonaLibrary(settings)
        self.extractor = MemoryExtractor(settings, self.storage)
        self.bot = bot or MySoulBot(
            settings, self.storage, self.prompts, self.extractor, self.library, self.clawd
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._server: asyncio.Server | None = None
        self._opened: set[str] = set()
        self.requests = 0

    @property
    def settings(self) -> Settings:
        return self._settings

    # ------------------------------------------------------------ 生命周期
    async def start(self, host: str = "", port: int = 0) -> tuple[str, int]:
        host = host or self._settings.server_host
        port = port or self._settings.server_port
        await self.clawd.ensure()
        if self.extractor.enabled:
            self.extractor.start()
        self._server = await asyncio.start_server(self._handle, host, port)
        bound = self._server.sockets[0].getsockname() if self._server.sockets else (host, port)
        logger.info("酒馆兼容端点已监听 http://%s:%s/v1", bound[0], bound[1])
        return str(bound[0]), int(bound[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.extractor.aclose(timeout=5.0)
        await self.bot.aclose()

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        if self._server is None:
            raise RuntimeError("服务没能起来")
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------ 连接处理
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._dispatch(reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass  # 酒馆客户端断开是常态，不算事故
        except HttpError as exc:
            await self._fail(writer, exc.status, exc.message)
        except Exception as exc:  # noqa: BLE001 - 单个连接不能带崩服务
            logger.warning("连接处理异常: %s", exc, exc_info=True)
            await self._fail(writer, 500, f"{type(exc).__name__}")
        finally:
            _close_quietly(writer)

    async def _fail(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message, "type": "mysoulbot_error", "code": status}},
                          ensure_ascii=False).encode()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await _write(writer, status, body, extra={"Content-Type": "application/json", **_CORS_HEADERS})

    async def _dispatch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readline()
        if not head:
            return
        headers = await _read_headers(reader)
        try:
            method, raw_path, _ = head.decode("latin-1").split(" ", 2)
        except ValueError:
            raise HttpError(400, "请求行读不懂") from None
        body = await _read_body(reader, headers)
        path = urlparse(raw_path).path
        query = parse_qs(urlparse(raw_path).query)
        self.requests += 1

        if method.upper() == "OPTIONS":
            await _write(writer, 204, b"", extra=_CORS_HEADERS)
            return
        if method.upper() == "GET" and path.rstrip("/") in ("/v1/models", "/v1"):
            await _write(
                writer,
                200,
                json.dumps(self._models(), ensure_ascii=False).encode(),
                extra={"Content-Type": "application/json", **_CORS_HEADERS},
            )
            return
        if method.upper() == "GET" and path.rstrip("/") == "/healthz":
            await _write(
                writer,
                200,
                json.dumps(self._health(), ensure_ascii=False).encode(),
                extra={"Content-Type": "application/json", **_CORS_HEADERS},
            )
            return
        if method.upper() != "POST":
            raise HttpError(405, f"这个路径不接 {method}")
        payload = _json_of(body)
        if path.rstrip("/") == "/v1/chat/completions":
            await self._chat(writer, payload, query, headers)
            return
        if path.rstrip("/") == "/v1/completions":
            await self._completion(writer, payload, query, headers)
            return
        raise HttpError(404, f"没有这个路径：{path}")

    # ------------------------------------------------------------ 路由实现
    async def _chat(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        query: dict[str, list[str]],
        headers: Mapping[str, str],
    ) -> None:
        user_id = self._user_of(payload, query, headers)
        text = _last_user_text(payload)
        if not text:
            raise HttpError(400, "消息里没有用户说的话")
        await self._ensure_session(user_id)
        # 客户端送来的采样参数一律忽略：人格的节奏不是表单能改的
        logger.info("酒馆请求 · user=%s stream=%s 参数忽略=%s", user_id, bool(payload.get("stream")),
                    sorted(k for k in payload if k in {"temperature", "max_tokens", "top_p", "frequency_penalty"}))
        reply_id = f"chatcmpl-soul-{int(time.time() * 1000) % 10_000_000:07d}"
        created = int(time.time())
        model = self._settings.model

        if not payload.get("stream"):
            chunks = [piece async for piece in self._generate(user_id, text)]
            content = "".join(chunks)
            body = json.dumps(
                {
                    "id": reply_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": max(1, len(text) // 2),
                        "completion_tokens": max(1, len(content) // 2),
                        "total_tokens": max(2, (len(text) + len(content)) // 2),
                    },
                },
                ensure_ascii=False,
            ).encode()
            await _write(writer, 200, body, extra={"Content-Type": "application/json", **_CORS_HEADERS})
            return

        await _stream_head(writer)
        await _sse(writer, reply_id, created, model, {"role": "assistant", "content": ""})
        try:
            async for piece in self._generate(user_id, text):
                await _sse(writer, reply_id, created, model, {"content": piece})
        except HttpError:
            raise
        await _sse(writer, reply_id, created, model, {}, finish="stop")
        await _write_raw(writer, b"data: [DONE]\n\n")
        writer.close()

    async def _completion(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        query: dict[str, list[str]],
        headers: Mapping[str, str],
    ) -> None:
        user_id = self._user_of(payload, query, headers)
        raw = payload.get("prompt")
        text = raw if isinstance(raw, str) else " ".join(str(item) for item in (raw or []))
        text = _strip_tavern_prompt(text)
        if not text.strip():
            raise HttpError(400, "prompt 是空的")
        await self._ensure_session(user_id)
        reply_id = f"cmpl-soul-{int(time.time() * 1000) % 10_000_000:07d}"
        created = int(time.time())
        chunks = [piece async for piece in self._generate(user_id, text)]
        body = json.dumps(
            {
                "id": reply_id,
                "object": "text_completion",
                "created": created,
                "model": self._settings.model,
                "choices": [{"index": 0, "text": "".join(chunks), "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        ).encode()
        await _write(
            writer, 200, body, extra={"Content-Type": "application/json", **_CORS_HEADERS}
        )

    async def _generate(self, user_id: str, text: str) -> AsyncIterator[str]:
        """一个用户同一时刻只跑一轮；后来的请求排队，不甩「正在生成中」。"""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                async for piece in self.bot.stream_reply(user_id, text, today=dt.date.today()):
                    yield piece
            except BotError as exc:
                # 引擎的错不能变成 500 吓走酒馆：给一句能看的话
                logger.info("生成失败（已转成台词）：%s", exc.message)
                yield "（我这边卡了一下）" + exc.message
            except PathSafetyError as exc:
                raise HttpError(400, str(exc)) from exc

    async def _ensure_session(self, user_id: str) -> None:
        if user_id in self._opened:
            return
        # 酒馆开局常会并发几个请求：建会话串行，否则同一用户被开两次、后一个盖掉前一个
        lock = self._opening.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id in self._opened:
                return
            await self.bot.open_session(user_id, restore=True)
            self._opened.add(user_id)

    def _user_of(
        self, payload: dict[str, Any], query: dict[str, list[str]], headers: Mapping[str, str]
    ) -> str:
        for source in (
            headers.get("x-mysoulbot-user"),
            (query.get("user") or [""])[0],
            payload.get("user"),
            payload.get("X-MySoulBot-User"),
        ):
            candidate = str(source or "").strip()
            if candidate:
                try:
                    self.storage.user_dir(candidate)  # 越界的 id 在这里就顶回去
                except PathSafetyError as exc:
                    raise HttpError(400, str(exc)) from exc
                return candidate
        return self._settings.default_user_id

    def _models(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self._settings.model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "mysoulbot",
                }
            ],
        }

    def _health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "engine": "MySoulBot",
            "model": self._settings.model,
            "storage": str(self._settings.storage_dir),
            "tools_enabled": self._settings.tools_enabled,
            "rapport_enabled": self._settings.rapport_enabled,
            "extractor_enabled": self._settings.extractor_enabled,
            "requests": self.requests,
        }


# ---------------------------------------------------------------- 传输细节
def _close_quietly(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:  # noqa: BLE001 - 收尾失败不值得吵
        logger.debug("连接关闭异常", exc_info=True)


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


async def _read_body(reader: asyncio.StreamReader, headers: Mapping[str, str]) -> bytes:
    length = int(headers.get("content-length", "0") or 0)
    if length <= 0:
        return b""
    if length > _MAX_BODY_BYTES:
        raise HttpError(413, f"请求体太大（{length} 字节），酒馆历史别整个塞进来")
    return await reader.readexactly(length)


def _json_of(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(400, f"请求体不是合法 JSON：{type(exc).__name__}") from exc
    return loaded if isinstance(loaded, dict) else {}


def _last_user_text(payload: Mapping[str, Any]) -> str:
    """只取最后一条 user 消息：上下文由引擎自己管，客户端历史一律不叠第二份。"""
    for message in reversed(list(payload.get("messages") or [])):
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role")) == "user":
            return _text_of(message.get("content"))
    return ""


def _text_of(content: Any) -> str:  # noqa: ANN401 - OpenAI 的 content 可以是字符串或分段数组
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, Mapping):
                out.append(str(part.get("text", "")))
            else:
                out.append(str(part))
        return " ".join(piece for piece in out if piece).strip()
    return str(content or "").strip()


def _strip_tavern_prompt(text: str) -> str:
    """Text Completion 模式下酒馆会把整个人格提示塞进 prompt。
    我们只认最后一段「{{user}}: …」样式的用户话，其余交给引擎自己的分层。"""
    lines = [line for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        for marker in ("<|user|>", "{{user}}:", "User:", "用户:"):
            if marker in line:
                return line.split(marker, 1)[1].strip()
    return lines[-1].strip() if lines else ""


async def _write(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    *,
    extra: Mapping[str, str] | None = None,
) -> None:
    reason = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
              405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error"}.get(
        status, "OK"
    )
    headers = {"Content-Length": str(len(body)), "Connection": "close", **(extra or {})}
    head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(
        f"{key}: {value}\r\n" for key, value in headers.items()
    ) + "\r\n"
    writer.write(head.encode("latin-1") + body)
    await writer.drain()


async def _stream_head(writer: asyncio.StreamWriter) -> None:
    """SSE 没有 Content-Length：靠 Connection: close 收束，读完即断开，酒馆吃这套。"""
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {_STREAM_MEDIA}\r\n"
        "Cache-Control: no-cache\r\n"
        "X-Accel-Buffering: no\r\n"
        "Connection: close\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in _CORS_HEADERS.items())
        + "\r\n"
    )
    writer.write(head.encode("latin-1"))
    await writer.drain()


async def _write_raw(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    await writer.drain()


async def _sse(
    writer: asyncio.StreamWriter,
    reply_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    *,
    finish: str | None = None,
) -> None:
    payload = {
        "id": reply_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    await _write_raw(writer, b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mysoulbot-server",
        description="MySoulBot 的 OpenAI 兼容本地端点（酒馆 SillyTavern 直连）",
    )
    parser.add_argument("--host", default="", help="默认只绑 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="默认取 SERVER_PORT（11555）")
    parser.add_argument("--public", action="store_true", help="绑 0.0.0.0（会把灵魂和记忆裸露在网段里）")
    parser.add_argument("--verbose", action="store_true", help="输出引擎日志到终端")
    args = parser.parse_args(argv)

    from config import get_settings

    settings = get_settings()
    if args.public:
        settings.server_host = "0.0.0.0"
        logger.warning("已绑定 0.0.0.0：局域网里任何设备都能读写这套灵魂与记忆")
    settings.apply_logging(terminal_info=args.verbose)
    server = SoulServer(settings)
    host = args.host or settings.server_host
    try:
        asyncio.run(_serve(server, host, args.port))
    except KeyboardInterrupt:
        print("已停止。")
    return 0


async def _serve(server: SoulServer, host: str, port: int) -> None:
    bound_host, bound_port = await server.start(host, port)
    print(f"MySoulBot · OpenAI 兼容端点： http://{bound_host}:{bound_port}/v1")
    print(f"  模型名（酒馆里填这个）： {server.settings.model}")
    print("  酒馆 → API 连接：Chat Completion，客户端 = OpenAI，兼容 = 本地")
    try:
        await server.serve_forever()
    finally:
        await server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
