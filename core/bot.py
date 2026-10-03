"""核心对话调度器。

串联「分层存储 → 双层灵魂 Prompt → 流式模型调用 → 静默工具往返 → 日志落盘 → 后台反思抽取」。
支持任意 OpenAI 兼容接口（base_url + api_key 由 config 注入）。

工具往返对用户不可见：模型下单 → 引擎静默执行 → 结果只回到模型的消息列表里 →
模型用角色自己的话说出来。界面上永远只有一段角色表达。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from config import Settings
from core.card_loader import PersonaLibrary, Preset
from core.clawd_soul import ClawdSoul
from core.memory_extractor import MemoryExtractor
from core.prompt_builder import Message, PromptBuilder, PromptLayers, history_from_log_records
from core.storage_manager import StorageManager, StorageError
from core.tools.base import ToolContext
from core.tools.protocol import Directive, StreamGuard
from core.tools.registry import ToolRegistry

logger: Final = logging.getLogger("mysoulbot.bot")

HISTORY_MULTIPLIER: Final[int] = 4
TOOL_TRAIL_ROUNDS: Final[int] = 3


class BotError(RuntimeError):
    """已经过翻译、可直接展示给用户的错误。"""

    def __init__(self, message: str, *, hint: str = "", cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.cause = cause

    def __str__(self) -> str:
        return f"{self.message}（{self.hint}）" if self.hint else self.message


@dataclass
class Session:
    """单个用户的会话状态。"""

    user_id: str
    history: list[Message] = field(default_factory=list)
    turns: int = 0
    busy: bool = False
    last_layers: PromptLayers | None = None
    persona_slug: str = ""
    persona_name: str = ""
    gen_params: dict[str, Any] = field(default_factory=dict)
    tool_mode: str = ""  # ""=自动；接口不认原生工具时被降级成 "inline"
    tool_calls: int = 0

    def append(self, role: str, content: str, cap: int) -> None:
        self.history.append({"role": role, "content": content})
        limit = max(cap, 2) * HISTORY_MULTIPLIER
        if len(self.history) > limit:
            del self.history[: -limit]

    def merged_params(self, settings: Settings) -> dict[str, Any]:
        """全局配置打底，人格级参数覆盖。"""
        params: dict[str, Any] = {
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "max_tokens": settings.max_tokens,
            "frequency_penalty": settings.frequency_penalty,
        }
        params.update(self.gen_params)
        return params


@dataclass
class PersonaApplied:
    """一次人格切换的结果。"""

    slug: str
    name: str
    title: str
    previous_slug: str
    backup_path: Path | None
    greeting: str
    history_reset: bool
    config: dict[str, float] = field(default_factory=dict)


class MySoulBot:
    """对话引擎门面。CLI / 未来的 HTTP 层都只依赖它。"""

    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        prompt_builder: PromptBuilder,
        extractor: MemoryExtractor,
        library: PersonaLibrary | None = None,
        clawd: ClawdSoul | None = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._prompts = prompt_builder
        self._extractor = extractor
        self.library = library or PersonaLibrary(settings)
        self.clawd = clawd or ClawdSoul(settings)
        self._client: AsyncOpenAI | None = None
        self._sessions: dict[str, Session] = {}
        self._today = dt.date.today()

    # ------------------------------------------------------------ 资源
    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def storage(self) -> StorageManager:
        return self._storage

    @property
    def extractor(self) -> MemoryExtractor:
        return self._extractor

    @property
    def model(self) -> str:
        return self._settings.model

    def registry(self, user_id: str) -> ToolRegistry:
        """给某个用户装配可用工具。工具层失败不影响对话——空注册表就是「没有能使的劲」。"""
        if not self._settings.tools_enabled:
            return ToolRegistry(self._context(user_id), [])
        try:
            return ToolRegistry(self._context(user_id))
        except Exception as exc:  # noqa: BLE001 - 工具装配失败只是少点能力
            logger.warning("工具装配失败，本轮无工具可用: %s", exc)
            return ToolRegistry(self._context(user_id), [])

    def _context(self, user_id: str) -> ToolContext:
        return ToolContext(
            settings=self._settings, storage=self._storage, user_id=user_id, clawd=self.clawd
        )

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._settings.api_key or "EMPTY",
                base_url=self._settings.base_url,
                timeout=self._settings.request_timeout,
                max_retries=self._settings.max_retries,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("对话客户端关闭异常", exc_info=True)
            self._client = None

    # ------------------------------------------------------------ 会话
    async def open_session(
        self, user_id: str, *, soul_text: str | None = None, restore: bool = True
    ) -> Session:
        """初始化用户目录并（可选）从日志恢复上下文。"""
        await self._storage.ensure_user(user_id, soul_text=soul_text)
        session = Session(user_id=user_id)
        if restore:
            limit = self._settings.context_max_turns
            try:
                records = await self._storage.read_recent_transcript(user_id, limit)
            except StorageError as exc:
                logger.warning("上下文恢复失败: %s", exc)
                records = []
            session.history = history_from_log_records(records)[-limit:]
            if session.history:
                logger.info("已为 %s 恢复 %d 条上下文", user_id, len(session.history))
        meta = await self._storage.read_persona_meta(user_id)
        session.persona_slug = str(meta.get("slug") or "")
        session.persona_name = str(meta.get("name") or "")
        config = meta.get("config")
        session.gen_params = dict(config) if isinstance(config, dict) else {}
        self._sessions[user_id] = session
        return session

    def session(self, user_id: str) -> Session:
        session = self._sessions.get(user_id)
        if session is None:
            raise BotError(f"会话 {user_id} 未打开")
        return session

    # ------------------------------------------------------------ 人格
    async def apply_persona(
        self, user_id: str, preset: Preset, *, keep_history: bool = False, greet: bool = True
    ) -> PersonaApplied:
        """把人格预设应用到当前用户。

        语义：**记忆与画像保留，SOUL 替换并备份，近期上下文默认重置**。
        理由：MEMORY.md 记录的是「用户」的事实，与谁来扮演不相关，换人不该失忆；
        而旧上下文里满是上一任角色的语气，留着会污染新人格。
        """
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        previous_slug = session.persona_slug
        soul_text = preset.soul_text()
        if not soul_text.strip():
            raise BotError(f"人格 {preset.slug} 的 SOUL.md 是空的，已中止切换")

        backup = await self._storage.backup_doc(user_id, "SOUL")
        await self._storage.write_doc(user_id, "SOUL", soul_text)
        await self._storage.write_persona_meta(
            user_id,
            {
                "schema_version": 1,
                "slug": preset.slug,
                "name": preset.name,
                "title": preset.title,
                "source": preset.source,
                "config": preset.config,
                "first_mes": preset.first_mes,
                "applied_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
        session.persona_slug = preset.slug
        session.persona_name = preset.name
        session.gen_params = dict(preset.config)

        reset = False
        if not keep_history:
            session.history.clear()
            session.turns = 0
            reset = True

        greeting = ""
        if greet and preset.first_mes and (reset or not session.history):
            greeting = preset.first_mes.strip()
            session.append("assistant", greeting, self._settings.context_max_turns)
            await self._storage.append_transcript(
                user_id, [{"role": "assistant", "content": greeting, "kind": "greeting"}]
            )

        logger.info("已为 %s 应用人格 %s（上下文%s）", user_id, preset.slug, "保留" if keep_history else "重置")
        return PersonaApplied(
            slug=preset.slug,
            name=preset.name,
            title=preset.title,
            previous_slug=previous_slug,
            backup_path=backup,
            greeting=greeting,
            history_reset=reset,
            config=dict(preset.config),
        )

    # ------------------------------------------------------------ Prompt
    async def preview_prompt(
        self, user_id: str, *, today: dt.date | None = None, tools: ToolRegistry | None = None
    ) -> tuple[str, PromptLayers]:
        """返回当前将用于对话的 system prompt 与分层明细。"""
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        registry = tools if tools is not None else self.registry(user_id)
        return await self._prompts.build_system_prompt(
            user_id,
            session.history,
            today=today or self._today,
            tool_mode=self._tool_mode(session, registry),
            tools=registry,
        )

    # ------------------------------------------------------------ 对话
    async def stream_reply(
        self,
        user_id: str,
        user_text: str,
        *,
        today: dt.date | None = None,
        speakers: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """流式产出一段角色回复。

        界面上只会看到角色说的话：工具下单被就地剥掉，执行过程完全静默，
        结果只补进送给模型的消息列表。生成器结束时，日志与反思抽取已提交后台。
        """
        session = self._sessions.get(user_id) or await self.open_session(user_id)
        if session.busy:
            raise BotError("上一条回复还在生成中")
        session.busy = True

        day = today or self._today
        registry = self.registry(user_id)
        mode = self._tool_mode(session, registry)
        params = session.merged_params(self._settings)
        guard = StreamGuard(trim_closers=self._settings.trim_stock_closers)
        visible: list[str] = []
        groups: list[list[Message]] = []  # 每组=一次「下单+结果」，永不拆开，避免留下无主的 tool 消息
        rounds = 0
        completed = False
        try:
            rebuild = True
            while True:
                if rebuild:
                    messages, session.last_layers = await self._prompts.build_messages(
                        user_id,
                        user_text,
                        session.history,
                        today=day,
                        tool_mode=mode,
                        tools=registry,
                        speakers=speakers,
                    )
                    rebuild = False
                sink: dict[str, Any] = {"tool_calls": []}
                ordered: list[Directive] = []
                try:
                    async for delta in self._call_stream(
                        messages + [m for group in groups for m in group],
                        params,
                        tools=registry.native_specs() if mode == "native" else None,
                        sink=sink,
                    ):
                        shown, found = guard.feed(delta)
                        ordered.extend(found)
                        if shown:
                            visible.append(shown)
                            yield shown
                except BotError as exc:
                    if self._should_degrade_native(exc, mode, groups, visible):
                        mode = "inline"
                        session.tool_mode = "inline"
                        rebuild = True
                        logger.info("接口不认原生工具调用，本轮改用行内暗号继续")
                        continue
                    if groups or "".join(visible).strip():
                        # 工具结果回填被拒或中途出错：已有内容照旧收尾，不甩机械错误
                        logger.info("工具往返中断，用已有内容收尾：%s", exc.message)
                        break
                    raise
                tail, tail_dirs = guard.flush()
                ordered.extend(tail_dirs)
                if tail:
                    visible.append(tail)
                    yield tail

                calls = self._collect_calls(mode, sink, ordered, registry)
                if not calls:
                    break
                if rounds >= self._settings.tool_max_rounds:
                    logger.info("本轮工具往返已达上限 %d，剩下的单不接", self._settings.tool_max_rounds)
                    break
                rounds += 1
                session.tool_calls += len(calls)
                groups = await self._run_tools(calls, groups, registry, mode)
            completed = True
        finally:
            try:
                await self._finalize(session, user_text, "".join(visible), not completed, day)
            finally:
                session.busy = False
        if completed and not "".join(visible).strip():
            raise BotError(
                "模型没有返回可见内容",
                hint=(
                    "多为推理型模型把 MAX_TOKENS 全部消耗在思考上（本接口会把正文留在 "
                    "reasoning_content 里），或该网关模型已下线。请先调高 MAX_TOKENS，"
                    "或用 tests/probe_models.py 换一个模型。若刚才有工具下单，"
                    "也可能是接口拒绝了回填——可用 /panel tools 关掉工具再试。"
                ),
            )

    @staticmethod
    def _should_degrade_native(
        exc: BotError, mode: str, groups: list[list[Message]], visible: list[str]
    ) -> bool:
        """第一趟就报「不懂 tools」才降级；已有正文或已在回填结果时不再折腾。"""
        if mode != "native" or groups or "".join(visible).strip():
            return False
        cause = exc.cause
        return isinstance(cause, APIStatusError) and cause.status_code in {400, 404, 422, 501}

    def tool_mode(self, user_id: str) -> str:
        """当前会话实际采用的工具形态（none|native|inline），供控制台展示。"""
        session = self._sessions.get(user_id)
        if session is None:
            return "none"
        return self._tool_mode(session, self.registry(user_id))

    def _tool_mode(self, session: Session, registry: ToolRegistry) -> str:
        if not registry or not self._settings.tools_enabled:
            return "none"
        if session.tool_mode in {"native", "inline"}:
            return session.tool_mode
        return "native" if self._settings.tool_native_calling else "inline"

    @staticmethod
    def _collect_calls(
        mode: str, sink: dict[str, Any], ordered: list[Directive], registry: ToolRegistry
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """把这一趟的下单整理成 (call_id, 工具名, 参数)。"""
        if mode == "native" and sink.get("tool_calls"):
            calls: list[tuple[str, str, dict[str, Any]]] = []
            for call in sink["tool_calls"]:
                try:
                    args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append((str(call["id"] or f"call-{len(calls) + 1}"), str(call["name"]), args))
            return calls
        out: list[tuple[str, str, dict[str, Any]]] = []
        for index, directive in enumerate(ordered, start=1):
            tool = registry.resolve(directive.name)
            args = directive.args or (tool.from_bare(directive.raw_args) if tool else {})
            out.append((f"inline-{index}", directive.name, dict(args)))
        return out

    async def _run_tools(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        groups: list[list[Message]],
        registry: ToolRegistry,
        mode: str,
    ) -> list[list[Message]]:
        """静默执行，然后把「下单 + 结果」补成一组消息。

        形态必须跟着降级走：接口刚拒绝了 `tools`，就不能再给它 `tool_calls`/`role:"tool"`
        这种它不认的结构——inline 模式改用普通消息把结果带回去。
        """
        pairs = await registry.call_many((name, call_args) for _, name, call_args in calls)
        if mode == "native":
            announced = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(call_args, ensure_ascii=False),
                    },
                }
                for (call_id, name, call_args), _ in zip(calls, pairs, strict=True)
            ]
            group: list[Message] = [{"role": "assistant", "content": "", "tool_calls": announced}]
            group += [
                {"role": "tool", "tool_call_id": call["id"], "content": result.content}
                for call, (_, result) in zip(announced, pairs, strict=True)
            ]
        else:
            notes = "\n\n".join(
                f"（内部结果，不是对方说的话）{name}：{result.content}"
                for (_, name, _), (_, result) in zip(calls, pairs, strict=True)
            )
            group = [{"role": "user", "content": notes}]
        # 整组进出：绝不留下没有下单头的 tool 消息，那会让严格网关直接 400
        return (groups + [group])[-TOOL_TRAIL_ROUNDS:]

    async def _call_stream(
        self,
        messages: list[Message],
        params: dict[str, Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        sink: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": float(params["temperature"]),
            "top_p": float(params["top_p"]),
            "max_tokens": int(params["max_tokens"]),
            "frequency_penalty": float(params["frequency_penalty"]),
            "stream": True,
            "timeout": self._settings.request_timeout,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            stream = await self._get_client().chat.completions.create(**kwargs)
        except APIStatusError as exc:
            raise self._translate_status_error(exc) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise self._translate_connection_error(exc) from exc
        except OpenAIError as exc:
            raise BotError(f"SDK 错误：{type(exc).__name__}", cause=exc) from exc

        try:
            async for chunk in stream:
                if sink is not None:
                    self._absorb_tool_calls(chunk, sink)
                delta = self._delta_text(chunk)
                if delta:
                    yield delta
        except APIStatusError as exc:
            raise self._translate_status_error(exc) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise self._translate_connection_error(exc) from exc
        finally:
            await self._close_stream(stream)

    @staticmethod
    def _absorb_tool_calls(chunk: Any, sink: dict[str, Any]) -> None:
        """把流式分片里的 tool_calls 按 index 拼回完整下单。"""
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        delta = getattr(choices[0], "delta", None)
        fragments = getattr(delta, "tool_calls", None) or []
        if not fragments:
            return
        bucket: dict[int, dict[str, Any]] = sink.setdefault("by_index", {})
        for position, fragment in enumerate(fragments):
            index = int(getattr(fragment, "index", position) or 0)
            slot = bucket.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = getattr(fragment, "id", None)
            if call_id:
                slot["id"] = str(call_id)
            function = getattr(fragment, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] = str(function.name)
                if getattr(function, "arguments", None):
                    slot["arguments"] += str(function.arguments)
        sink["tool_calls"] = [bucket[key] for key in sorted(bucket)]

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        closer = getattr(stream, "close", None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 流关闭失败不值得打断退出流程
                logger.debug("流式连接关闭异常", exc_info=True)

    @staticmethod
    def _delta_text(chunk: Any) -> str:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None) if delta else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # 少数兼容接口返回分段
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return ""

    async def _finalize(
        self,
        session: Session,
        user_text: str,
        reply: str,
        interrupted: bool,
        day: dt.date,
    ) -> None:
        """把这一轮写入历史/日志，并提交后台抽取。"""
        text = reply.strip()
        if not text:
            if interrupted:
                logger.info("%s 回复被中断且无内容，本回合不落盘", session.user_id)
            else:
                logger.warning("模型返回空内容")
            return

        suffix = "（回复被中断）" if interrupted else ""
        session.append("user", user_text, self._settings.context_max_turns)
        session.append("assistant", text + suffix, self._settings.context_max_turns)
        session.turns += 1

        await self._storage.append_transcript(
            session.user_id,
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text + suffix, "interrupted": interrupted},
            ],
        )

        window = session.history[-self._settings.extractor_lookback_turns :]
        self._extractor.submit(session.user_id, window, today=day)

    # ------------------------------------------------------------ 维护
    async def flush_extractions(self, timeout: float = 60.0) -> int:
        """等待后台抽取排空，返回剩余积压数（CLI 退出或 /sync 时使用）。"""
        return await self._extractor.wait_idle(timeout)

    def reset_history(self, user_id: str) -> None:
        session = self._sessions.get(user_id)
        if session is not None:
            session.history.clear()
            session.turns = 0
            logger.info("%s 上下文已清空（人格与记忆未受影响）", user_id)

    # ------------------------------------------------------------ 错误翻译
    @staticmethod
    def _translate_status_error(exc: APIStatusError) -> BotError:
        code = exc.status_code
        detail = str(exc)[:300]
        table: dict[int, tuple[str, str]] = {
            400: ("请求被接口拒绝", f"检查 MODEL / 参数是否被该接口支持：{detail}"),
            401: ("API_KEY 无效或未授权", "核对 .env 中的 API_KEY，注意有的服务用 sk- 前缀之外的格式"),
            402: ("账户余额不足", "到模型服务商处充值后重试"),
            403: ("该 Key 无权访问此模型", "确认服务商已开通目标模型"),
            404: ("接口路径或模型不存在", f"BASE_URL 应含 /v1 之类的版本路径，MODEL 需与服务商一致：{detail}"),
            410: ("该模型已下线", "中转站常按 end-of-life 直接返回 410，用 tests/probe_models.py 重新选模型"),
            429: ("触发限流", "稍后重试，或调低并发 / 更换 Key"),
            500: ("服务端内部错误", "稍后重试"),
            502: ("网关错误", "服务不可用，稍后重试"),
            503: ("服务过载或不可用", "稍后重试"),
        }
        message, hint = table.get(code, (f"接口返回 HTTP {code}", "查看日志中的原始错误"))
        return BotError(message, hint=hint, cause=exc)

    def _translate_connection_error(self, exc: BaseException) -> BotError:
        if isinstance(exc, APITimeoutError):
            return BotError(
                "请求超时",
                hint=f"可提高 REQUEST_TIMEOUT（当前 {int(self._settings.request_timeout)}s）或改用更快的服务",
                cause=exc,
            )
        return BotError(
            "无法连接到接口",
            hint=f"确认 {self._settings.base_url} 可访问、服务已启动、代理未拦截",
            cause=exc,
        )
