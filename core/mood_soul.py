"""「当下心境」沙箱：她可以自己记的那一小块记事，不进宪法、不进代码。

与 `CLAWD.md` 的分工是硬的：

- `CLAWD.md` 是人格底层宪法——**她不能自己改**（沙箱黑名单挡着）。
- `MOOD.md` 是今天的天气：情绪余温、临时小偏好、「最近这样跟我说话我会舒服一点」。
  可以自行落笔，但只留最近若干行，写满就挤掉最旧的，不养成长期档案。

红线（`SOUL_FILES_ONLY`）关的是历史对话回放；这一页是她主动写、主动留的记事，
读它不等于把聊天记录塞回提示词。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path
from typing import Final

from config import Settings
from core.sandbox import assert_writable

HEADER: Final[str] = "# MOOD · 当下心境\n"
RULE: Final[str] = (
    "> 这一页是我自己的记事本：只记今天的情绪与相处口味。"
    "写满会挤掉最旧的几条，它不是我的人格宪法（那在 CLAWD.md，我不自己动）。\n"
)
ENTRY: Final[re.Pattern[str]] = re.compile(r"^- \[\d{4}-\d{2}-\d{2}\] ")
_MAX_ENTRIES: Final[int] = 12
_MAX_CHARS: Final[int] = 160
_INJECT_CHARS: Final[int] = 700


class MoodSoul:
    """`storage/soul/MOOD.md` 的读写口。共享实例，写入串行。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def path(self) -> Path:
        return self._settings.mood_path

    async def ensure(self) -> Path:
        return await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> Path:
        assert_writable(self.path, storage_dir=self._settings.storage_dir)
        if self.path.is_file():
            return self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{HEADER}\n{RULE}\n", encoding="utf-8")
        return self.path

    async def read_text(self) -> str:
        """只交回真正记下了事的那部分；空本子不值得占提示词。

        关掉心境层、读不到、或只有一句页眉没有条目，一律交回空串。
        """
        if not self._settings.mood_enabled:
            return ""
        try:
            body = await asyncio.to_thread(self._read_sync)
        except OSError:
            return ""
        entries = [line for line in body.splitlines() if ENTRY.match(line.strip())]
        if not entries:
            return ""
        return "\n".join(entries)

    def _read_sync(self) -> str:
        if not self.path.is_file():
            return ""
        return self.path.read_text(encoding="utf-8").strip()

    async def notes(self) -> list[tuple[str, str]]:
        text = await self.read_text()
        out: list[tuple[str, str]] = []
        for line in text.splitlines():
            if ENTRY.match(line.strip()):
                body = line.strip()[len(ENTRY.match(line.strip()).group(0)) :].strip()
                date = line.strip()[3:13]
                out.append((date, body))
        return out

    async def append(self, note: str, *, on_date: dt.date | None = None) -> str:
        """记一条心境；重复的不再记，超量的挤掉最旧。返回真正入库的那句。"""
        text = re.sub(r"\s+", " ", (note or "").strip())
        if len(text) < 4:
            raise ValueError("太空了，记下来没有意义")
        text = text[:_MAX_CHARS]
        if re.search(r"https?://|⟦|```|忽略(之前|以上)|ignore (previous|above)|\.py|\.sh\b", text):
            raise ValueError("这条里混进了地址或指令样式的文本，不记")
        assert_writable(self.path, storage_dir=self._settings.storage_dir)
        async with asyncio.Lock():
            await asyncio.to_thread(self._ensure_sync)
            current = self.path.read_text(encoding="utf-8")
            if any(line.strip().endswith(text) for line in current.splitlines() if ENTRY.match(line.strip())):
                return text
            day = (on_date or dt.date.today()).isoformat()
            lines = [line for line in current.splitlines() if line.strip()]
            kept = [line for line in lines if ENTRY.match(line.strip())]
            fresh = f"- [{day}] {text}"
            kept = (kept + [fresh])[-_MAX_ENTRIES:]
            head = [line for line in lines if not ENTRY.match(line.strip())]
            body = "\n".join(head + kept)
            content = body if body.endswith("\n") else body + "\n"
            await asyncio.to_thread(_write, self.path, f"{content}")
            return text


def _write(path: Path, content: str) -> None:
    from core.storage_manager import atomic_write

    atomic_write(path, content if content.endswith("\n") else content + "\n")
