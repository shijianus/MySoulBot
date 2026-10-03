"""面板视图：把内核的状态翻成一张能被安心盯着看的脸。

这是**只读**的一层。它读 `state.json`、四份文档与归档，把它们整理成 JSON 给浏览器；
它不写任何东西——熟络度只能由引擎在真实回合里攒出来，面板连一个 PATCH 的入口都没有。
温度若在网页上可拖，它就不再是温度了，那是进度条。

沉浸优先：路径、模型参数、文件名、字节数一律不出去。手机上是「她现在什么样」，
不是「storage/data/users/guest/state.json 的哪个键」。

`build_status` 走的是与 `/panel` 完全相同的计算函数（`build_presence` /
`RapportEngine.read`），所以网页上看到的体温与终端里看到的不会是两具身体。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Final

from config import Settings
from core.presence import build_presence, resolve_now
from core.rapport import RapportEngine
from core.storage_manager import StorageManager, parse_facts

logger: Final = logging.getLogger("mysoulbot.panel")

TIMELINE_LIMIT: Final[int] = 160
DOC_MAX_CHARS: Final[int] = 40_000
ACTIVITY_DAYS: Final[int] = 14
DOCS: Final[tuple[str, ...]] = ("USER", "MEMORY", "RELATIONS", "SOUL")
_DAY: Final[re.Pattern[str]] = re.compile(r"^(\d{4}-\d{2}-\d{2})")

_MOOD_WORDS: Final[tuple[tuple[float, str], ...]] = ((0.15, "松"), (-0.15, "堵"))
_FEEL_FLAT: Final[str] = "平"


def mood_word(valence: float) -> str:
    """心情只给三个字：真人报状态也就是这个粒度。"""
    for threshold, word in _MOOD_WORDS:
        if (threshold > 0 and valence >= threshold) or (threshold < 0 and valence <= threshold):
            return word
    return _FEEL_FLAT


def _percent(value: float) -> int:
    return max(0, min(100, int(round(value * 100))))


def _gap_phrase(days: float | None) -> str:
    if not days:
        return ""
    if days < 1:
        return "不到一天没说话"
    if days < 2:
        return "一天多没说话"
    if days < 30:
        return f"{int(days)} 天没说话"
    return f"{int(days // 30)} 个多月没说话"


async def build_status(settings: Settings, storage: StorageManager, user_id: str) -> dict[str, Any]:
    """此刻的她：人格、温度、体温、生理节律。全只读。"""
    state, meta, facts, relations = await asyncio.gather(
        storage.read_state(user_id),
        storage.read_persona_meta(user_id),
        storage.read_facts(user_id),
        storage.read_relations(user_id),
    )
    now = resolve_now(None, settings.user_timezone)
    presence = build_presence(settings, state, now)
    rapport = await RapportEngine(settings, storage).read(user_id)
    counters = dict(state.get("rapport") or {})
    felt = presence.mood.current(now, settings.mood_half_life_minutes)
    days_together = counters.get("days")
    return {
        "user_id": user_id,
        "persona": {
            "slug": str(meta.get("slug") or ""),
            "name": str(meta.get("name") or "") or "（还没定下的那个人）",
            "title": str(meta.get("title") or ""),
            "applied_at": str(meta.get("applied_at") or ""),
        },
        "rapport": {
            "score": rapport.value,
            "stage": rapport.stage,
            "label": rapport.label,
            "conduct": rapport.conduct,
            "peak": int(round(rapport.peak)),
            "evidence": list(rapport.evidence),
            "editable": False,
        },
        "rhythm": {
            "at": now.isoformat(timespec="seconds"),
            "clock": now.strftime("%H:%M"),
            "slot": presence.slot.label,
            "body": presence.slot.body,
            "conduct": presence.slot.conduct,
            "deep_night": presence.deep_night,
            "gap": _gap_phrase(presence.gap_days if settings.rhythm_enabled else None),
        },
        "mood": {
            "word": mood_word(felt) if settings.rhythm_enabled else _FEEL_FLAT,
            "residual": _percent(presence.mood_residual),
            "cause": presence.mood.cause if presence.mood_residual >= 0.15 else "",
            "valence": round(felt, 3),
        },
        "energy": {
            "patience": _percent(presence.patience.left),
            "turns_today": presence.patience.turns_today,
        },
        "memory": {
            "facts": len(facts),
            "dynamics": len(relations),
            "days_together": len(days_together) if isinstance(days_together, list) else 0,
            "turns": int(counters.get("turns") or 0),
        },
        "visible": settings.panel_enabled,
    }


def _read_day(path: Path, limit: int) -> list[dict[str, Any]]:
    """从一天的 JSONL 里取出轮数与最后几句话。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError as exc:
        logger.debug("日志读不动 %s: %s", path, exc)
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
            out.append(item)
    return out


def activity_of(storage: StorageManager, user_id: str) -> list[dict[str, Any]]:
    """相处动态：最近若干天，每天说了多少句。时光机的横轴。"""
    directory = storage.logs_dir(user_id)
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl"))[-ACTIVITY_DAYS:]:
        match = _DAY.match(path.stem)
        if not match:
            continue
        entries = _read_day(path, 400)
        rows.append(
            {
                "date": match.group(1),
                "turns": sum(1 for item in entries if item.get("role") == "user"),
                "words": sum(len(str(item.get("content", ""))) for item in entries),
            }
        )
    return rows


def archive_facts(storage: StorageManager, user_id: str, doc: str) -> list[tuple[str, str]]:
    """归档里的事实：条目被下沉到 archive/*.md.gz 之后，时光机仍然要能翻到。"""
    directory = storage.memory_archive_dir(user_id)
    if not directory.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for path in sorted(directory.glob(f"{doc}-*.md.gz")):
        try:
            entries.extend(parse_facts(StorageManager.read_gz(path)))
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            logger.debug("归档读不动 %s: %s", path, exc)
    return entries


async def build_timeline(
    storage: StorageManager, user_id: str, *, limit: int = TIMELINE_LIMIT
) -> dict[str, Any]:
    """记忆时光机：她眼中的你、彼此的事实、相处攒下的分寸，加归档。"""
    facts, relations, profile = await asyncio.gather(
        storage.read_facts(user_id), storage.read_relations(user_id), storage.read_doc(user_id, "USER")
    )
    archived_facts = archive_facts(storage, user_id, "MEMORY")
    archived_dynamics = archive_facts(storage, user_id, "RELATIONS")
    recent = facts[-limit:]
    return {
        "profile": profile[:DOC_MAX_CHARS],
        "facts": [{"day": day, "text": text} for day, text in recent],
        "dynamics": [{"day": day, "text": text} for day, text in relations[-limit:]],
        "archived": {
            "facts": len(archived_facts),
            "dynamics": len(archived_dynamics),
            "recent_facts": [{"day": day, "text": text} for day, text in archived_facts[-24:]],
        },
        "activity": activity_of(storage, user_id),
        "total_facts": len(facts) + len(archived_facts),
        "truncated": len(facts) > len(recent),
    }


async def build_doc(storage: StorageManager, user_id: str, doc: str) -> dict[str, Any]:
    """单份文档的只读正文。"""
    if doc not in DOCS:
        raise ValueError(f"面板不给人看 {doc}")
    content = await storage.read_doc(user_id, doc)
    return {"doc": doc, "content": content[:DOC_MAX_CHARS], "truncated": len(content) > DOC_MAX_CHARS}
