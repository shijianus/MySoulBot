"""MySoulBot 交互式终端（CLI）。

这里只有一条界面律：**主气泡里只允许出现角色说的话**。
监控条、Prompt 转储、HTTP 状态、记忆落盘弹窗——这些不属于对话，全部收进 `/panel` 控制台，
由你自己呼出；群聊场景下则整套锁死，任何参数与系统状态都调不出来。

用法：
    python main.py                        # 1V1，直接输入就是说话
    python main.py --user alice           # 指定用户（人格/画像/记忆完全隔离）
    python main.py --group 阿哲 老周       # 群聊：消息行首写「名字: 内容」
    python main.py --once "你好"          # 单轮，便于脚本冒烟
    python main.py --panel                # 启动即打开控制台
    python main.py --no-immersive         # 恢复角色标签与逐条系统提示（调试观感）

命令：
    /help            看能做什么
    /panel <子命令>   控制台（人格、参数、Prompt、记忆、工具、状态都在这里）
    /panel web       沉浸面板地址：手机或浏览器里看她此刻与记忆时光机
    /mode <模式>      solo（1V1）/ group（群聊，锁死控制台）
    /sync remote     把灵魂与记忆推到远端仓库
    /quit            退出

发一句话时带上图片路径、图片链接或 base64，她就会真的看见那张图（VISION_ENABLED）。
常驻与优雅停机：bash scripts/daemon.sh start|stop|restart（酒馆端点与面板共用 11555）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Final

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import Settings, get_settings
from core.bot import BotError, MySoulBot
from core.card_loader import CardError, PersonaLibrary, PresetError
from core.clawd_soul import ClawdSoul
from core.identity import PairingDesk, consume_pairing, format_code, read_owner, unpair
from core.pair_phrase import make_greeting, make_phrase, short_ask, the_box
from core.memory_extractor import MemoryExtractor
from core.prompt_builder import PromptBuilder
from core.storage_manager import (
    DocName,
    PathSafetyError,
    StorageError,
    StorageManager,
    parse_facts,
)
from core.sync import run_sync
from core.vector_index import VectorIndex
from core.vision import find_sources

logger: Final = logging.getLogger("mysoulbot.cli")

EDIT_TARGETS: Final[dict[str, DocName]] = {"soul": "SOUL", "user": "USER", "memory": "MEMORY"}
SOURCE_LABEL: Final[dict[str, str]] = {"builtin": "内置", "tavern": "酒馆卡", "template": "模板"}
DEFAULT_FLUSH_SECONDS: Final[float] = 30.0

# 群聊里一律拒绝的命令：任何能改参数、换人格、碰存储、露系统状态的入口都在这张表里。
# /help、/mode（逃生用）、/quit 是唯一放行的三个。
GROUP_LOCKED: Final[frozenset[str]] = frozenset(
    {
        "panel", "persona", "char", "model", "edit", "append", "note", "user", "sync",
        "tools", "tool", "debug", "clear", "status", "prompt", "memory", "facts", "soul",
        "clawd", "profile", "whoami", "archive", "audit", "log", "relations", "dynamics",
        "rapport", "温度", "rhythm", "体温",
    }
)
# 已经搬进控制台的旧命令：给一行「去哪儿找」，而不是把调试信息摊在对话里
MOVED: Final[dict[str, str]] = {
    "status": "/panel status",
    "prompt": "/panel prompt",
    "memory": "/panel memory",
    "facts": "/panel memory",
    "soul": "/panel soul",
    "profile": "/panel soul profile",
    "whoami": "/panel whoami",
    "clear": "/panel clear",
    "model": "/panel model",
    "persona": "/panel persona",
    "char": "/panel persona",
    "edit": "/panel edit",
    "append": "/panel append",
    "debug": "/panel debug",
}
_SPEAKER: Final[str] = "  ▸ "


PROMPT: Final[str] = "admin ▸ "
# 角色说的一律浅色、不带前缀；提示符只属于「正在等你输入」那一刻。
ROLE_STYLE: Final[str] = "grey84"


class TerminalUI:
    """渲染层：角色表达（沉浸）与控制台输出（诊断）严格分家。

    界面上只有一种前缀：`admin ▸ ` —— 它只在**等键盘**的那一刻出现。
    她说的话、系统提示、报错都不带前缀，角色那几行是浅灰的。
    """

    def __init__(self, settings: Settings, console: Console | None = None) -> None:
        self.console: Console = console or Console(highlight=False)
        self._settings = settings
        self._role_style = ROLE_STYLE
        self._stream_open = False
        self._chars = 0

    # ------------------------------------------------------------ 观感开关
    @property
    def immersive(self) -> bool:
        return self._settings.immersive

    def short_path(self, path: Path | str) -> str:
        target = Path(path)
        for base in (self._settings.storage_dir.parent, Path.cwd()):
            try:
                return f"./{target.relative_to(base)}"
            except ValueError:
                continue
        return str(target)

    # ------------------------------------------------------------ 开场
    def banner(self, user_id: str, persona: str) -> None:
        """1V1 才报状态；群聊只留一行，别让人看见后台。"""
        if self._settings.group_mode:
            self.line("群聊已就位。我在场，但不抢话。", style="dim")
            return
        body = "\n".join(
            [
                f"在跟谁说话  {persona or '（SOUL.md 现内容）'}",
                f"当前用户    {user_id}",
                "看见 admin ▸ 就是等你：直接打字就是说话。想看后台：/panel",
            ]
        )
        self.console.print(
            Panel(Text(body), border_style="cyan", title="MySoulBot", title_align="left")
        )

    def line(self, text: str, *, style: str = "dim") -> None:
        """系统级的一行字。沉浸模式下保持极轻，不进角色气泡。"""
        self.console.print(Text(text, style=style))

    def warn(self, text: str) -> None:
        self.console.print(Text(f" ! {text}", style="yellow"))

    def error(self, text: str, hint: str = "") -> None:
        """默认只给一句「没说出来」；状态码与 hint 都要开 diagnostics 才看得见。"""
        if self._settings.diagnostics:
            self.console.print(Text(f" ✗ {text}", style="bold red"))
            if hint:
                self.console.print(Text(f"   {hint}", style="red"))
        else:
            self.console.print(Text(f" （这句没说出口：{_plain(text)}）", style="dim"))

    def block(self, title: str, content: str) -> None:
        """控制台输出：markdown 文档、人格正文。"""
        self.console.print(
            Panel(
                Markdown(content or "（空）"),
                title=title,
                title_align="left",
                border_style="blue",
                padding=(0, 2),
            )
        )

    def raw(self, title: str, content: str, style: str = "white") -> None:
        self.console.print(Text(f"── {title} " + "─" * max(4, 56 - len(title)), style="dim"))
        self.console.print(Text(content or "（空）", style=style))

    def table(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        body = Table(title=title, title_style="cyan", header_style="dim", show_header=False)
        body.add_column("项", style="bold")
        body.add_column("值")
        for key, value in rows:
            body.add_row(key, value)
        self.console.print(body)

    # ------------------------------------------------------------ 流式：角色的声音
    def begin_stream(self) -> None:
        self._stream_open = True
        self._chars = 0

    def push(self, delta: str) -> None:
        if not self._stream_open:
            self.begin_stream()
        self._chars += len(delta)
        self.console.print(Text(delta, style=self._role_style), end="", markup=False, soft_wrap=True)
        self.console.file.flush()

    def end_stream(self) -> bool:
        if not self._stream_open:
            return False
        self.console.print()
        self._stream_open = False
        return self._chars > 0

    def role_block(self, text: str, tag: str = "") -> None:
        """非流式输出角色台词（开场白）。浅色、无前缀。"""
        self.console.print(Text(text, style=self._role_style))

    def ask(self, prompt_text: str = PROMPT) -> str | None:
        try:
            return self.console.input(prompt_text, markup=False)
        except EOFError:
            return None


class _StdinPump:
    """一根常驻 daemon 线程读键盘；**提示符只在被放行那一刻打出来**。

    两件事都要办到：
      · 不能每次输入都新建一根读线程 —— Ctrl-C 之后老线程还堵在 `input()` 里，
        而线程池的线程解释器退出时要一根根 join，那就成了「按了 Ctrl-C 退不掉，
        再按一次甩 Exception ignored」。daemon 线程不参与那个 join。
      · 但也不能刚读完就立刻再打提示符 —— 那样上一轮的回话会盖在提示符后面，
        看着就是「等输入却没有光标前那串 admin ▸」。所以每一轮由主循环 `arm()` 放行，
        线程打印 `admin ▸ ` 然后等键盘；没放行就一句话都不打。
    """

    def __init__(self, ui: "TerminalUI") -> None:
        self._ui = ui
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._go = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._pump, daemon=True, name="stdin")
        self._thread.start()

    def arm(self) -> None:
        """轮到键盘了：这会儿除了提示符，谁都不该往屏幕上写。"""
        self._go.set()

    def _push(self, line: str | None) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):      # 循环已经关了就别再喊
            loop.call_soon_threadsafe(self._queue.put_nowait, line)

    def _pump(self) -> None:
        while True:
            self._go.wait()
            self._go.clear()
            try:
                line = self._ui.ask()
            except EOFError:
                self._push(None)                     # Ctrl-D：到此为止
                return
            except KeyboardInterrupt:
                # Ctrl-C 是「这一行作废」，不是退出：把空行交回去，等下一次放行
                self._push("")
                continue
            self._push(line if line is not None else "")

    async def get(self) -> str | None:
        return await self._queue.get()


class App:
    """CLI 应用：装配引擎、分发命令、对话主循环。"""

    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self._settings = settings
        self._args = args
        self.user_id: str = args.user or settings.default_user_id
        self.ui = TerminalUI(settings)
        self._stdin: _StdinPump | None = None   # 读键盘的那根常驻线程
        self._busy = False                      # Ctrl-C 落在这上面时该掐哪一样
        self._turn_task: asyncio.Task[None] | None = None
        self._last_sigint = 0.0
        self.storage = StorageManager(settings)
        self.clawd = ClawdSoul(settings)
        self.prompts = PromptBuilder(settings, self.storage, self.clawd)
        self.library = PersonaLibrary(settings)
        # 一份向量索引，写入端（抽取器）和检索端（提示词装配）共用：
        # 两边各开一个就会出现「写了但查的是另一个库」这种丢召回
        self.vector = VectorIndex(settings)
        self.extractor = MemoryExtractor(settings, self.storage, on_outcome=self._on_outcome,
                                         index=self.vector)
        self.bot = MySoulBot(settings, self.storage, self.prompts, self.extractor, self.library, self.clawd)
        self.prompts.bind_vector(self.vector)
        # 配对台账：只有人在命令行上敲 /pair 才会开挑战，她没有任何路径能自己开
        self.pairing = PairingDesk(settings)
        self._quiet_events: list[str] = []  # 后台抽取完成的通知，只进 /panel log，不打扰对话

    # ------------------------------------------------------------ 生命周期
    async def setup(self) -> None:
        soul_text: str | None = None
        if self._args.soul_file:
            try:
                soul_text = await asyncio.to_thread(_read_text, self._args.soul_file)
            except OSError as exc:
                raise BotError(f"无法读取人格文件 {self._args.soul_file}", hint=str(exc)) from exc
        await self.clawd.ensure()
        await self.bot.open_session(self.user_id, soul_text=soul_text, restore=not self._args.fresh)
        if self.extractor.enabled:
            self.extractor.start()

    def _install_signals(self) -> None:
        """把 Ctrl-C 变成控制台该有的样子：一次作废手上这行，两次立刻走。

        默认的 SIGINT 会把 KeyboardInterrupt 掷进事件循环里——那正是
        「按了没退成、再按一次甩一段 threading traceback」的来源。
        """
        def handle(signum: int, frame: Any) -> None:   # noqa: ANN001 - 信号处理的原型
            now = time.monotonic()
            if now - self._last_sigint < 1.5:
                _bail(self.ui)
            self._last_sigint = now
            if self._busy and self._turn_task is not None and not self._turn_task.done():
                # 掷 KeyboardInterrupt 会落在事件循环当前那句 C 代码上——从那儿漏出去
                # 就是整个进程被打断。取消这一回合的任务才是这一回合自己的事
                self._turn_task.cancel()
                self.ui.line("  掐掉这一句了。")
                return
            self.ui.line("  已作废这一行。退出：/quit 或 Ctrl-D（连按两次 Ctrl-C 直接走）")
        with contextlib.suppress(ValueError):       # 不在主线程（测试里可能）就不装
            signal.signal(signal.SIGINT, handle)

    async def run(self) -> None:
        self._install_signals()
        meta = await self.storage.read_persona_meta(self.user_id)
        self.ui.banner(self.user_id, str(meta.get("name") or ""))
        if self._settings.group_mode:
            return  # 群聊里启动不 dump 任何东西，控制台整套锁着
        if self._args.show_prompt:
            await self._panel("prompt")
        elif self._args.open_panel:
            await self._panel("help")

        if self._args.once is not None:
            await self.turn(self._args.once)
            left = await self.extractor.wait_idle(self._args.flush or DEFAULT_FLUSH_SECONDS)
            return

        while True:
            line = await self._read_line()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if not await self.handle_command(line[1:]):
                    break
            else:
                # 配对挂起时，激活语与回填码从对话里截走——它们是说给控制台听的，
                # 不是说给她听的。没有挑战在跑就一句也截不动，闲聊照旧
                note = consume_pairing(self.pairing, line, source="cli")
                if note is not None:
                    self.ui.line(note)
                    if "配对完成" in note:
                        # 认出来之后她得主动打招呼——这是「我认出你了」的实测证据，
                        # 不是日志里一行 paired_at
                        self.ui.line("  " + await make_greeting(
                            short_ask(self.bot.ask_once, self._settings),
                            deadline=self._settings.pair_phrase_deadline_seconds,
                            settings=self._settings))
                    continue
                # 包成任务：Ctrl-C 才只掐这一回合，而不是把整个事件循环打断
                self._turn_task = asyncio.ensure_future(self.turn(line))
                try:
                    await self._turn_task
                except asyncio.CancelledError:
                    self.ui.line("  （这一句没说完，算了）")
                finally:
                    self._turn_task = None

    async def _read_line(self) -> str | None:
        """从键盘取一行。读的那根线程是常驻 daemon 线程（见 `_StdinPump`）：
        Ctrl-C 不该留下一根堵在 input() 上、退出时非 join 不可的线。"""
        if self._stdin is None:
            self._stdin = _StdinPump(self.ui)
            self._stdin.start()
        self.ui.end_stream()                  # 上一句哪怕没说完，也先把行收干净
        self._stdin.arm()                     # 到这一步才打 admin ▸，等的就是键盘
        try:
            return await self._stdin.get()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 终端故障不抛 traceback
            logger.debug("读取输入失败", exc_info=True)
            self.ui.error(f"读取输入失败：{exc}")
            return None

    async def shutdown(self) -> None:
        self.ui.end_stream()
        if self.extractor.backlog:
            await self.extractor.wait_idle(20.0)
        await self.extractor.aclose(timeout=10.0)
        await self.bot.aclose()

    # ------------------------------------------------------------ 一轮对话
    async def turn(self, text: str) -> None:
        self._busy = True
        try:
            return await self._turn_body(text)
        finally:
            self._busy = False

    async def _turn_body(self, text: str) -> None:
        speakers = _speakers_of(text) if self._settings.group_mode else None
        words, shots = find_sources(text)
        if shots:
            # 递图这件事要点一下，但只点一行灰字——角色的气泡里不夹附件回显
            self.ui.line(f"  递过去 {len(shots)} 样东西", style="dim")
        self.ui.begin_stream()
        stream: AsyncGenerator[str, None] = self.bot.stream_reply(
            self.user_id, words, today=dt.date.today(), speakers=speakers, images=shots
        )
        try:
            async for delta in stream:
                self.ui.push(delta)
        except BotError as exc:
            self.ui.end_stream()
            self.ui.error(exc.message, exc.hint)
            logger.debug("原始错误", exc_info=exc)
            return
        except KeyboardInterrupt:
            self.ui.end_stream()
            return
        except StorageError as exc:
            self.ui.end_stream()
            logger.warning("存储层故障: %s", exc)
            self.ui.error("存储层没写进去")
            return
        except Exception as exc:  # noqa: BLE001 - CLI 不做未处理 traceback
            self.ui.end_stream()
            logger.debug("未预期错误", exc_info=True)
            self.ui.error(f"{type(exc).__name__}")
            return
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()
        self.ui.end_stream()

    def _on_outcome(self, user_id: str, facts: Sequence[str], dynamics: Sequence[str], error: str | None) -> None:
        """后台抽取完成 → 只记账，不弹窗。想看在 /panel log。"""
        if user_id != self.user_id:
            return
        stamp = dt.datetime.now().strftime("%H:%M")
        if error:
            self._quiet_events.append(f"{stamp} 记忆未落盘：{error}")
            return
        if facts:
            self._quiet_events.append(f"{stamp} MEMORY +{len(facts)}：" + "；".join(facts))
        if dynamics:
            self._quiet_events.append(f"{stamp} RELATIONS +{len(dynamics)}：" + "；".join(dynamics))
        if len(self._quiet_events) > 50:
            del self._quiet_events[:-50]

    # ------------------------------------------------------------ 命令分发
    async def handle_command(self, raw: str) -> bool:
        name, _, rest = raw.partition(" ")
        name = name.strip("/").lower()
        rest = rest.strip()
        if name in {"quit", "exit", "q"}:
            return False
        if name == "help":
            await self._help()
            return True
        if self._settings.group_mode and name in GROUP_LOCKED:
            self.ui.line("（群里我不做这个动作。要调：/mode solo）", style="dim")
            return True
        if name == "panel":
            return await self._panel(rest)
        if name == "mode":
            return await self._mode(rest)
        if name == "sync":
            return await self._sync(rest)
        if name in {"pair", "unpair", "whoami-owner", "pairbox"}:
            return await self._pairing(name, rest)
        if name in MOVED:
            self.ui.line(f"  这个现在在 {MOVED[name]} 里", style="dim")
            return True
        self.ui.line(f"  没有 /{name} 这个命令，/help 看能做什么", style="dim")
        return True

    async def _help(self) -> None:
        rows = [
            ("直接打字", "就是说话。剩下的都归角色自己决定怎么说"),
            ("/panel", "控制台：人格、参数、Prompt、记忆、工具、状态（群聊里锁死）"),
            ("/mode solo|group [名字…]", "切 1V1 / 群聊"),
            ("/sync remote", "把灵魂与记忆推到 shijianus/MySoulBot"),
            ("/sync check", "同步前自查：体积闸门、忽略规则、凭据扫描"),
            ("/pair", "发起管理者配对（2 分钟有效）：一个机器人只能有一个管理者"),
            ("/unpair", "解绑管理者。改的是谁能管这台机器，想清楚再敲"),
            ("/pairbox", "看配对用的话术本（密文）里各有几句；/pairbox 换钥匙 重抄一本"),
            ("/quit", "退出"),
        ]
        if self._settings.group_mode:
            rows = [
                ("名字: 内容", "群聊发言，行首写谁说的"),
                ("/mode solo", "退出群聊，解锁控制台"),
                ("/quit", "退出"),
            ]
        self.ui.block("能做什么", "\n".join(f"- `{key}` — {desc}" for key, desc in rows))

    # ------------------------------------------------------------ /mode
    async def _mode(self, arg: str) -> bool:
        parts = _split(arg)
        target = (parts[0] if parts else "").lower()
        if not target:
            label = "群聊" if self._settings.group_mode else "1V1"
            names = "、".join(self._args.speakers or []) or "（还没点名）"
            self.ui.line(f"  当前场景：{label} · 群里的人：{names}")
            return True
        if target in {"solo", "1v1", "单"}:
            self._settings.chat_mode = "solo"
            self.ui.line("  已回到 1V1，控制台解锁：/panel")
            return True
        if target in {"group", "群"}:
            self._settings.chat_mode = "group"
            if len(parts) > 1:
                self._args.speakers = [name for name in parts[1:] if name]
            self.ui.line("  群聊模式：控制台与所有参数指令已锁死。发言写成「名字: 内容」。")
            return True
        self.ui.line("  只认 /mode solo 或 /mode group [名字…]", style="dim")
        return True

    # ------------------------------------------------------------ /sync
    async def _pairing(self, command: str, rest: str) -> bool:
        """管理者配对：发起、看进度、解绑。"""
        settings = self._settings
        if command == "pairbox":
            box = the_box(settings)
            counts = {slot: len(box.pool(slot)) for slot in
                      ("slot_a", "slot_b", "slot_c", "slot_d", "templates",
                       "code_line", "done_line", "greet_line", "nudge_line")}
            self.ui.line(f"话术本（密文）：{settings.pairing_box_path}")
            self.ui.line(f"  里面：{counts}；钥匙：{settings.pairing_key_path}"
                         f"（{settings.pairing_key_path.stat().st_size if settings.pairing_key_path.is_file() else 0} 字节）")
            self.ui.line("  这一本是配对用的口令拼法与应答句，模型读不到它——AI 全断了配对也走得完。")
            if rest.strip().lower() in {"rotate", "换钥匙", "重抄"}:
                box.rotate()
                self.ui.warn("已换一把主密钥重抄一遍；旧钥匙读不到这一本了。")
            return True
        if command == "unpair":
            record = read_owner(settings)
            if record is None:
                self.ui.warn("现在没有绑定的管理者，不用解。")
                return True
            self.ui.warn(f"要解绑的是：{record.binding_key()}（{record.paired_at}）")
            answer = await self._read_line()
            if (answer or "").strip().lower() not in {"y", "yes", "确认"}:
                self.ui.line("  没确认，解绑取消。", style="dim")
                return True
            unpair(settings)
            self.ui.warn("已解绑。在重新配对之前，账号级能力一律不可用。")
            return True
        current = self.pairing.active()
        if current is not None and current.stage == "unique":
            code = self.pairing.plaintext_code(current)
            self.ui.line(f"配对进行中：口令是 {current.source_key} 报的，他那一份码是 {format_code(code)}")
            self.ui.line(f"  下一步就一件事：把上面那串码贴回这里（贴错不发紧，贴错一次整场作废）")
            self.ui.line(f"  （码贴到手机那头去不算——那一步在控制台；口令也只在 {current.source_key} 那儿说过一次）")
            self.ui.line(f"  （这一场的口令是「{current.phrase}」，{current.seconds_left()} 秒后作废）")
            return True
        if current is not None:
            self.ui.line(f"已有一场配对在跑（{current.seconds_left()} 秒后作废）。"
                         f"把这句从你自己的 QQ 发给机器人：{current.phrase}")
            self.ui.line("  机器人会回一串码；把那串码**贴回这里**才算完成。")
            return True
        if not settings.owner_enabled:
            self.ui.warn("OWNER_ENABLED=false，分层没开。要配对先在 .env 里打开。")
            return True
        if read_owner(settings) is not None:
            self.ui.warn("已经绑过管理者了。一个机器人只有一个——要换先 /unpair")
            return True
        # 口令每场现生成，且**有硬预算**：人站在控制台前等，一条 40 秒不落正文的
        # 免费线路不该把配对卡在那儿——到点就本地现拼一句真随机的
        t0 = time.monotonic()
        phrase = await make_phrase(short_ask(self.bot.ask_once, settings),
                                   deadline=settings.pair_phrase_deadline_seconds,
                                   settings=settings)
        spent = time.monotonic() - t0
        challenge = self.pairing.start(channel="cli", phrase=phrase)
        self.ui.line(f"配对已开始，{challenge.seconds_left()} 秒内有效，到点自动作废。"
                     f"（口令现拼用了 {spent:.1f} 秒）")
        self.ui.line("  ① 把下面这句从**你自己的 QQ**发给机器人（在这儿敲不算数）：")
        self.ui.line(f"      【{challenge.phrase}】")
        self.ui.line("  ② 机器人会在 QQ 那头回你一串配对码（只回给报口令的那一个号）。")
        self.ui.line("  ③ 把那串码**贴回这里**再回车 —— 三个方向都对上，这一场才完成。")
        self.ui.line(f"  来源必须唯一：同一时间只有一个号在配（口令在谁那儿说过，就绑谁）")
        self.ui.line("  提示：码贴错一次整场作废，重来一遍就行；口令打错她只会当闲聊。")
        return True

        return True

    async def _sync(self, arg: str) -> bool:
        action = (arg or "").strip().lower()
        if action in {"", "flush", "抽取"}:
            if not self.extractor.enabled:
                self.ui.warn("反思抽取未启用")
                return True
            left = await self.extractor.wait_idle(60.0)
            self.ui.line(f"  抽取已排空{'（还有 %d 项）' % left if left else ''}")
            return True
        if action not in {"remote", "push", "check", "dry", "dry-run"}:
            self.ui.line("  用法：/sync remote | /sync check | /sync（等抽取落盘）", style="dim")
            return True
        dry = action in {"check", "dry", "dry-run"}
        push = action in {"remote", "push"}
        self.ui.line("  同步中…", style="dim")
        try:
            report = await run_sync(
                self._settings,
                push=push,
                dry_run=dry,
                storage=self.storage,
                user_id=self.user_id,
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败只给一句
            self.ui.error(f"同步失败：{type(exc).__name__}")
            logger.debug("同步失败", exc_info=True)
            return True
        if not report.ok:
            self.ui.warn(report.aborted)
            return True
        self.ui.line(f"  {report.human()}")
        if self._settings.diagnostics:
            for step in report.steps:
                self.ui.line(f"    · {step}", style="dim")
        return True

    # ------------------------------------------------------------ /panel 控制台
    async def _panel(self, arg: str) -> bool:
        parts = _split(arg)
        sub = (parts[0] if parts else "").lower()
        rest = " ".join(parts[1:]) if len(parts) > 1 else ""
        handlers = {
            "help": self._panel_help,
            "ls": self._panel_help,
            "status": self._panel_status,
            "prompt": self._panel_prompt,
            "memory": self._panel_memory,
            "facts": self._panel_memory,
            "relations": self._panel_relations,
            "dynamics": self._panel_relations,
            "soul": self._panel_soul,
            "clawd": self._panel_clawd,
            "profile": self._panel_profile,
            "whoami": self._panel_whoami,
            "log": self._panel_log,
            "tools": self._panel_tools,
            "tool": self._panel_tool,
            "debug": self._panel_debug,
            "sync": self._panel_sync,
            "clear": self._panel_clear,
            "model": self._panel_model,
            "note": self._panel_note,
            "append": self._panel_append,
            "edit": self._panel_edit,
            "archive": self._panel_archive,
            "rapport": self._panel_rapport,
            "温度": self._panel_rapport,
            "web": self._panel_web,
            "面板": self._panel_web,
            "rhythm": self._panel_rhythm,
            "体温": self._panel_rhythm,
            "audit": self._panel_audit,
            "persona": self._panel_persona,
            "char": self._panel_persona,
            "user": self._panel_user,
        }
        if not sub:
            return await self._panel_help()
        handler = handlers.get(sub)
        if handler is None:
            self.ui.line(f"  控制台里没有 {sub}，/panel help 看清单", style="dim")
            return True
        if sub in PANEL_ARG_COMMANDS:
            return await handler(rest)  # type: ignore[call-arg]
        return await handler()  # type: ignore[call-arg]

    async def _panel_help(self) -> bool:
        rows = [
            ("/panel status", "用户、四份文档体积、记忆条数、日志与归档、工具清单"),
            ("/panel prompt", "本轮真正送进模型的 system prompt 与分层字数"),
            ("/panel memory", "MEMORY.md（事实）"),
            ("/panel relations", "RELATIONS.md（关系动态与态度演变）"),
            ("/panel soul", "SOUL.md（外在人格）"),
            ("/panel clawd", "CLAWD.md（深层灵魂）"),
            ("/panel profile", "USER.md（用户画像）"),
            ("/panel edit <soul|user|memory>", "用 $EDITOR 改，保存即生效"),
            ("/panel append <一句话>", "手工写一条事实"),
            ("/panel note <一句话>", "手工写一条关系动态（怎么跟他相处）"),
            ("/panel persona [list|switch|import|show|delete]", "人格库与酒馆卡"),
            ("/panel user <id>", "切用户（四份文件与日志完全隔离）"),
            ("/panel model [名字]", "查看或临时切换对话模型"),
            ("/panel tools", "当前挂载了哪些工具"),
            ("/panel tool <name> k=v", "手动下一个单（结果只回给模型）"),
            ("/panel log", "这段时间后台默默记下了什么"),
            ("/panel archive", "立刻滚动归档日志、下沉超限记忆"),
            ("/panel audit", "工具调用记录"),
            ("/panel web", "沉浸面板的地址（手机或浏览器里看，只读）"),
            ("/panel sync", "等待后台抽取排空"),
            ("/panel clear", "清空近期上下文（人格与记忆不动）"),
            ("/panel debug on|off", "显示错误细节与同步步骤"),
            ("/panel whoami", "当前用户与场景"),
        ]
        self.ui.block("控制台", "\n".join(f"- `{key}` — {desc}" for key, desc in rows))
        return True

    async def _panel_status(self) -> bool:
        info = await self.storage.describe(self.user_id)
        session = self.bot.session(self.user_id)
        clawd = await self.clawd.describe()
        registry = self.bot.registry(self.user_id)
        rows: list[tuple[str, str]] = [
            ("当前用户", str(info["user_id"])),
            ("场景", "群聊" if self._settings.group_mode else "1V1"),
            ("目录", self.ui.short_path(info["dir"])),
            ("外在人格", (info["persona_slug"] or "（未应用预设）") + (f" · {info['persona_name']}" if info["persona_name"] else "")),
            ("深层灵魂", f"{'存在' if clawd['exists'] else '缺失'} · {clawd['chars']} 字节 · 演进备注 {clawd['notes']} 条"),
            ("对话轮数", f"{session.turns} · 上下文 {len(session.history)} 条 · 工具调用 {session.tool_calls} 次"),
        ]
        for doc, meta in info["docs"].items():
            rows.append((f"{doc}.md", f"{self.ui.short_path(meta['path'])} · {'存在' if meta['exists'] else '缺失'} · {meta['chars']} 字节"))
        rows += [
            ("长期记忆", f"{info['facts']} 条事实 · {info['relations']} 条关系动态"),
            ("人格参数", (str(info["persona_config"]) if info["persona_config"] else "（无覆盖）") + f" · SOUL 备份 {info['backups']} 份"),
            ("明文日志", f"{info['bytes'] // 1024} KB · 文件 {'、'.join(info['log_files']) or '（尚无）'}"),
            ("已归档", str(len(info["archived"])) + (" 份：" + "、".join(info["archived"]) if info["archived"] else " 份")),
            ("工具", registry.summary() + f" · 形态 {self.bot.tool_mode(self.user_id)}"),
            ("此刻", self._rhythm_brief()),
            ("温度", self._rapport_brief()),
            ("对话模型", f"{self._settings.model} @ {self._settings.base_url}"),
            ("抽取模型", self._settings.effective_extractor_model),
        ]
        if info["over_budget"]:
            rows.append(("超体积预算", "、".join(info["over_budget"])))
        self.ui.table("status", rows)
        return True

    async def _panel_prompt(self) -> bool:
        prompt, layers = await self.bot.preview_prompt(self.user_id)
        self.ui.raw("分层概览", layers.render_report(), style="cyan")
        self.ui.raw("system prompt · 本轮实际发送", prompt)
        return True

    async def _panel_memory(self) -> bool:
        content = await self.storage.read_doc(self.user_id, "MEMORY")
        facts = parse_facts(content)
        self.ui.block("MEMORY.md", content)
        self.ui.line(f"  {len(facts)} 条事实 · 队列积压 {self.extractor.backlog}")
        return True

    async def _panel_relations(self) -> bool:
        content = await self.storage.read_doc(self.user_id, "RELATIONS")
        self.ui.block("RELATIONS.md", content)
        return True

    async def _panel_soul(self) -> bool:
        self.ui.block("SOUL.md（外在人格）", await self.storage.read_doc(self.user_id, "SOUL"))
        return True

    async def _panel_clawd(self) -> bool:
        self.ui.block("CLAWD.md（深层灵魂）", await self.clawd.read_text())
        return True

    async def _panel_profile(self) -> bool:
        self.ui.block("USER.md", await self.storage.read_doc(self.user_id, "USER"))
        return True

    async def _panel_whoami(self) -> bool:
        session = self.bot.session(self.user_id)
        self.ui.line(
            f"  用户 {self.user_id} · 人格 {session.persona_slug or '（未命名）'} · "
            f"{'群聊' if self._settings.group_mode else '1V1'} · 工具 {'on' if self._settings.tools_enabled else 'off'}"
        )
        return True

    def _rhythm_brief(self) -> str:
        presence = self.bot.presence_of(self.user_id)
        if presence is None:
            return "（本轮尚未开始）"
        return (
            f"{presence.slot.label} · 余温 {int(presence.mood_residual * 100)}%"
            f" · 耐心 {int(presence.patience.left * 100)}%"
            + (f" · 隔了 {presence._gap_text()}" if presence.gap_days else "")
        )

    def _rapport_brief(self) -> str:
        rapport = self.bot.rapport_of(self.user_id)
        return f"{rapport.value}/100 · {rapport.label}" if rapport else "（未结算）"

    async def _panel_log(self) -> bool:
        if not self._quiet_events:
            self.ui.line("  这段时间后台没有记新东西")
            return True
        self.ui.raw("后台默默记下的", "\n".join(self._quiet_events))
        self._quiet_events.clear()
        return True

    async def _panel_tools(self) -> bool:
        registry = self.bot.registry(self.user_id)
        if not len(registry):
            self.ui.line("  没有挂载任何工具（TOOLS_ENABLED 或能力开关关着）")
            return True
        self.ui.table("当前工具", [(tool.name, tool.hint) for tool in registry.tools])
        self.ui.line(f"  形态：{self.bot.tool_mode(self.user_id)}")
        return True

    async def _panel_tool(self, arg: str) -> bool:
        parts = _split(arg)
        if not parts:
            self.ui.line("  用法：/panel tool <name> k=v [k2=v2]", style="dim")
            return True
        registry = self.bot.registry(self.user_id)
        result = await registry.call(parts[0], _pairs(parts[1:]))
        mark = "✓" if result.ok else "✗"
        self.ui.line(f"  {mark} {parts[0]} → {result.digest(300)}")
        for artifact in result.artifacts:
            self.ui.line(f"    产物：{self.ui.short_path(artifact)}", style="dim")
        return True

    async def _panel_debug(self, arg: str) -> bool:
        flag = arg.strip().lower()
        if flag in {"on", "开", "true", "1"}:
            self._settings.diagnostics = True
            self._settings.apply_logging(terminal_info=True)
            self.ui.line("  已显示错误细节、同步步骤与引擎日志")
        elif flag in {"off", "关", "false", "0"}:
            self._settings.diagnostics = False
            self._settings.apply_logging(terminal_info=False)
            self.ui.line("  已收回到只留角色表达（引擎 chatter 仍在 storage/logs/runtime.log）")
        else:
            self.ui.line(f"  diagnostics = {self._settings.diagnostics}（/panel debug on|off）")
        return True

    async def _panel_sync(self) -> bool:
        return await self._sync("flush")

    async def _panel_clear(self) -> bool:
        self.bot.reset_history(self.user_id)
        self.ui.line("  近期上下文已清空")
        return True

    async def _panel_model(self, arg: str) -> bool:
        if not arg:
            self.ui.line(
                f"  对话模型 {self._settings.model} · 抽取模型 {self._settings.effective_extractor_model}"
            )
            return True
        self._settings.model = arg
        self.ui.line(f"  对话模型已切换为 {arg}（BASE_URL / API_KEY 变更需重启）")
        return True

    async def _panel_note(self, arg: str) -> bool:
        if not arg:
            self.ui.line("  用法：/panel note 他累了就嫌话多，宜短", style="dim")
            return True
        written = await self.storage.append_dynamics(self.user_id, [arg])
        self.ui.line(f"  {'记下了' if written else '这条已经在关系动态里了'}")
        return True

    async def _panel_append(self, arg: str) -> bool:
        if not arg:
            self.ui.line("  用法：/panel append 用户不喜欢被追问细节", style="dim")
            return True
        written = await self.storage.append_facts(self.user_id, [arg])
        self.ui.line(f"  {'写进 MEMORY 了' if written else '这条已经在记忆里了'}")
        return True

    async def _panel_edit(self, arg: str) -> bool:
        key = (arg or "").strip().lower()
        if key not in EDIT_TARGETS:
            self.ui.line("  用法：/panel edit <soul|user|memory>", style="dim")
            return True
        doc = EDIT_TARGETS[key]
        await self.storage.read_doc(self.user_id, doc)
        path = self.storage.doc_path(self.user_id, doc)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        try:
            code = await asyncio.to_thread(_run_editor, editor, path)
        except OSError as exc:
            self.ui.error(f"无法启动编辑器 {editor}", str(exc))
            return True
        if code != 0:
            self.ui.warn(f"{editor} 退出码 {code}，文件可能未保存")
        content = await self.storage.read_doc(self.user_id, doc)
        self.ui.line(f"  {doc}.md {'已更新（%d 字符）' % len(content) if content.strip() else '现在是空的，下一轮会退回模板'}")
        return True

    async def _panel_web(self) -> bool:
        """沉浸面板的入口：只报地址，不碰状态。"""
        host = self._settings.server_host if self._settings.server_host not in {"0.0.0.0", ""} else "127.0.0.1"
        link = f"http://{host}:{self._settings.server_port}/panel?user={self.user_id}"
        if not self._settings.panel_enabled:
            self.ui.line("  面板被 PANEL_ENABLED=false 关着，起了服务也打不开")
            return True
        self.ui.line(f"  沉浸面板： {link}")
        self.ui.line("  要它常驻：bash scripts/daemon.sh start（酒馆端点与面板同一个端口）", style="dim")
        self.ui.line("  面板只许看：熟络度在上面是一根不能拖的条，温度只能由相处攒出来。", style="dim")
        return True

    async def _panel_rapport(self, arg: str) -> bool:
        """只读：熟络度、阶段分寸、依据。没有 set——温度只能由相处攒出来。"""
        rapport = await self.bot.rapport.read(self.user_id)
        rows = [
            ("熟络度", f"{rapport.value}/100"),
            ("阶段", f"{rapport.label} · {rapport.stage}"),
            ("峰值", str(int(round(rapport.peak)))),
            ("依据", " · ".join(rapport.evidence) or "刚开始"),
            ("这一轮涨了多少", " · ".join(f"{key} {value:+g}" for key, value in rapport.deltas.items()) or "—"),
        ]
        self.ui.table("熟络度", rows)
        self.ui.line(f"  分寸：{rapport.conduct}")
        if (arg or "").strip().lower() in {"why", "依据", "为什么"}:
            counters = dict((self.bot.session(self.user_id).state or {}).get("rapport") or {})
            self.ui.raw("计数字段", "\n".join(f"{key} = {value}" for key, value in sorted(counters.items())))
            self.ui.line("  温度由引擎按「轮次 / 深夜 / 他新交代的事 / 攒下的分寸 / 吵过又好和」累积，"
                         "隔久了会凉但不会掉破峰值的一半。命令与酒馆都改不了它。")
        return True

    async def _panel_rhythm(self) -> bool:
        presence = self.bot.presence_of(self.user_id)
        if presence is None:
            self.ui.line("  这一轮还没开始，体温暖不出来（先说一句 /panel rhythm 之前随便聊一句）")
            return True
        rows = [
            ("此刻", presence.stamp.strftime("%Y-%m-%d %H:%M") + f"（{self._settings.user_timezone or '系统时区'}）"),
            ("时段", f"{presence.slot.label}" + (" · 深夜" if presence.deep_night else "")),
            ("身体", presence.slot.body),
            ("分寸", presence.slot.conduct),
            ("情绪余温", f"{int(presence.mood_residual * 100)}%"
             + (f" · 效价 {presence.mood.valence:+g}" if presence.mood.valence else "")
             + (f" · 起因：{presence.mood.cause}" if presence.mood.cause else "")),
            ("耐心余额", f"{int(presence.patience.left * 100)}% · 今天第 {presence.patience.turns_today} 轮"),
            ("隔了多久", presence._gap_text() if presence.gap_days else "（没有上一见的记录）"),
        ]
        self.ui.table("此刻的我", rows)
        return True

    async def _panel_archive(self) -> bool:
        rotation = await self.storage.rotate_transcripts(self.user_id)
        moved = 0
        for doc in ("MEMORY", "RELATIONS"):
            moved += await self.storage.compact_memory(self.user_id, doc=doc)
        self.ui.line(
            f"  归档 {len(rotation['archived'])} 份日志 · 下沉 {moved} 条记忆 · "
            f"明文残留 {rotation['plain_bytes'] // 1024} KB"
        )
        if rotation["over_budget"]:
            self.ui.warn("超出单文档预算：" + "、".join(rotation["over_budget"]))
        return True

    async def _panel_audit(self) -> bool:
        path = self._settings.audit_dir / "tools.jsonl"
        if not path.is_file():
            self.ui.line("  还没有工具调用记录")
            return True
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.ui.raw("工具调用（最近 20 条）", "\n".join(lines[-20:]))
        return True

    # ------------------------------------------------------------ /panel persona
    async def _panel_persona(self, arg: str) -> bool:
        parts = _split(arg)
        sub = (parts[0] if parts else "").lower()
        rest = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not sub or sub in {"list", "ls", "all"}:
            self._persona_list()
        elif sub in {"switch", "use", "apply"}:
            await self._persona_switch(rest)
        elif sub == "import":
            await self._persona_import(rest)
        elif sub in {"delete", "rm", "remove"}:
            if not rest:
                self.ui.line("  用法：/panel persona delete <标识>", style="dim")
                return True
            self.library.delete(rest)
            self.ui.line(f"  已删除 {rest}")
        elif sub in {"show", "info"}:
            preset = self.library.get(rest)
            self.ui.block(f"{preset.slug} · {preset.name}", preset.soul_text())
        else:
            self.ui.line("  可用：/panel persona | switch <标识> [keep] | import <卡.json> [标识] | show <标识> | delete <标识>", style="dim")
        return True

    def _persona_list(self) -> None:
        current = self.bot.session(self.user_id).persona_slug or "（未应用，走 SOUL.md 现内容）"
        table = Table(title=f"可用人格 · 当前：{current}", title_style="cyan", header_style="dim")
        table.add_column("标识", style="bold")
        table.add_column("名称")
        table.add_column("类型")
        table.add_column("开场", justify="right")
        table.add_column("人格级参数", style="dim")
        for preset in self.library.list():
            params = " ".join(f"{k}={v:g}" for k, v in sorted(preset.config.items())) or "—"
            table.add_row(
                preset.slug,
                f"{preset.name}（{preset.title}）" if preset.title else preset.name,
                SOURCE_LABEL.get(preset.source, preset.source),
                "有" if preset.first_mes else "—",
                params,
            )
        self.ui.console.print(table)
        self.ui.line("  切换：/panel persona switch <标识>   导入：/panel persona import ./卡.json [标识]")

    async def _persona_switch(self, arg: str) -> None:
        slug, _, tail = arg.partition(" ")
        keep = tail.strip().lower() in {"keep", "继续", "n"}
        if not slug:
            self.ui.line("  用法：/panel persona switch <标识> [keep]", style="dim")
            return
        preset = self.library.get(slug)
        applied = await self.bot.apply_persona(self.user_id, preset, keep_history=keep)
        lines = [
            f"已应用人格 [bold]{applied.slug}[/bold] · {applied.name}"
            + (f"（{applied.title}）" if applied.title else ""),
            f"SOUL.md 已替换；原内容备份：{self.ui.short_path(applied.backup_path) if applied.backup_path else '无（此前没有文件）'}",
            "深层灵魂 CLAWD.md 不动——换的是人格，不是我的立场。",
            "近期上下文：[yellow]已重置[/yellow]" if applied.history_reset else "近期上下文：[green]保留[/green]",
        ]
        if applied.config:
            lines.append("生成参数已按人格覆盖：" + " ".join(f"{k}={v:g}" for k, v in sorted(applied.config.items())))
        self.ui.console.print(Panel(Text.from_markup("\n".join(lines)), border_style="dim", title="控制台"))
        if applied.greeting:
            self.ui.role_block(applied.greeting, "开场")

    async def _persona_import(self, arg: str) -> None:
        path_text, _, slug = arg.partition(" ")
        if not path_text:
            self.ui.line("  用法：/panel persona import ./角色卡.json [标识]", style="dim")
            return
        path = Path(path_text).expanduser()
        if not path.is_file():
            self.ui.line(f"  找不到文件：{path}", style="dim")
            return
        preset = await asyncio.to_thread(self.library.import_card, path, slug=slug.strip())
        lines = [
            f"已导入并生成人格 [bold]{preset.slug}[/bold] · {preset.name}",
            f"位置：{self.ui.short_path(preset.soul_path)}",
            f"开场白：{'有' if preset.first_mes else '无'} · 备选开场 {len(preset.greetings)} 条 · 标签 {'、'.join(preset.tags) or '无'}",
        ]
        for warning in preset.warnings:
            lines.append(f"[yellow]注意：{warning}[/yellow]")
        self.ui.console.print(Panel(Text.from_markup("\n".join(lines)), border_style="dim", title="控制台"))
        self.ui.line(f"  应用它：/panel persona switch {preset.slug}")

    async def _panel_user(self, arg: str) -> bool:
        if not arg:
            self.ui.line(f"  当前用户：{self.user_id}（/panel user <新id> 切换）")
            return True
        try:
            await self.bot.open_session(arg, restore=True)
        except PathSafetyError as exc:
            self.ui.error(str(exc))
            return True
        self.user_id = arg
        self.ui.line(f"  已切换到用户 {arg}，其 SOUL / USER / MEMORY / RELATIONS / logs 完全独立。")
        return True


# ---------------------------------------------------------------- 辅助函数
PANEL_ARG_COMMANDS: Final[frozenset[str]] = frozenset(
    {"tool", "debug", "model", "note", "append", "edit", "persona", "char", "user", "rapport", "温度"}
)


def _plain(text: str) -> str:
    """把「HTTP 410」这类机器字样从日常可见的输出里抹掉。"""
    cleaned = re.sub(r"\s*HTTP\s*\d{3}\b", "", str(text))
    cleaned = re.sub(r"[（(]\s*[)）]", "", cleaned)
    return cleaned.strip(" ：;。") or "接口那边没接上"


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _run_editor(editor: str, path: Path) -> int:
    return subprocess.run([*editor.split(), str(path)], check=False).returncode


def _split(text: str) -> list[str]:
    """按 shell 规则切参数，但允许中文与 URL 直接写。"""
    try:
        return [piece for piece in shlex.split(text) if piece]
    except ValueError:
        return [piece for piece in text.split() if piece]


def _pairs(pieces: Sequence[str]) -> dict[str, str]:
    args: dict[str, str] = {}
    for piece in pieces:
        key, sep, value = piece.partition("=")
        if sep:
            args[key] = value
    return args


def _speakers_of(text: str) -> list[str]:
    """从「阿哲: …… ／ 老周：……」里点出在场的人，供语境层标注谁在说话。"""
    names: list[str] = []
    for line in text.splitlines():
        head, found, _ = line.partition(":")
        if not found:
            head, found, _ = line.partition("：")
        name = head.strip()
        if found and name and len(name) <= 24 and name not in names:
            names.append(name)
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mysoulbot",
        description="MySoulBot —— 深层灵魂 + 外在人格的双层角色扮演引擎（终端入口）",
    )
    parser.add_argument("--user", help="用户 ID，决定 storage/data/users/<id>/ 的隔离目录")
    parser.add_argument("--once", help="只说一句话然后退出，便于脚本调用")
    parser.add_argument("--flush", type=float, help="配合 --once：等待后台抽取的秒数")
    parser.add_argument("--soul-file", dest="soul_file", help="用指定 Markdown 文件作为该用户的 SOUL.md")
    parser.add_argument("--fresh", action="store_true", help="不从日志恢复近期上下文")
    parser.add_argument("--no-extract", dest="no_extract", action="store_true", help="关闭后台反思抽取")
    parser.add_argument("--no-tools", dest="no_tools", action="store_true", help="不挂载工具")
    parser.add_argument("--no-immersive", dest="no_immersive", action="store_true", help="显示角色标签与系统提示")
    parser.add_argument("--panel", dest="open_panel", action="store_true", help="启动即打开控制台")
    parser.add_argument("--group", action="store_true", help="群聊模式：控制台与参数指令锁死")
    parser.add_argument("--speakers", nargs="*", default=[], help="群聊里在场的人名")
    parser.add_argument("--show-prompt", dest="show_prompt", action="store_true", help="启动即打印 system prompt")
    parser.add_argument("--verbose", action="store_true", help="显示错误细节（等价 /panel debug on）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.no_extract:
        settings.extractor_enabled = False
    if args.no_tools:
        settings.tools_enabled = False
    if args.no_immersive:
        settings.immersive = False
    if args.group:
        settings.chat_mode = "group"
    if args.verbose:
        settings.log_level = "DEBUG"
        settings.diagnostics = True
    settings.apply_logging(terminal_info=settings.diagnostics)
    ui = TerminalUI(settings)
    try:
        code = asyncio.run(_amain(settings, args, ui))
    except KeyboardInterrupt:
        ui.line("已中断")
        _bail(ui)                                  # 不等任何线程：直接走
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 不做未处理 traceback
        logger.debug("CLI 崩了", exc_info=True)
        ui.error(f"出了点意外：{type(exc).__name__}: {exc}")
        code = 1
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    # 读键盘那根 daemon 线程没法被 join，也不该被 join：
    # 用 os._exit 收尾，解释器退出时就不会再走到 threading 的那一串 join
    os._exit(code)


# 收尾最多等多久。记忆都是原子写落盘的，超时的代价只是「这一段余温没攒上」，
# 而不是「按了 Ctrl-C 退不掉」——后者才是那截 traceback 的来处
EXIT_GRACE_SECONDS: Final[float] = 8.0


async def _amain(settings: Settings, args: argparse.Namespace, ui: TerminalUI) -> int:
    app = App(settings, args)
    try:
        await app.setup()
    except BotError as exc:
        ui.error(exc.message, exc.hint)
        return 2
    except (PathSafetyError, StorageError) as exc:
        ui.error(str(exc))
        return 2
    interrupted = False
    try:
        await app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # 掷进事件循环的那一下：别当场散架，先把记忆落盘再走
        interrupted = True
        ui.line("已中断，正在收尾…")
    with contextlib.suppress(ValueError):        # 不在主线程（测试里可能）就不装
        signal.signal(signal.SIGINT, lambda *_: _bail(ui))
    try:
        await asyncio.wait_for(app.shutdown(), timeout=EXIT_GRACE_SECONDS)
    except asyncio.TimeoutError:
        ui.warn(f"收尾超过 {EXIT_GRACE_SECONDS:.0f} 秒，不接着等了（该落盘的都已原子落盘）")
    except (KeyboardInterrupt, asyncio.CancelledError):
        ui.warn("收尾被打断，不接着等了")
    except Exception as exc:  # noqa: BLE001 - 收尾失败只少点余温，别甩 traceback
        logger.debug("收尾没走完", exc_info=True)
        ui.warn(f"收尾没走完：{type(exc).__name__}")
    return 130 if interrupted else 0


def _bail(ui: TerminalUI) -> None:
    """硬退出：flush 之后直接走，绕开解释器退出时那一串线程 join。"""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(130)


if __name__ == "__main__":
    sys.exit(main())
