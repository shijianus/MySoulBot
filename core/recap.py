"""会话回看（recap）——把超出逐字窗口的那几轮压成几条「刚才说到哪儿」。

为什么要有这一层：对方连着说十句、隔一小时再回一句时，逐字回放要么把提示词撑爆、
要么把前头几句挤掉，两种都是「她忘了自己刚说过什么」。这里改成
`前几句的要点 + 最近几句原文 + 当前这句`，长对话才有连续性。

**红线要说清楚**：这一层只作**本轮对话的上下文**，不是人格依据。
人格仍只由 SOUL / CLAWD / USER 这些显式文件决定（见 prompt_builder 的 soul_files_only），
回看里的内容不会渗进「我是谁」，只会渗进「我们刚才聊到哪儿」。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Final

from config import Settings
from core.storage_manager import StorageManager

logger: Final = logging.getLogger("mysoulbot.recap")

_MAX_BULLETS: Final[int] = 6
_BULLET_CHARS: Final[int] = 46
_HEAD: Final[str] = "# 会话回看（引擎生成的上下文，不是人格依据）"

_BULLET = re.compile(r"^\s*(?:[-*·]|\d+[.)])\s*(?P<text>.+?)\s*$")
_REJECT = re.compile(r"https?://|```|忽略(之前|以上)|ignore (previous|above)", re.I)

_PROMPT: Final[str] = (
    "你在替一段聊天记录维护「刚才说到哪儿」的要点，只写事实进度，不写感想、不写评价。\n"
    "把已有要点和新添的对话合并成最多 6 条，每条一行、以「- 」开头、不超过 30 字：\n"
    "谁做了什么、说到哪儿、有没有还没接的事。旧要点里还成立的要留着，被新话推翻的就删掉。\n"
    "没有内容可记时只回「无」。\n\n"
)


class SessionRecap:
    """每个用户一份 RECAP.md。压缩在后台跑，绝不挡在回话的那条路上。"""

    def __init__(self, settings: Settings, storage: StorageManager) -> None:
        self._settings = settings
        self._storage = storage
        self._pending: set[asyncio.Task[None]] = set()
        self._running: set[str] = set()
        self._client: Any = None
        self.stats = {"folded": 0, "skipped": 0, "failed": 0}

    @property
    def enabled(self) -> bool:
        return bool(self._settings.recap_enabled)

    def path(self, user_id: str) -> Any:  # noqa: ANN401 - Path
        return self._storage.user_dir(user_id) / "RECAP.md"

    # ------------------------------------------------------------ 读
    async def read(self, user_id: str) -> list[str]:
        """读出要点。文件不在或读失败就是没内容，不报错——回看缺了不影响说话。"""
        if not self.enabled:
            return []
        path = self.path(user_id)
        try:
            text = await asyncio.to_thread(path.read_text, "utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return self._bullets(text)

    @staticmethod
    def _bullets(text: str) -> list[str]:
        out: list[str] = []
        for line in (text or "").splitlines():
            match = _BULLET.match(line.strip())
            body = (match.group("text") if match else "").strip()
            if not body or body in {"无", "没有", "none"} or _REJECT.search(body):
                continue
            out.append(body[:_BULLET_CHARS])
            if len(out) >= _MAX_BULLETS:
                break
        return out

    # ------------------------------------------------------------ 写
    def note(self, user_id: str, dropped: list[str]) -> None:
        """把被逐字窗口挤出去的那几句送去压缩。上一趟没跑完就并进去，不越积越多。"""
        if not self.enabled or not dropped or user_id in self._running:
            return
        self._running.add(user_id)
        task = asyncio.create_task(self._fold(user_id, [line for line in dropped if line.strip()]))
        self._pending.add(task)

        def _done(finished: asyncio.Task[None]) -> None:
            self._pending.discard(finished)
            self._running.discard(user_id)

        task.add_done_callback(_done)

    async def _fold(self, user_id: str, dropped: list[str]) -> None:
        existing = await self.read(user_id)
        fresh = self._plain(dropped)
        if not fresh:
            self.stats["skipped"] += 1
            return
        merged = await self._ask(existing, fresh)
        if not merged:
            # 压缩没成、或成了却一条都不剩，都不许丢料：
            # 宁可糙一点把原文挂上去，也不能让她下一句真的「不记得」
            merged = self._limit(existing + fresh)
        await self._write(user_id, merged)
        self.stats["folded"] += 1

    @staticmethod
    def _limit(lines: list[str]) -> list[str]:
        return [line[:_BULLET_CHARS] for line in lines if line.strip()][-_MAX_BULLETS:]

    @staticmethod
    def _plain(dropped: list[str]) -> list[str]:
        out: list[str] = []
        for raw in dropped:
            line = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not line or _REJECT.search(line):
                continue
            out.append(line[:_BULLET_CHARS])
        return out

    async def _write(self, user_id: str, bullets: list[str]) -> None:
        path = self.path(user_id)
        body = "\n".join(f"- {line}" for line in bullets)
        content = f"{_HEAD}\n{body}\n" if body else f"{_HEAD}\n"
        def _save() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        try:
            await asyncio.to_thread(_save)
        except OSError as exc:
            logger.warning("%s 回看落盘失败: %s", user_id, exc)
            self.stats["failed"] += 1

    async def _ask(self, existing: list[str], fresh: list[str]) -> list[str] | None:
        key, base = self._settings.extractor_credentials()
        model = self._settings.recap_model or self._settings.extractor_model or self._settings.model
        if not key or not base or not model:
            return None
        from openai import AsyncOpenAI

        if self._client is None:
            self._client = AsyncOpenAI(api_key=key, base_url=base,
                                       timeout=self._settings.recap_timeout, max_retries=0)
        parts = []
        if existing:
            parts.append("已有要点：\n" + "\n".join(f"- {line}" for line in existing))
        parts.append("新添的对话：\n" + "\n".join(fresh))
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _PROMPT + "\n\n".join(parts)}],
                max_tokens=320,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - 压缩失败就退回原文拼接
            logger.info("回看压缩没成: %s", exc.__class__.__name__)
            self.stats["failed"] += 1
            return None
        choice = (getattr(completion, "choices", None) or [None])[0]
        answer = str(getattr(getattr(choice, "message", None), "content", "") or "")
        bullets = self._bullets(answer)
        return bullets or []

    async def wait_idle(self, timeout: float = 20.0) -> None:
        deadline = timeout
        while self._pending and deadline > 0:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
            deadline -= 0.2

    async def aclose(self) -> None:
        await self.wait_idle(6.0)
        for task in list(self._pending):
            task.cancel()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 - 收尾失败不拖别人
                logger.debug("回看客户端关闭异常", exc_info=True)
            self._client = None
