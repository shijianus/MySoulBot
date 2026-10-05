"""OpenAI 兼容的本地服务：让酒馆（SillyTavern）直连本机 MySoulBot。

为什么手搓 HTTP：本环境没有 aiohttp/fastapi，而这一层只需要几个路由。
标准库 `asyncio.start_server` 就够，且 clone 即用、零新依赖。

路由：
- `POST /v1/chat/completions` —— 流式（SSE）与非流式都支持，酒馆 Chat Completion 直接可用
- `POST /v1/completions`      —— 老式 Text Completion 模式
- `POST /voice/say`           —— 把说出口的话念成一段音频（Web 伴侣端的播放条；纯渲染）
- `GET  /v1/models`           —— 酒馆探活要看的模型列表
- `GET  /healthz`             —— 引擎状态（模型、存储目录、是否挂工具、语音走哪条路）
- `GET  /panel`               —— 沉浸 Web 面板（纯静态 HTML/CSS/JS，无打包）
- `GET  /api/state|/api/timeline|/api/docs/<档>` —— 面板的只读数据
- `GET  /media/<用户>/<文件>` —— 工具产物（生成的图、拍下的屏）的静态查看链接
- `GET  /media/audio/<用户>/<文件>` —— 念出来的声音

外加一条**非 HTTP** 的入口：`ONEBOT_ENABLED=true` 时另开 `ONEBOT_PORT`，
QQ 那侧的协议端反向连进来（`core/adapters/qq_onebot.py`），用的是同一具灵魂、同一份记忆。

**面板只读**：`/api/*` 与 `/panel*` 只接 GET，其余一律 405。熟络度没有写入口——
温度只能由引擎在真实回合里攒，网页上一个 PATCH 按钮都不长。`/voice/say` 能收 POST，
是因为它只出声不记事：不建会话、不进上下文、不碰 `state.json`。

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
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlparse

from config import Settings
from core import panel
from core.adapters.qq_onebot import OneBotBridge
from core.bot import BotError, MySoulBot
from core.card_loader import PersonaLibrary
from core.clawd_soul import ClawdSoul
from core.memory_extractor import MemoryExtractor
from core.prompt_builder import PromptBuilder
from core.storage_manager import PathSafetyError, StorageManager
from core.tools.voice import VoiceError, audio_sniff, provider_of, synthesize
from core.vision import sniff

logger: Final = logging.getLogger("mysoulbot.server")

_MAX_BODY_BYTES: Final[int] = 12_000_000
_STREAM_MEDIA: Final[str] = "text/event-stream"
_CORS_HEADERS: Final[dict[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type,authorization,x-mysoulbot-user",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}
_PANEL_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "web" / "panel"
_ASSET_MEDIA: Final[dict[str, str]] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
_MEDIA_MEDIA: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}
# 音频单独一档：`vision.sniff` 见 RIFF 就认 WEBP，拿它验 .wav 会被伪装的图骗过去
_AUDIO_MEDIA: Final[dict[str, str]] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}
_VOICE_MAX_CONCURRENT: Final[int] = 2
_READ_ONLY_PREFIXES: Final[tuple[str, ...]] = ("/api", "/panel", "/media")
_PANEL_FILES: Final[frozenset[str]] = frozenset({"index.html", "app.css", "app.js"})


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
        # QQ 那侧的入口：只有 ONEBOT_ENABLED=true 才存在，存在就与酒馆共用同一个 bot 实例
        self.onebot: OneBotBridge | None = None
        self._assets: dict[str, tuple[bytes, str]] = {}
        self.requests = 0
        self.active = 0  # 正在出话的回合数：停机前要等它归零
        self._voice_running = 0  # 同时在念的句数：合成是 CPU 活，不能由着界面敞开灌

    @property
    def settings(self) -> Settings:
        return self._settings

    # ------------------------------------------------------------ 生命周期
    async def start(self, host: str = "", port: int | None = None) -> tuple[str, int]:
        host = host or self._settings.server_host
        # 0 是「给我随机端口」的合法意思，不能被当成「没填」而回落成配置端口
        port = self._settings.server_port if port is None else port
        await self.clawd.ensure()
        if self.extractor.enabled:
            self.extractor.start()
        self._server = await asyncio.start_server(self._handle, host, port)
        bound = self._server.sockets[0].getsockname() if self._server.sockets else (host, port)
        logger.info("酒馆兼容端点已监听 http://%s:%s/v1", bound[0], bound[1])
        if self._settings.onebot_enabled:
            bridge = OneBotBridge(self._settings, self.bot)
            qq_host, qq_port = await bridge.start()
            self.onebot = bridge
            logger.info("QQ 网桥已就绪：协议端反向连 ws://%s:%s", qq_host, qq_port)
        return str(bound[0]), int(bound[1])

    async def stop(self) -> None:
        if self.onebot is not None:
            # 先跟协议端挥手，再关抽取与模型客户端：话说一半被掐断是最难看的收场
            await self.onebot.stop()
            self.onebot = None
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
        if method.upper() == "GET" and await self._panel_route(writer, path, query):
            return
        if any(path.rstrip("/").startswith(prefix) for prefix in _READ_ONLY_PREFIXES):
            raise HttpError(405, "这一页只许看，不许改")
        if method.upper() != "POST":
            if path.rstrip("/").startswith(("/v1", "/panel", "/api", "/media")):
                raise HttpError(404, f"没有这个路径：{path}")
            raise HttpError(405, f"这个路径不接 {method}")
        payload = _json_of(body)
        if path.rstrip("/") == "/v1/chat/completions":
            await self._chat(writer, payload, query, headers)
            return
        if path.rstrip("/") == "/v1/completions":
            await self._completion(writer, payload, query, headers)
            return
        if path.rstrip("/") == "/voice/say":
            await self._say(writer, payload, query, headers)
            return
        raise HttpError(404, f"没有这个路径：{path}")

    # ------------------------------------------------------------ 面板与产物
    async def _panel_route(
        self, writer: asyncio.StreamWriter, path: str, query: dict[str, list[str]]
    ) -> bool:
        """只读面板的全部 GET 路由；命中返回 True。"""
        clean = path.rstrip("/")
        if not self._settings.panel_enabled:
            if clean.startswith("/panel") or clean.startswith("/api"):
                raise HttpError(404, "面板没开着（PANEL_ENABLED=false）")
            return False
        if clean in ("/panel", ""):
            await self._send_asset(writer, "index.html")
            return True
        if clean.startswith("/panel/"):
            await self._send_asset(writer, clean[len("/panel/") :] or "index.html")
            return True
        if clean == "/api/state":
            await self._send_json(writer, await self._state_of(query))
            return True
        if clean == "/api/approvals":
            # 只读列工单：面板按设计没有任何写入口，批准这一下留在命令行上由人来做
            from core.sandbox import ApprovalDesk

            desk = ApprovalDesk(self._settings)
            items = desk.list()
            await self._send_json(writer, {
                "ok": True,
                "pending": [item.human() for item in items if item.state == "pending"],
                "total": len(items),
                "decided": [item.human() for item in items if item.state != "pending"][-20:],
            })
            return True
        if clean == "/api/timeline":
            view = await panel.build_timeline(
                self.storage, self._user_for_panel(query), limit=panel.TIMELINE_LIMIT
            )
            await self._send_json(writer, view)
            return True
        if clean.startswith("/api/docs/"):
            doc = clean[len("/api/docs/") :].upper()
            if doc not in panel.DOCS:
                raise HttpError(404, f"面板没有这一份：{doc}")
            view = await panel.build_doc(self.storage, self._user_for_panel(query), doc)
            await self._send_json(writer, view)
            return True
        if clean.startswith("/media/audio/"):
            await self._send_audio(writer, clean[len("/media/audio/") :])
            return True
        if clean.startswith("/media/"):
            await self._send_media(writer, clean[len("/media/") :])
            return True
        return False

    def _user_for_panel(self, query: dict[str, list[str]]) -> str:
        candidate = (query.get("user") or [""])[0].strip()
        if not candidate:
            return self._settings.default_user_id
        try:
            self.storage.user_dir(candidate)
        except PathSafetyError as exc:
            raise HttpError(400, str(exc)) from exc
        return candidate

    async def _state_of(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return await panel.build_status(self._settings, self.storage, self._user_for_panel(query))

    async def _send_json(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        await _write(
            writer,
            200,
            json.dumps(payload, ensure_ascii=False).encode(),
            extra={"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
        )

    async def _send_asset(self, writer: asyncio.StreamWriter, name: str) -> None:
        """静态资源：读一次进内存，之后不碰磁盘。只认白名单文件名。"""
        if name not in _PANEL_FILES:
            raise HttpError(404, "面板里没有这个文件")
        cached = self._assets.get(name)
        if cached is None:
            path = _PANEL_DIR / name
            if not path.is_file():
                raise HttpError(404, f"面板缺文件：{name}")
            data = await asyncio.to_thread(path.read_bytes)
            cached = (data, _ASSET_MEDIA[Path(name).suffix])
            self._assets[name] = cached
        body, media = cached
        await _write(
            writer, 200, body, extra={"Content-Type": media, "Cache-Control": "no-cache", **_CORS_HEADERS}
        )

    async def _send_media(self, writer: asyncio.StreamWriter, rest: str) -> None:
        """工具产物（画来的、拍来的）的静态查看链接。"""
        await self._send_artifact(writer, rest, audio=False)

    async def _send_audio(self, writer: asyncio.StreamWriter, rest: str) -> None:
        """念出来的声音。片段短（默认上限 24 秒），整份发出去就够，不谎称支持 Range。"""
        await self._send_artifact(writer, rest, audio=True)

    async def _send_artifact(
        self, writer: asyncio.StreamWriter, rest: str, *, audio: bool
    ) -> None:
        """只准读本用户产物目录里的那一个文件：后缀要在清单里，内容还要与后缀对得上。"""
        user_id, _, name = rest.partition("/")
        name = unquote(name)
        try:
            directory = (
                self.storage.audio_dir(user_id)
                if audio
                else self.storage.user_dir(user_id) / "artifacts"
            )
        except PathSafetyError as exc:
            raise HttpError(400, str(exc)) from exc
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise HttpError(400, "只能取产物目录里的单个文件")
        allow = _AUDIO_MEDIA if audio else _MEDIA_MEDIA
        suffix = Path(name).suffix.lower()
        if suffix not in allow:
            raise HttpError(415, f"这东西不在可查看的清单里：{suffix or '没有后缀'}")
        path = (directory / name).resolve()
        if path.parent != directory.resolve() or not path.is_file():
            raise HttpError(404, "没有这个产物")
        data = await asyncio.to_thread(path.read_bytes)
        media = allow[suffix]
        if media.startswith("image/") and not sniff(data):
            raise HttpError(415, "这文件的后缀和内容不一致")
        if media.startswith("audio/") and not audio_sniff(data):
            raise HttpError(415, "这东西不是能播的声音")
        await _write(
            writer,
            200,
            data,
            extra={"Content-Type": media, "Content-Disposition": "inline", **_CORS_HEADERS},
        )

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
        shots = _last_user_images(payload)
        if not text and not shots:
            raise HttpError(400, "消息里没有用户说的话")
        await self._ensure_session(user_id)
        # 客户端送来的采样参数一律忽略：人格的节奏不是表单能改的
        logger.info("酒馆请求 · user=%s stream=%s 图=%d 参数忽略=%s", user_id,
                    bool(payload.get("stream")), len(shots),
                    sorted(k for k in payload if k in {"temperature", "max_tokens", "top_p", "frequency_penalty"}))
        reply_id = f"chatcmpl-soul-{int(time.time() * 1000) % 10_000_000:07d}"
        created = int(time.time())
        model = self._settings.model

        if not payload.get("stream"):
            chunks = [piece async for piece in self._generate(user_id, text, shots)]
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
        streaming = self._generate(user_id, text, shots)
        try:
            async for piece in streaming:
                await _sse(writer, reply_id, created, model, {"content": piece})
        finally:
            # 浏览器切后台、手机锁屏都会把写入打断在这一句上：不显式关掉生成器，
            # 这一用户的回合锁与 busy 标记要一直占到垃圾回收，之后再也发不出话。
            await streaming.aclose()
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

    async def _say(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        query: dict[str, list[str]],
        headers: Mapping[str, str],
    ) -> None:
        """把说出口的话念成一段能播的声音。

        这是纯渲染的一条路：不建会话、不进上下文、一个字都不碰 state.json——
        温度只能由相处攒出来，「让她出声」不该有任何改写关系动态的副作用。
        失败收敛成 `{ok:false}`：界面上什么都不挂，而不是冒一个点得响的红叉。
        """
        user_id = self._user_of(payload, query, headers)
        if not self._settings.tts_enabled or provider_of(self._settings) == "none":
            await self._send_json(writer, {"ok": False, "reason": "这条路没接声音"})
            return
        text = str(payload.get("text") or "")
        if not text.strip():
            raise HttpError(400, "没有要说出口的话")
        if self._voice_running >= _VOICE_MAX_CONCURRENT:
            await self._send_json(writer, {"ok": False, "reason": "正念着上一句，稍等一下"})
            return
        self._voice_running += 1
        try:
            clip = await synthesize(text, self._settings, self.storage, user_id)
        except VoiceError as exc:
            await self._send_json(writer, {"ok": False, "reason": str(exc)})
            return
        except PathSafetyError as exc:
            raise HttpError(400, str(exc)) from exc
        finally:
            self._voice_running -= 1
        await self._send_json(writer, {"ok": True, **clip.as_dict()})

    async def _generate(
        self, user_id: str, text: str, images: list[str] | None = None
    ) -> AsyncIterator[str]:
        """一个用户同一时刻只跑一轮；后来的请求排队，不甩「正在生成中」。"""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            self.active += 1
            try:
                async for piece in self.bot.stream_reply(
                    user_id, text, today=dt.date.today(), images=images or []
                ):
                    yield piece
            except BotError as exc:
                # 引擎的错不能变成 500 吓走酒馆：给一句能看的话
                logger.info("生成失败（已转成台词）：%s", exc.message)
                yield "（我这边卡了一下）" + exc.message
            except PathSafetyError as exc:
                raise HttpError(400, str(exc)) from exc
            finally:
                self.active -= 1

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
            "vision_enabled": self._settings.vision_enabled,
            "vision_model": self._settings.effective_vision_model,
            "vision_max_images": self._settings.vision_max_images,
            "tts_enabled": self._settings.tts_enabled,
            "tts_provider": provider_of(self._settings),
            "panel_enabled": self._settings.panel_enabled,
            "requests": self.requests,
            "active_turns": self.active,
            "memory_backlog": self.bot.extractor.backlog,
            "upstreams": self.bot.routes.view(),
            "onebot": (
                self.onebot.status()
                if self.onebot is not None
                else {"enabled": self._settings.onebot_enabled, "listening": False}
            ),
        }

    # ------------------------------------------------------------ 停机
    async def close_listener(self) -> None:
        """先停止接单，让在途的回合自己说完。"""
        if self.onebot is not None:
            await self.onebot.close_listener()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def drain(self, timeout: float | None = None) -> dict[str, Any]:
        """优雅退出前的一道等：先看在途回合说完，再等内存里的记忆任务落盘。

        后台抽取是 fire-and-forget 的——进程被直接掐掉就会丢掉那一口「刚攒下的事实」。
        这里等到队列空为止；超时就如实报出还欠多少，让守护层把数字留在日志里。
        QQ 那头的回合也算回合：酒馆空闲不代表网桥也空闲。
        """
        grace = timeout if timeout is not None else self._settings.drain_timeout_seconds
        deadline = time.monotonic() + grace
        while self._in_flight() > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        turns_left = self._in_flight()
        backlog = await self.bot.flush_extractions(max(1.0, deadline - time.monotonic()))
        return {"turns_left": turns_left, "backlog": backlog}

    def _in_flight(self) -> int:
        """正在出话的回合数，两个入口一起算：只盯酒馆会把 QQ 那一半漏在停机之外。"""
        return self.active + (self.onebot.active_turns if self.onebot is not None else 0)


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


def _last_user_images(payload: Mapping[str, Any]) -> list[str]:
    """酒馆发图走的是同一条通道：取最后一条 user 消息里的图片分段。

    返回的是来路（`data:image/...;base64,` 或 http 链接），落盘与格式校验
    由 `core.vision` 统一做——服务层不自己解 base64，两处解法迟早会分叉。
    """
    for message in reversed(list(payload.get("messages") or [])):
        if not isinstance(message, Mapping) or str(message.get("role")) != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            return []
        found: list[str] = []
        for part in content:
            if not isinstance(part, Mapping) or str(part.get("type")) != "image_url":
                continue
            link = part.get("image_url")
            url = str((link or {}).get("url", "") if isinstance(link, Mapping) else link or "").strip()
            if url:
                found.append(url)
        return found
    return []


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
        description="MySoulBot 的 OpenAI 兼容本地端点（酒馆 SillyTavern 直连 + 沉浸面板）",
    )
    parser.add_argument("--host", default="", help="默认只绑 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="默认取 SERVER_PORT（11555）")
    parser.add_argument("--public", action="store_true", help="绑 0.0.0.0（会把灵魂和记忆裸露在网段里）")
    parser.add_argument("--verbose", action="store_true", help="输出引擎日志到终端")
    parser.add_argument(
        "--grace", type=float, default=0.0, help="停机时等在途记忆落盘的秒数（默认取配置）"
    )
    args = parser.parse_args(argv)

    from config import get_settings

    settings = get_settings()
    if args.public:
        settings.server_host = "0.0.0.0"
        logger.warning("已绑定 0.0.0.0：局域网里任何设备都能读写这套灵魂与记忆")
    settings.apply_logging(terminal_info=args.verbose)
    server = SoulServer(settings)
    host = args.host or settings.server_host
    grace = args.grace or settings.drain_timeout_seconds
    try:
        asyncio.run(_serve(server, host, args.port, grace))
    except KeyboardInterrupt:
        print("已停止。")
    return 0


async def _serve(server: SoulServer, host: str, port: int, grace: float) -> None:
    bound_host, bound_port = await server.start(host, port)
    print(f"MySoulBot · OpenAI 兼容端点： http://{bound_host}:{bound_port}/v1")
    print(f"  沉浸面板： http://{bound_host}:{bound_port}/panel")
    print(f"  模型名（酒馆里填这个）： {server.settings.model}")
    print("  酒馆 → API 连接：Chat Completion，客户端 = OpenAI，兼容 = 本地")
    serve_task = asyncio.create_task(server.serve_forever())
    if server.onebot is not None:
        state = server.onebot.status()
        bound = state.get("bound") or {}
        print(f"  QQ 网桥（反向 WS，等协议端连进来）： ws://{bound.get('host')}:{bound.get('port')}")
        print(
            "    鉴权："
            + ("已配 ONEBOT_ACCESS_TOKEN" if state.get("authenticated") else "未配 token（只绑了回环）")
        )
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    try:
        await stop.wait()
    finally:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task
        await server.close_listener()
        settled = await server.drain(grace)
        await server.stop()
        if settled["backlog"] or settled["turns_left"]:
            logger.warning(
                "停机时仍有欠账：在途回合 %d、未落盘记忆 %d",
                settled["turns_left"],
                settled["backlog"],
            )
        else:
            logger.info("停机前已排空：在途回合归零，记忆全部落盘")


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT 都走同一条收尾路径——`stop` 是 systemd 的默认信号，不能当异常处理。"""
    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, stop.set)


if __name__ == "__main__":
    raise SystemExit(main())
