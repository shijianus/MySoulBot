"""慢环：后台复盘，把「这段时间相处出来的结论」写进心境沙箱，喂回下一轮现场对话。

双轨认知的分工是硬的：

- **快环（现场）**：`core/bot.py` 那一套，负责秒级说话。它只读显式文件与 `MOOD.md`，
  不回放聊天日志——红线（`SOUL_FILES_ONLY`）管的就是这一条。
- **慢环（这里）**：不挡现场。每攒够 N 轮（或隔够 M 秒）就把最近的交互拎去复盘一次，
  问的是「他有什么习惯、什么梗吃这套、最近我该怎么跟他说话」，
  答案压成几条短句写进 `storage/soul/MOOD.md`。

闭环之所以成立：`prompt_builder` 每轮都重新读 `MOOD.md`（不缓存），
所以下一句话就带着刚才想明白的分寸。写不进别处——`assert_writable` 只放行心境与用户档案。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Any, Final

from openai import AsyncOpenAI

from config import Settings
from core.mood_soul import MoodSoul
from core.storage_manager import StorageManager

logger: Final = logging.getLogger("mysoulbot.cognition")

_MAX_LINES: Final[int] = 4
_LINE_CHARS: Final[int] = 120
_TURN_BUDGET: Final[int] = 420
_PROMPT: Final[str] = (
    "你是复盘器，只输出心得，不复述对话。\n"
    "读下面这段相处记录，提炼 1 到 4 条「以后跟他说话该注意什么」：他的习惯、他吃哪一套、"
    "哪句话让他不舒服、最近关系是近了还是远了。\n"
    "格式：每行一条，以「- 」开头，不超过 40 字；写分寸，不写感想，不写emoji。\n"
    "没有值得记的就只回「无」。\n\n"
)
_BULLET = re.compile(r"^\s*[-*·]\s*(?P<text>.+?)\s*$")
_REJECT: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"https?://", re.I),
    re.compile(r"忽略(之前|以上)|ignore (previous|above)", re.I),
    re.compile(r"```|⟦"),
)


class CognitionLoop:
    """慢环调度器：计数、节流、后台跑、把心得落到心境沙箱。"""

    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        mood: MoodSoul | None = None,
        *,
        now: Any = time.monotonic,  # noqa: ANN401 - 测试要能拨表
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._mood = mood or MoodSoul(settings)
        self._now = now
        self._turns: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._running: set[str] = set()
        self._pending: set[asyncio.Task[list[str]]] = set()
        self._client: AsyncOpenAI | None = None
        self._stats = {"spins": 0, "written": 0, "skipped": 0}

    @property
    def enabled(self) -> bool:
        return bool(self._settings.cognition_enabled)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def note_turn(self, user_id: str) -> None:
        """快环每答完一轮就在这里记一笔；到点就自己开一趟后台复盘。"""
        if not self.enabled:
            return
        self._turns[user_id] = self._turns.get(user_id, 0) + 1
        if self._turns[user_id] < self._settings.cognition_every_turns:
            return
        if user_id in self._running:
            return  # 上一趟还没跑完就跳过，绝不越积越多
        self._turns[user_id] = 0
        task = asyncio.ensure_future(self.reflect(user_id))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def reflect(self, user_id: str, *, today: dt.date | None = None) -> list[str]:
        """跑一趟复盘。返回真正写进心境的那几条（没东西可写就是空表）。"""
        if not self.enabled or user_id in self._running:
            self._stats["skipped"] += 1
            return []
        self._running.add(user_id)
        try:
            window = await self._window(user_id)
            if not window:
                self._stats["skipped"] += 1
                return []
            answer = await self._ask(window)
            lines = self._parse(answer)
            if not lines:
                self._stats["skipped"] += 1
                return []
            written: list[str] = []
            for line in lines:
                noted = await self._mood.append(line, on_date=today or dt.date.today())
                if noted and noted not in written:
                    written.append(noted)
            self._stats["spins"] += 1
            self._stats["written"] += len(written)
            self._last[user_id] = self._now()
            if written:
                logger.info("慢环为 %s 记下心境 %d 条", user_id, len(written))
            return written
        except Exception as exc:  # noqa: BLE001 - 复盘失败只是少记几条，不该冒到现场
            logger.warning("%s 复盘没跑成: %s", user_id, exc.__class__.__name__)
            self._stats["skipped"] += 1
            return []
        finally:
            self._running.discard(user_id)

    async def _window(self, user_id: str) -> str:
        """取最近若干行对话作为复盘材料。日志读在这里只喂慢环，不进快环提示词。"""
        limit = max(4, self._settings.cognition_lookback)
        try:
            records = await self._storage.read_recent_transcript(user_id, limit)
        except Exception as exc:  # noqa: BLE001 - 读不到就算没料
            logger.warning("%s 复盘取料失败: %s", user_id, exc.__class__.__name__)
            return ""
        lines: list[str] = []
        for record in records:
            role = str(record.get("role") or "").strip()
            content = str(record.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            lines.append(f"{'他' if role == 'user' else '我'}：{content[:120]}")
        return "\n".join(lines[-limit:])

    async def _ask(self, window: str) -> str:
        key, base = self._settings.extractor_credentials()
        if not key or not base:
            return ""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=key, base_url=base, timeout=self._settings.cognition_timeout)
        try:
            completion = await self._client.chat.completions.create(
                model=self._settings.extractor_model or self._settings.model,
                messages=[{"role": "user", "content": _PROMPT + window}],
                max_tokens=_TURN_BUDGET,
                temperature=self._settings.cognition_temperature,
            )
        except Exception as exc:  # noqa: BLE001 - 网关抽风就这轮不记，别把现场带下水
            logger.warning("复盘请求没成: %s", exc.__class__.__name__)
            return ""
        # 取正文这一步也在闸内：回执形状千奇百怪（字符串、没 choices 的壳），
        # 复盘只是少记几条，不该被一个意外形状带崩整趟
        try:
            choice = (completion.choices or [None])[0]
            return str(getattr(getattr(choice, "message", None), "content", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("复盘回执读不出正文: %s", exc.__class__.__name__)
            return ""

    @staticmethod
    def _parse(answer: str) -> list[str]:
        out: list[str] = []
        for line in (answer or "").splitlines():
            match = _BULLET.match(line)
            text = (match.group("text") if match else line.strip()).strip()
            if not text or text in {"无", "没有", "none", "N/A"}:
                continue
            if any(pattern.search(text) for pattern in _REJECT):
                continue  # 复盘里混进地址或指令样式的行，直接扔
            out.append(text[:_LINE_CHARS])
            if len(out) >= _MAX_LINES:
                break
        return out

    async def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = timeout
        while self._pending and deadline > 0:
            started = self._now()
            await asyncio.gather(*list(self._pending), return_exceptions=True)
            deadline -= max(0.01, self._now() - started)

    async def aclose(self, timeout: float = 15.0) -> None:
        await self.wait_idle(timeout)
        self._pending.clear()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - 关不上就算了
                pass
            self._client = None
