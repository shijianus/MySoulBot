"""记忆向量索引：按「像不像」取记忆，而不是按「新不新」取。

旧行为是把 MEMORY/RELATIONS 里最近 N 条原样塞进提示词。人不是这么想起一件事的——
他是因为**这句话让我想起了那件事**才提起。所以这里：

- 存 sqlite（`storage/data/vectors.db`），一行一条记忆，向量按 float32 打包。
  不引第三方向量库：这台机器上 numpy 都没有，装一个 faiss 只是把「clone 即用」变成
  「clone 完先编译半小时」。纯 python 点积在这个量级（每人几百条）完全够。
- **按人分库键**：查的时候只在该用户自己的行里找。跨人召回不是"更聪明"，是串味，
  而串味正好是 `secrecy` 那层在挡的事。
- **后端换了就重建**：Cohere 的 1024 维和本地 384 维不能混在一张表里比距离，
  所以每条都记 `backend` 与 `dim`，对不上就当脏数据重建，不做半新半旧的检索。
- **写不进去不许挡回话**：索引是加速器，不是真相来源。md 文件才是。
  向量化失败、库被锁、磁盘满——一律降级成「这次按老办法取最近 N 条」，
  对话照走。记忆不能因为一个索引坏了就丢。

真相来源永远是那些纯 md 文件；这个库只是让人想起东西的那条路。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from config import Settings
from core.embeddings import Embedder

logger: Final = logging.getLogger("mysoulbot.vector")

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS memories (
    key      TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL,
    backend  TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL,
    day      TEXT NOT NULL,
    updated  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, kind);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_BUSY_TIMEOUT_MS: Final[int] = 4000


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def _key(user_id: str, kind: str, text: str) -> str:
    digest = hashlib.blake2b(f"{user_id}\x00{kind}\x00{text}".encode("utf-8"), digest_size=12)
    return digest.hexdigest()


@dataclass(frozen=True)
class Hit:
    text: str
    kind: str
    day: str
    score: float


class VectorIndex:
    """记忆的向量索引。所有阻塞调用都在 `asyncio.to_thread` 里跑。"""

    def __init__(self, settings: Settings, embedder: Embedder | None = None) -> None:
        self._settings = settings
        self.embedder = embedder or Embedder(settings)
        self._path = settings.storage_dir / "data" / "vectors.db"
        self._broken = ""

    @property
    def enabled(self) -> bool:
        return bool(self._settings.vector_enabled) and not self._broken

    @property
    def status(self) -> dict[str, Any]:
        """给人看的现状。退化成词法检索时必须在这里说得出来，不许悄悄装作在跑向量。"""
        info: dict[str, Any] = {
            "enabled": self._settings.vector_enabled,
            "backend": self.embedder.backend,
            "db": self._path.name,
        }
        if self._broken:
            info["broken"] = self._broken
        if self.embedder.degraded_because:
            info["degraded"] = self.embedder.degraded_because
        return info

    # ------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=%d" % _BUSY_TIMEOUT_MS)
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        # 换过后端就把旧向量整体丢掉：不同来源的向量比距离没有意义
        probe = conn.execute("SELECT value FROM meta WHERE name='backend'").fetchone()
        want = self.embedder.backend
        if probe is not None and probe[0] != want:
            conn.execute("DELETE FROM memories")
            logger.info("向量后端从 %s 换成 %s，旧向量整体作废", probe[0], want)
        conn.execute("INSERT OR REPLACE INTO meta(name,value) VALUES('backend',?)", (want,))
        conn.commit()

    # ------------------------------------------------------------ 写入
    def upsert_sync(self, user_id: str, kind: str, entries: list[tuple[str, str]]) -> int:
        """把 (日期, 正文) 一批写进索引。返回新写入的条数。"""
        if not self._settings.vector_enabled or not entries:
            return 0
        texts = [text for _day, text in entries if text.strip()]
        if not texts:
            return 0
        try:
            vectors = self.embedder.embed(texts)
        except Exception as exc:  # noqa: BLE001 - 向量化失败只是少个加速器
            self._broken = f"向量化失败：{exc}"
            logger.warning("%s", self._broken)
            return 0
        written = 0
        try:
            with self._connect() as conn:
                self._init(conn)
                for (day, text), vec in zip(entries, vectors):
                    if not text.strip():
                        continue
                    if len(vec) > self._settings.vector_dim_guard:
                        self._broken = f"向量维度 {len(vec)} 超出上限，索引暂停"
                        return written
                    key = _key(user_id, kind, text)
                    had = conn.execute("SELECT 1 FROM memories WHERE key=?", (key,)).fetchone()
                    conn.execute(
                        "INSERT OR REPLACE INTO memories"
                        "(key,user_id,kind,text,backend,dim,vec,day,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, user_id, kind, text[:600], self.embedder.backend, len(vec),
                         _pack(vec), day, time.time()))
                    written += int(had is None)
                conn.commit()
        except sqlite3.Error as exc:
            self._broken = f"索引写入失败：{exc}"
            logger.warning("%s", self._broken)
        return written

    async def upsert(self, user_id: str, kind: str, entries: list[tuple[str, str]]) -> int:
        return await asyncio.to_thread(self.upsert_sync, user_id, kind, entries)

    # ------------------------------------------------------------ 检索
    def search_sync(self, user_id: str, query: str, *, kinds: tuple[str, ...] = (),
                    limit: int = 6, floor: float = 0.12) -> list[Hit]:
        body = (query or "").strip()
        if not self._settings.vector_enabled or not body or self._broken:
            return []
        try:
            target = self.embedder.embed([body], query=True)[0]
        except Exception as exc:  # noqa: BLE001 - 检索失败不该影响回话
            self._broken = f"查询向量化失败：{exc}"
            return []
        sql = "SELECT text,kind,day,vec,dim FROM memories WHERE user_id=?"
        args: list[Any] = [user_id]
        if kinds:
            sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            args.extend(kinds)
        try:
            with self._connect() as conn:
                self._init(conn)
                rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            self._broken = f"索引读取失败：{exc}"
            return []
        scored: list[Hit] = []
        for text, kind, day, blob, dim in rows:
            if dim != len(target):
                continue  # 换过维度没重建干净的行，跳过而不是算个错分
            vec = _unpack(blob)
            score = sum(a * b for a, b in zip(target, vec))
            if score >= floor:
                scored.append(Hit(text=text, kind=kind, day=day, score=round(score, 4)))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:limit]

    async def search(self, user_id: str, query: str, *, kinds: tuple[str, ...] = (),
                     limit: int = 6, floor: float = 0.12) -> list[Hit]:
        return await asyncio.to_thread(self.search_sync, user_id, query,
                                       kinds=kinds, limit=limit, floor=floor)

    # ------------------------------------------------------------ 维护
    def forget_sync(self, user_id: str, kind: str, texts: list[str]) -> int:
        keys = [_key(user_id, kind, text) for text in texts]
        if not keys:
            return 0
        try:
            with self._connect() as conn:
                conn.executemany("DELETE FROM memories WHERE key=?", [(k,) for k in keys])
                conn.commit()
        except sqlite3.Error:
            return 0
        return len(keys)

    def count_sync(self, user_id: str = "") -> int:
        try:
            with self._connect() as conn:
                self._init(conn)
                if user_id:
                    row = conn.execute("SELECT COUNT(*) FROM memories WHERE user_id=?",
                                       (user_id,)).fetchone()
                else:
                    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    async def reindex(self, corpus: dict[str, list[tuple[str, str, str]]]) -> int:
        """全量重建：`{user_id: [(kind, day, text), ...]}`。换后端之后手动跑一次。"""
        try:
            with self._connect() as conn:
                self._init(conn)
                conn.execute("DELETE FROM memories")
                conn.commit()
        except sqlite3.Error as exc:
            self._broken = f"重建失败：{exc}"
            return 0
        total = 0
        for user_id, rows in corpus.items():
            bucket: dict[str, list[tuple[str, str]]] = {}
            for kind, day, text in rows:
                bucket.setdefault(kind, []).append((day, text))
            for kind, entries in bucket.items():
                total += await self.upsert(user_id, kind, entries)
        return total


def build_index(settings: Settings) -> VectorIndex:
    return VectorIndex(settings)
