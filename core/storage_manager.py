"""按用户隔离的 Markdown 分层存储。

职责：
- 用户目录与模板初始化
- SOUL / USER / MEMORY / RELATIONS 四份文档的安全读写
  （进程内锁 + POSIX 文件锁 + 原子替换）
- MEMORY 事实与 RELATIONS 关系动态的解析、去重与追加
- 逐轮对话日志（JSONL）读写，用于上下文恢复
- 历史归档与体积闸门：明文日志滚动 gzip、超限条目下沉归档、
  保证任何单个文件都远低于 GitHub 的 100MB 硬线
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import gzip
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import unquote

try:  # POSIX 跨进程锁；缺失时退化为仅进程内锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from config import Settings

logger: Final = logging.getLogger("mysoulbot.storage")

DocName: Final = Literal["SOUL", "USER", "MEMORY", "RELATIONS"]
DOCS: Final[tuple[DocName, ...]] = ("SOUL", "USER", "MEMORY", "RELATIONS")
MEMORY_DOCS: Final[tuple[DocName, ...]] = ("MEMORY", "RELATIONS")

MEMORY_SECTION: Final[str] = "## 事实"
RELATIONS_SECTION: Final[str] = "## 动态"
FACT_LINE: Final[re.Pattern[str]] = re.compile(r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+?)\s*$")
_USER_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"^_[\u4e00-\u9fff(（].*?_\s*$")
_DAY_STEM: Final[re.Pattern[str]] = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# 反思轨的条目在抽取输出里用这个前缀标记；落盘时剥掉，RELATIONS.md 里只留正文
DYNAMIC_MARK: Final[str] = ">>"


class StorageError(RuntimeError):
    """存储层不可恢复的错误（非法路径、模板缺失等）。"""


class PathSafetyError(StorageError):
    """user_id 或路径越界。"""


def normalize_fact(text: str) -> str:
    """用于去重的弱归一化：压缩空白、去尾部标点。"""
    collapsed = re.sub(r"\s+", "", text.strip()).rstrip("。.！!？?，,")
    return collapsed.casefold()


def atomic_write(path: Path, content: str) -> None:
    """同目录临时文件 + os.replace，避免读到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_facts(content: str) -> list[tuple[str, str]]:
    """从 Markdown 正文中提取所有 `- [YYYY-MM-DD] 事实` 行，按出现顺序返回。"""
    facts: list[tuple[str, str]] = []
    for line in content.splitlines():
        match = FACT_LINE.match(line.strip())
        if match:
            facts.append((match.group(1), match.group(2)))
    return facts


def parse_section(content: str, heading: str) -> list[str]:
    """取指定小节（到下一个标题为止）内的非空正文行。"""
    lines = content.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return []
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("#"):
            break
        if line.strip():
            body.append(line)
    return body


def insert_into_section(content: str, heading: str, lines: Sequence[str]) -> str:
    """把条目插到指定小节末尾（下一个标题之前），并清掉空占位行。

    小节不存在时追加到文档末尾，保证长期记忆文件不会被整体重写。
    """
    body = content.rstrip("\n")
    parts = body.splitlines()
    try:
        start = next(i for i, line in enumerate(parts) if line.strip() == heading)
    except StopIteration:
        parts.extend(("", heading, *lines))
        return "\n".join(parts) + "\n"

    end = len(parts)
    for i in range(start + 1, len(parts)):
        if parts[i].lstrip().startswith("#"):
            end = i
            break

    section = [line for line in parts[start + 1 : end] if not _PLACEHOLDER.match(line.strip())]
    while section and not section[-1].strip():
        section.pop()
    rebuilt = parts[: start + 1] + section + list(lines) + parts[end:]
    return "\n".join(rebuilt).rstrip("\n") + "\n"


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}GB"



class StorageManager:
    """所有文件操作的唯一入口。实例可被多个协程共享。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- 路径
    @property
    def template_dir(self) -> Path:
        return self._settings.template_dir

    def user_dir(self, user_id: str) -> Path:
        candidate = unquote(user_id).strip()
        if not _USER_ID.fullmatch(candidate) or ".." in candidate:
            raise PathSafetyError(
                f"非法 user_id: {user_id!r}（允许字母数字开头，可含 _ . - ，长度 1-64）"
            )
        path = (self._settings.users_dir / candidate).resolve()
        users_root = self._settings.users_dir.resolve()
        if path.parent != users_root:
            raise PathSafetyError(f"user_id {candidate!r} 解析越出了用户目录")
        return path

    def doc_path(self, user_id: str, doc: DocName) -> Path:
        return self.user_dir(user_id) / f"{doc}.md"

    def logs_dir(self, user_id: str) -> Path:
        return self.user_dir(user_id) / "logs"

    def state_path(self, user_id: str) -> Path:
        """运行时体温（情绪余温、耐心、熟络度计数）；不是长期记忆资产，不进版本库。"""
        return self.user_dir(user_id) / "state.json"

    async def read_state(self, user_id: str) -> dict[str, Any]:
        path = self.state_path(user_id)
        async with self._critical(path):
            if not await asyncio.to_thread(path.is_file):
                return {}
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("state.json 损坏（%s）：%s，按空状态处理", path, exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    async def write_state(self, user_id: str, state: dict[str, Any]) -> None:
        path = self.state_path(user_id)
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        async with self._critical(path):
            await asyncio.to_thread(atomic_write, path, payload)

    def artifacts_dir(self, user_id: str) -> Path:
        """工具产物（图片、快照）落这里；默认不入 git。"""
        return self.user_dir(user_id) / "artifacts"

    def audio_dir(self, user_id: str) -> Path:
        """念出来的声音落这里——同样是产物，不该进版本库。"""
        return self.artifacts_dir(user_id) / "audio"

    def memory_archive_dir(self, user_id: str) -> Path:
        """被下沉的记忆条目落这里。它是记忆资产的一部分，跟着仓库一起走。"""
        return self.user_dir(user_id) / "archive"

    def logs_archive_dir(self, user_id: str) -> Path:
        return self.logs_dir(user_id) / "archive"

    # ---------------------------------------------------------------- 并发
    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def _critical(self, path: Path):
        """进程内互斥 + 文件锁，覆盖 read-modify-write 全程。"""
        async with self._lock_for(str(path)):
            handle = await asyncio.to_thread(self._acquire_file_lock, path)
            try:
                yield
            finally:
                await asyncio.to_thread(self._release_file_lock, handle)

    @staticmethod
    def _acquire_file_lock(path: Path):
        if fcntl is None:
            return None
        lock_path = path.with_name(f".{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            handle.close()
            logger.warning("文件锁获取失败 %s: %s", path.name, exc)
            return None
        return handle

    @staticmethod
    def _release_file_lock(handle) -> None:
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.debug("文件锁释放异常", exc_info=True)
        finally:
            handle.close()

    # ---------------------------------------------------------------- 初始化
    async def ensure_user(self, user_id: str, soul_text: str | None = None) -> Path:
        """建目录 + 缺失文档用模板初始化，返回用户目录。"""
        directory = self.user_dir(user_id)
        await asyncio.to_thread(self._init_user_dir, directory, user_id, soul_text)
        return directory

    def _init_user_dir(self, directory: Path, user_id: str, soul_text: str | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "logs").mkdir(parents=True, exist_ok=True)
        for doc in DOCS:
            target = directory / f"{doc}.md"
            if target.exists():
                continue
            if doc == "SOUL" and soul_text:
                body = soul_text
            else:
                body = self._read_template(doc)
            atomic_write(target, body)
            logger.info("已为 %s 初始化 %s.md", user_id, doc)

    def _read_template(self, doc: DocName) -> str:
        path = self.template_dir / f"{doc}.md"
        if not path.is_file():
            raise StorageError(f"模板缺失: {path}")
        return path.read_text(encoding="utf-8")

    # ---------------------------------------------------------------- 文档读写
    async def read_doc(self, user_id: str, doc: DocName) -> str:
        """读取文档；文件不存在时先用模板落盘再读，保证返回非空。"""
        path = self.doc_path(user_id, doc)
        async with self._critical(path):
            await asyncio.to_thread(self._ensure_doc_file, path, doc)
            return await asyncio.to_thread(path.read_text, encoding="utf-8")

    def _ensure_doc_file(self, path: Path, doc: DocName) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, self._read_template(doc))
        logger.info("%s 缺失，已按模板补建", path)

    async def write_doc(self, user_id: str, doc: DocName, content: str) -> None:
        path = self.doc_path(user_id, doc)
        async with self._critical(path):
            await asyncio.to_thread(atomic_write, path, content)

    async def mutate_doc(self, user_id: str, doc: DocName, transform: Callable[[str], str]) -> str:
        """在锁内做 read-modify-write，返回写入后的正文。"""
        path = self.doc_path(user_id, doc)

        async with self._critical(path):
            await asyncio.to_thread(self._ensure_doc_file, path, doc)
            current = await asyncio.to_thread(path.read_text, encoding="utf-8")
            updated = transform(current)
            if updated != current:
                await asyncio.to_thread(atomic_write, path, updated)
            return updated

    # ------------------------------------------------------------ 人格元数据与备份
    def persona_meta_path(self, user_id: str) -> Path:
        return self.user_dir(user_id) / "persona.json"

    def backups_dir(self, user_id: str) -> Path:
        return self.user_dir(user_id) / "backups"

    async def read_persona_meta(self, user_id: str) -> dict[str, Any]:
        """读取当前应用的人格；缺失或损坏都返回空 dict（不阻断对话）。"""
        path = self.persona_meta_path(user_id)

        async with self._critical(path):
            if not await asyncio.to_thread(path.is_file):
                return {}
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("persona.json 损坏（%s）：%s，按未应用人格处理", path, exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    async def write_persona_meta(self, user_id: str, meta: dict[str, Any]) -> None:
        path = self.persona_meta_path(user_id)
        payload = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
        async with self._critical(path):
            await asyncio.to_thread(atomic_write, path, payload)

    async def backup_doc(self, user_id: str, doc: DocName, *, keep: int = 5) -> Path | None:
        """把当前版本存进 backups/，保留最近 keep 份。返回备份路径，原文缺失则 None。"""
        source = self.doc_path(user_id, doc)
        if not await asyncio.to_thread(source.is_file):
            return None
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.backups_dir(user_id) / f"{doc}-{stamp}.md"

        def run() -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            siblings = sorted(self.backups_dir(user_id).glob(f"{doc}-*.md"))[:-keep]
            for stale in siblings:
                stale.unlink(missing_ok=True)
            return target

        return await asyncio.to_thread(run)

    # ---------------------------------------------------------------- 记忆
    async def read_facts(self, user_id: str) -> list[tuple[str, str]]:
        """解析 MEMORY.md 中全部 `- [YYYY-MM-DD] 事实`，按文件顺序返回。"""
        content = await self.read_doc(user_id, "MEMORY")
        return parse_facts(content)

    async def read_relations(self, user_id: str) -> list[tuple[str, str]]:
        """解析 RELATIONS.md 中的关系动态条目（与事实条目同一行格式，独立文件互不污染）。"""
        content = await self.read_doc(user_id, "RELATIONS")
        return parse_facts(content)

    async def append_facts(
        self,
        user_id: str,
        facts: Iterable[str],
        *,
        on_date: dt.date | None = None,
    ) -> list[str]:
        """追加事实，跳过重复与非法条目，返回真正写入的条目文本。"""
        return await self._append_entries(user_id, "MEMORY", MEMORY_SECTION, facts, on_date)

    async def append_dynamics(
        self,
        user_id: str,
        dynamics: Iterable[str],
        *,
        on_date: dt.date | None = None,
    ) -> list[str]:
        """追加关系动态（态度演变、相处心得），写入 RELATIONS.md。"""
        return await self._append_entries(user_id, "RELATIONS", RELATIONS_SECTION, dynamics, on_date)

    async def _append_entries(
        self,
        user_id: str,
        doc: DocName,
        heading: str,
        entries: Iterable[str],
        on_date: dt.date | None,
    ) -> list[str]:
        """两条记忆轨共用的追加逻辑：去重 → 插入指定小节 → 超限则归档下沉。"""
        day = (on_date or dt.date.today()).isoformat()
        candidates = [e.strip().removeprefix(DYNAMIC_MARK).strip() for e in entries if e and e.strip()]
        candidates = [e for e in candidates if e]
        if not candidates:
            return []

        written: list[str] = []

        def transform(content: str) -> str:
            known = {normalize_fact(text) for _, text in parse_facts(content)}
            fresh: list[str] = []
            for entry in candidates:
                key = normalize_fact(entry)
                if not key or key in known:
                    continue
                known.add(key)
                fresh.append(entry)
            if not fresh:
                return content
            written.extend(fresh)
            lines = [f"- [{day}] {entry}" for entry in fresh]
            return insert_into_section(content, heading, lines)

        await self.mutate_doc(user_id, doc, transform)
        if written:
            logger.info("%s %s 写入 %d 条", user_id, doc, len(written))
        return written

    async def clear_facts(self, user_id: str) -> None:
        def transform(content: str) -> str:
            kept = [
                line
                for line in content.splitlines()
                if not FACT_LINE.match(line.strip())
            ]
            return "\n".join(kept) + "\n"

        await self.mutate_doc(user_id, "MEMORY", transform)

    async def compact_memory(self, user_id: str, *, doc: DocName = "MEMORY") -> int:
        """长期记忆超过阈值时，把最老的条目下沉到归档，返回下沉条数。

        只做「搬家」不做「删除」：条目原文先进 `archive/<DOC>-*.md.gz`，再重写文档。
        整个过程持同一把文档锁——中途崩溃不会凭空少条目。
        """
        if doc not in MEMORY_DOCS:
            raise StorageError(f"compact_memory 只处理记忆档，不处理 {doc}")
        threshold = self._settings.memory_compact_threshold
        path = self.doc_path(user_id, doc)
        heading = MEMORY_SECTION if doc == "MEMORY" else RELATIONS_SECTION
        moved = 0

        async with self._critical(path):
            await asyncio.to_thread(self._ensure_doc_file, path, doc)
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            entries = parse_facts(content)
            if len(entries) <= threshold:
                return 0
            keep_from = len(entries) - threshold
            evicted, kept = entries[:keep_from], entries[keep_from:]
            await self._archive_entries(user_id, doc, evicted)
            stripped = "\n".join(
                line for line in content.splitlines() if not FACT_LINE.match(line.strip())
            )
            rebuilt = insert_into_section(
                stripped, heading, [f"- [{day}] {text}" for day, text in kept]
            )
            await asyncio.to_thread(atomic_write, path, rebuilt)
            moved = len(evicted)

        logger.info("%s %s 下沉 %d 条到归档", user_id, doc, moved)
        return moved

    async def _archive_entries(
        self, user_id: str, doc: DocName, entries: Sequence[tuple[str, str]]
    ) -> Path:
        """下沉条目写进 gzip 归档；同秒重名自动加序号，绝不覆盖已有归档。"""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = self.memory_archive_dir(user_id)

        def run() -> Path:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{doc}-{stamp}.md.gz"
            index = 1
            while target.exists():
                target = directory / f"{doc}-{stamp}.{index}.md.gz"
                index += 1
            with gzip.open(target, "wt", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(f"- [{day}] {text}" for day, text in entries) + "\n")
            return target

        return await asyncio.to_thread(run)

    @staticmethod
    def read_gz(path: Path) -> str:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()

    # ---------------------------------------------------------------- 日志
    async def append_transcript(
        self,
        user_id: str,
        entries: Sequence[dict[str, Any]],
    ) -> Path | None:
        """按天追加 JSONL 日志；关闭持久化或出错时返回 None。"""
        if not self._settings.persist_transcript or not entries:
            return None
        path = self.logs_dir(user_id) / f"{dt.date.today().isoformat()}.jsonl"
        payload = "".join(
            json.dumps({"ts": _now_iso(), **entry}, ensure_ascii=False) + "\n" for entry in entries
        )
        try:
            async with self._critical(path):
                await asyncio.to_thread(_append_text, path, payload)
                if path.stat().st_size > self._settings.log_max_file_bytes:
                    # 当天就写爆了：在自己的锁内整份归档 + 留尾，不会和追加互相踩
                    await asyncio.to_thread(
                        self._split_in_place, path, self._settings.log_max_file_bytes // 2
                    )
        except OSError as exc:
            logger.warning("对话日志写入失败: %s", exc)
            return None
        return path

    def _split_in_place(self, path: Path, budget: int) -> None:
        """当天日志写爆：整份归档后，明文里只留最近的若干行。必须在 append 的锁内调用。"""
        self._gzip_into(path.parent, path)
        self._keep_tail(path, budget)

    async def read_recent_transcript(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        """跨天日志中取最近 limit 条消息，用于重启后恢复上下文。"""
        if limit <= 0:
            return []
        return await asyncio.to_thread(self._read_recent_sync, user_id, limit)

    def _read_recent_sync(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        directory = self.logs_dir(user_id)
        if not directory.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.jsonl"))[-4:]:
            try:
                raw = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.warning("日志读取失败 %s: %s", path, exc)
                continue
            for line in raw[-limit:]:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("跳过损坏日志行: %.80s", line)
                    continue
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                    records.append(item)
        return records[-limit:]

    # ------------------------------------------------------------ 归档与体积闸门
    async def rotate_transcripts(self, user_id: str, *, today: dt.date | None = None) -> dict[str, Any]:
        """滚动归档：过期日志整份 gzip、总量超限从最旧开始压、归档按期清理。

        只碰**过往**日期的日志。当天文件由 `append_transcript` 在自己的锁内切尾，
        否则「读旧值→压缩→删」与并发追加交错，会真的丢掉几行对话。
        """
        async with self._lock_for(f"rotate:{user_id}"):
            return await asyncio.to_thread(self._rotate_sync, user_id, today or dt.date.today())

    def _rotate_sync(self, user_id: str, today: dt.date) -> dict[str, Any]:
        s = self._settings
        logs = self.logs_dir(user_id)
        archived: list[str] = []
        over_budget: list[str] = []
        if not logs.is_dir():
            return {"archived": archived, "plain_bytes": 0, "over_budget": over_budget}

        for doc in DOCS:
            path = self.doc_path(user_id, doc)
            if path.is_file() and path.stat().st_size > s.doc_max_bytes:
                over_budget.append(f"{doc}.md({_human_size(path.stat().st_size)})")

        history = [
            path
            for path in sorted(logs.glob("*.jsonl"))
            if _stem_day(path) is not None and _stem_day(path) != today
        ]

        # 1) 过往某天单独超限：整份归档 + 留最近尾段（上下文恢复仍可用）
        for path in history:
            if path.stat().st_size > s.log_max_file_bytes:
                archived.append(self._gzip_into(logs, path).name)
                self._keep_tail(path, s.log_max_file_bytes // 2)

        # 2) 过期整天：整份归档后删除明文
        for path in history:
            day = _stem_day(path)
            if day is not None and (today - day).days >= s.log_keep_days and path.is_file():
                archived.append(self._gzip_into(logs, path).name)
                path.unlink(missing_ok=True)

        # 3) 明文总量仍超限：从最旧开始压
        plain_bytes = sum(p.stat().st_size for p in logs.glob("*.jsonl") if p.is_file())
        for path in sorted(history, key=lambda p: (_stem_day(p) or dt.date.min, p.name)):
            if plain_bytes <= s.log_max_total_bytes:
                break
            if not path.is_file():
                continue
            size = path.stat().st_size
            archived.append(self._gzip_into(logs, path).name)
            path.unlink(missing_ok=True)
            plain_bytes -= size

        # 4) 日志归档老化（记忆归档不清）
        archive = self.logs_archive_dir(user_id)
        if archive.is_dir():
            for gz in archive.glob("*.gz"):
                day = _stem_day(Path(gz.name.removesuffix(".gz")))
                age_days = (today - day).days if day else _mtime_days(gz, today)
                if age_days > s.archive_keep_days:
                    gz.unlink(missing_ok=True)

        plain_bytes = sum(p.stat().st_size for p in logs.glob("*.jsonl") if p.is_file())
        return {"archived": archived, "plain_bytes": plain_bytes, "over_budget": over_budget}

    @staticmethod
    def _keep_tail(path: Path, budget: int) -> None:
        """整份已归档之后，明文里只保留最近的若干行。"""
        tail: list[str] = []
        kept = 0
        for line in reversed(path.read_text(encoding="utf-8").splitlines(True)):
            size = len(line.encode("utf-8"))
            if kept + size > budget:
                break
            tail.append(line)
            kept += size
        atomic_write(path, "".join(reversed(tail)))

    @staticmethod
    def _gzip_into(logs: Path, source: Path) -> Path:
        """把明文日志压进 logs/archive/，同名则加序号，绝不覆盖已有归档。"""
        archive = logs / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / f"{source.name}.gz"
        index = 1
        while target.exists():
            target = archive / f"{source.stem}.{index}.jsonl.gz"
            index += 1
        with gzip.open(target, "wt", encoding="utf-8", newline="\n") as dst, open(
            source, encoding="utf-8"
        ) as src:
            dst.write(src.read())
        return target

    # ---------------------------------------------------------------- 概览
    async def describe(self, user_id: str) -> dict[str, Any]:
        """当前用户的存储概览，供 `/panel status` 使用。"""
        directory = self.user_dir(user_id)
        docs: dict[str, dict[str, Any]] = {}
        for doc in DOCS:
            path = self.doc_path(user_id, doc)
            exists = await asyncio.to_thread(path.is_file)
            docs[doc] = {
                "path": path,
                "exists": exists,
                "chars": path.stat().st_size if exists else 0,
            }
        logs = sorted(self.logs_dir(user_id).glob("*.jsonl"))
        meta = await self.read_persona_meta(user_id)
        rotation = await self.rotate_transcripts(user_id)
        return {
            "user_id": user_id,
            "dir": directory,
            "exists": directory.is_dir(),
            "docs": docs,
            "facts": len(await self.read_facts(user_id)),
            "relations": len(await self.read_relations(user_id)),
            "log_files": [p.name for p in logs],
            "archived": rotation["archived"],
            "persona_slug": str(meta.get("slug") or ""),
            "persona_name": str(meta.get("name") or ""),
            "persona_config": meta.get("config") if isinstance(meta.get("config"), dict) else {},
            "backups": len(list(self.backups_dir(user_id).glob("*.md")))
            if self.backups_dir(user_id).is_dir()
            else 0,
            "bytes": rotation["plain_bytes"],
            "over_budget": rotation["over_budget"],
        }


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _stem_day(path: Path) -> dt.date | None:
    """从 `2026-10-03.jsonl` 之类的文件名里取出日期，取不到返回 None。"""
    match = _DAY_STEM.match(path.stem)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _mtime_days(path: Path, today: dt.date) -> int:
    try:
        modified = dt.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return 0
    return max(0, (today - modified).days)


def scan_size_gate(
    root: Path,
    limit: int,
    *,
    skip: frozenset[str] = frozenset({".git", ".venv", "__pycache__", "node_modules", ".mypy_cache"}),
) -> list[tuple[Path, int]]:
    """列出体积超过闸门的文件。

    同步脚本用它把大文件挡在 `git add` 之前：GitHub 单文件 100MB 是硬墙，
    撞上去会留下无法轻易清除的仓库对象，所以宁可本地拒绝。
    """
    offenders: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > limit:
                offenders.append((path, size))
    return sorted(offenders, key=lambda item: -item[1])


def _append_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def build_storage(settings: Settings) -> StorageManager:
    """构造存储层并确保目录存在。"""
    settings.ensure_directories()
    return StorageManager(settings)
