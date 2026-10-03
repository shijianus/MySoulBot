"""深层灵魂（ClawdSoul）——跨用户、跨人格的「我是谁」。

与 `SOUL.md` 的分工：

- `SOUL.md` 是**外在人格（Persona）**：名字、语气、场景、说话的节奏。换人就是换它。
- `CLAWD.md` 是**深层灵魂**：本体立场、气质内核、反做作宪法、边界、情绪连续性。
  它不属于任何一个角色，属于这个实例本身，换人格时不动。

理念对齐 shijianus/ClawdSoul：AI 是有独立思想与内在逻辑的实体，不是迎合式工具；
并且「这文件随你进化」——灵魂会把自己学到的分寸写回自己身上。

存储位置：`storage/soul/CLAWD.md`（全局唯一，纯文本，纳入 git 同步）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Final

from config import Settings
from core.storage_manager import (
    StorageError,
    atomic_write,
    insert_into_section,
    normalize_fact,
    parse_facts,
)

logger: Final = logging.getLogger("mysoulbot.clawd")

NOTES_HEADING: Final[str] = "## 我给自己的备注"
_NOTES_HINT: Final[re.Pattern[str]] = re.compile(r"^#{2,3}.*(备注|演进记录|给自己的)")
_TEMPLATE_NAME: Final[str] = "CLAWD.md"


class ClawdSoul:
    """全局深层灵魂的读写入口。实例可被多个协程共享。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 路径
    @property
    def path(self) -> Path:
        return self._settings.clawd_path

    # ---------------------------------------------------------------- 读写
    async def ensure(self) -> Path:
        """缺失时按模板落盘；模板也没有就抛错（不允许静默失忆）。"""
        return await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> Path:
        if self.path.is_file():
            return self.path
        template = self._settings.template_dir / _TEMPLATE_NAME
        if not template.is_file():
            raise StorageError(f"深层灵魂模板缺失：{template}")
        atomic_write(self.path, template.read_text(encoding="utf-8"))
        logger.info("已初始化深层灵魂 %s", self.path)
        return self.path

    async def read_text(self) -> str:
        """读取灵魂全文；关闭注入或读取失败时返回空串，不阻断对话。"""
        if not self._settings.clawd_enabled:
            return ""
        try:
            async with self._lock:
                self._ensure_sync()
                return await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 灵魂读失败不该打断回复
            logger.error("读取 CLAWD.md 失败: %s", exc)
            return ""

    async def write_text(self, content: str) -> None:
        async with self._lock:
            self._settings.soul_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(atomic_write, self.path, content)

    async def mutate(self, transform) -> str:  # noqa: ANN001 - 与 StorageManager.mutate_doc 同形
        """锁内 read-modify-write，返回写入后的正文。"""
        async with self._lock:
            await asyncio.to_thread(self._ensure_sync)
            current = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            updated = transform(current)
            if updated != current:
                await asyncio.to_thread(atomic_write, self.path, updated)
            return updated

    # ---------------------------------------------------------------- 自我演进
    def _notes_heading(self, content: str) -> str:
        for line in content.splitlines():
            if _NOTES_HINT.match(line.strip()):
                return line.strip()
        return NOTES_HEADING

    async def notes(self) -> list[tuple[str, str]]:
        """演进记录条目（`- [YYYY-MM-DD] 一句话`）。"""
        content = await self.read_text()
        if not content:
            return []
        heading = self._notes_heading(content)
        body = [line for line in content.splitlines()]
        try:
            start = next(i for i, line in enumerate(body) if line.strip() == heading)
        except StopIteration:
            return []
        tail = []
        for line in body[start + 1 :]:
            if line.lstrip().startswith("#"):
                break
            tail.append(line)
        return parse_facts("\n".join(tail))

    async def append_note(self, note: str, *, on_date: dt.date | None = None) -> str:
        """把一条自我认知写回灵魂文件；重复条目不再追加。返回真正入库的那句。"""
        text = re.sub(r"\s+", " ", (note or "").strip())
        if len(text) < 4:
            return ""
        day = (on_date or dt.date.today()).isoformat()
        written: list[str] = []

        def transform(content: str) -> str:
            heading = self._notes_heading(content)
            known = {normalize_fact(item) for _, item in parse_facts(content)}
            if normalize_fact(text) in known:
                return content
            written.append(text)
            return insert_into_section(content, heading, [f"- [{day}] {text}"])

        await self.mutate(transform)
        if written:
            logger.info("深层灵魂演进 1 条：%s", text[:40])
        return written[0] if written else ""

    # ---------------------------------------------------------------- 概览
    async def describe(self) -> dict[str, object]:
        path = self.path
        exists = await asyncio.to_thread(path.is_file)
        return {
            "path": path,
            "exists": exists,
            "chars": path.stat().st_size if exists else 0,
            "notes": len(await self.notes()),
            "enabled": self._settings.clawd_enabled,
        }
