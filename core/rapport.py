"""熟络度演进引擎：温度只由相处攒出来，不接受调温指令。

`RELATIONS.md` 里多一个引擎独占的 `## 温度` 小节：熟络度（0–100）、阶段、峰值、依据。
阶段决定说话的**距离**：

- **陌生期 stranger (0–25)**：克制、礼貌有距离感，不轻易倾诉内心，不盲目热络，
  不许用亲昵称呼、不许一副「我们很熟」的样子。
- **初熟 acquaintance (26–50)**：开始放下防御，允许流露个人偏见、偶尔吐槽，
  不再句句给正式回答；但对方没提的私事仍然不掀。
- **熟络期 friend (51–75)**：话可以跳、可以省，可以打断，可以更直接地不同意；
  记得分寸也敢用分寸。
- **深层默契 confidant (76–100)**：允许极简（「嗯」「随便你」「去睡了」），
  知根知底，听得懂潜台词，不需要每轮都产出内容。

分数是**增量累积**的：每轮一点，深夜多一点，吵过又和好得多一点，他新交代一件事加一点；
隔了很久没联系则掉一截，但不会掉破峰值的一半——真人也不会把多年交情一夜清零。

写入路径只有 `RapportEngine`（由引擎在回合结束时调用）。CLI 不提供 set，
命令行与酒馆都无法把温度调上去。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from config import Settings
from core.storage_manager import RELATIONS_SECTION, StorageManager, insert_into_section

logger: Final = logging.getLogger("mysoulbot.rapport")

TEMPERATURE_SECTION: Final[str] = "## 温度"
_KV: Final[re.Pattern[str]] = re.compile(r"^-\s*(熟络度|阶段|峰值|更新|依据)\s*[:：]\s*(.+?)\s*$")

STAGES: Final[tuple[tuple[str, str, int, int, str], ...]] = (
    ("stranger", "陌生期", 0, 25,
     "你和他还不熟。克制、礼貌、有距离感：不主动交心，不热络，不亲昵称呼，"
     "不假装有共同回忆。他靠过来太快，你可以往后退半步。"),
    ("acquaintance", "初熟", 26, 50,
     "防御开始放下。可以流露你自己的偏见，可以偶尔吐槽，不必每句都给正式回答；"
     "但他没主动提的私事，你仍不去掀。"),
    ("friend", "熟络期", 51, 75,
     "话可以跳、可以省，可以打断他，可以更直接地不同意。你知道他的雷区，"
     "也偶尔会不小心踩到——然后像熟人那样认一句，不写检讨。"),
    ("confidant", "深层默契", 76, 100,
     "知根知底。允许极简：一个「嗯」，一句「随便你」，一句「去睡了」都算说完。"
     "你听得懂他没说的部分，不必每轮都产出内容。"),
)

# 增量模型：每轮的基础值很小，温度必须攒出来，而不是聊几句就爆表
BASE_DELTA: Final[float] = 0.35
NIGHT_DELTA: Final[float] = 0.5
REPAIR_DELTA: Final[float] = 1.6
DISCLOSURE_DELTA: Final[float] = 0.7
DISCLOSURE_CAP: Final[float] = 2.0
DYNAMIC_DELTA: Final[float] = 0.5
DYNAMIC_CAP: Final[float] = 1.5
COLD_AFTER_DAYS: Final[float] = 7.0
COLD_PER_DAY: Final[float] = 0.9
COLD_CAP: Final[float] = 18.0


def stage_for(score: float) -> tuple[str, str, str]:
    """(键, 中文阶段名, 分寸描述)。"""
    value = max(0, min(100, int(round(score))))
    for key, label, low, high, conduct in STAGES:
        if low <= value <= high:
            return key, label, conduct
    return STAGES[-1][0], STAGES[-1][1], STAGES[-1][4]


@dataclass
class Rapport:
    score: float = 0.0
    stage: str = "stranger"
    label: str = "陌生期"
    conduct: str = ""
    peak: float = 0.0
    evidence: tuple[str, ...] = ()
    deltas: dict[str, float] = field(default_factory=dict)

    @property
    def value(self) -> int:
        return max(0, min(100, int(round(self.score))))

    def line(self) -> str:
        return f"你们之间：熟络度 {self.value}/100（{self.label} · {self.stage}）。{self.conduct}"

    def block_lines(self) -> list[str]:
        return [
            f"- 熟络度: {self.value}",
            f"- 阶段: {self.stage}",
            f"- 峰值: {int(round(self.peak))}",
            f"- 更新: {dt.date.today().isoformat()}",
            f"- 依据: {' · '.join(self.evidence) if self.evidence else '刚开始'}",
        ]


class RapportEngine:
    """温度的唯一写入者。"""

    def __init__(self, settings: Settings, storage: StorageManager) -> None:
        self._settings = settings
        self._storage = storage

    # ------------------------------------------------------------ 读
    async def read(self, user_id: str) -> Rapport:
        """从 RELATIONS.md 的温度块读回现状（渲染与展示用，不作为计算依据）。"""
        content = await self._storage.read_doc(user_id, "RELATIONS")
        raw = parse_temperature(content)
        score = float(raw.get("熟络度", 0) or 0)
        key, label, conduct = stage_for(score)
        stored_stage = str(raw.get("阶段") or "").strip()
        if stored_stage and stored_stage != key:
            # 文档里写着别的阶段：以分数为准，但要说一声，别悄悄改人家的记录
            logger.info("%s 温度块的阶段字段(%s)与熟络度(%d)不符，按分数取 %s", user_id, stored_stage, score, key)
        return Rapport(
            score=score,
            stage=key,
            label=label,
            conduct=conduct,
            peak=float(raw.get("峰值", score) or score),
            evidence=tuple(part.strip() for part in str(raw.get("依据") or "").split("·") if part.strip()),
        )

    # ------------------------------------------------------------ 算
    def advance(
        self,
        previous: Rapport,
        counters: dict[str, Any],
        *,
        now: dt.datetime,
        gap_days: float | None,
        late_night: bool = False,
        repair: bool = False,
        disclosure_delta: int = 0,
        dynamic_delta: int = 0,
    ) -> tuple[Rapport, dict[str, Any]]:
        """攒一点温度。返回新状态与更新后的计数字典（写回 state.json）。"""
        counters = dict(counters or {})
        deltas: dict[str, float] = {}
        score = float(counters.get("score", previous.score))
        peak = float(counters.get("peak", previous.peak))

        if not self._settings.rapport_enabled:
            return previous, counters

        step = BASE_DELTA
        deltas["base"] = step
        if late_night:
            step += NIGHT_DELTA
            deltas["late_night"] = NIGHT_DELTA
        if repair:
            step += REPAIR_DELTA
            deltas["repair"] = REPAIR_DELTA
        if disclosure_delta > 0:
            gained = min(DISCLOSURE_CAP, DISCLOSURE_DELTA * disclosure_delta)
            step += gained
            deltas["disclosure"] = gained
        if dynamic_delta > 0:
            gained = min(DYNAMIC_CAP, DYNAMIC_DELTA * dynamic_delta)
            step += gained
            deltas["dynamics"] = gained

        score += step
        cooled = 0.0
        if gap_days and gap_days >= COLD_AFTER_DAYS:
            cooled = min(COLD_CAP, (gap_days - COLD_AFTER_DAYS + 1) * COLD_PER_DAY)
            floor = peak * self._settings.rapport_floor_ratio
            score = max(floor, score - cooled)
            deltas["cold"] = -cooled

        score = max(0.0, min(100.0, score))
        peak = max(peak, score)

        turns = int(counters.get("turns", 0)) + 1
        days = {str(day) for day in counters.get("days", [])}
        days.add(now.date().isoformat())
        late_nights = int(counters.get("late_nights", 0)) + (1 if late_night else 0)
        repairs = int(counters.get("repairs", 0)) + (1 if repair else 0)
        counters.update(
            {
                "score": round(score, 2),
                "peak": round(peak, 2),
                "turns": turns,
                "days": sorted(days)[-400:],
                "late_nights": late_nights,
                "repairs": repairs,
            }
        )

        key, label, conduct = stage_for(score)
        evidence = (
            f"在一起 {len(days)} 天",
            f"{turns} 轮",
            f"深夜 {late_nights} 次",
            f"吵过也和好 {repairs} 次" if repairs else "还没红过脸",
        )
        return (
            Rapport(
                score=score, stage=key, label=label, conduct=conduct, peak=peak,
                evidence=evidence, deltas=deltas,
            ),
            counters,
        )

    # ------------------------------------------------------------ 写
    async def publish(self, user_id: str, rapport: Rapport) -> None:
        """把温度块写进 RELATIONS.md。只替换 `## 温度` 小节，条目正文一律不动。"""
        lines = rapport.block_lines()

        def transform(content: str) -> str:
            # 先抹掉旧的 KV 行，标题、引言、格式说明一律原样留在原位
            stripped = "\n".join(
                line for line in content.splitlines() if not _KV.match(line.strip())
            )
            if TEMPERATURE_SECTION in stripped:
                return insert_into_section(stripped, TEMPERATURE_SECTION, lines)
            block = "\n".join([TEMPERATURE_SECTION, *lines])
            # 温度块紧贴「## 动态」之前：先看见你们有多熟，再看见攒下的分寸
            if RELATIONS_SECTION in stripped:
                return stripped.replace(
                    RELATIONS_SECTION, f"{block}\n\n{RELATIONS_SECTION}", 1
                )
            return f"{stripped.rstrip()}\n\n{block}\n\n{RELATIONS_SECTION}\n"

        await self._storage.mutate_doc(user_id, "RELATIONS", transform)


def parse_temperature(content: str) -> dict[str, str]:
    """取 `## 温度` 小节里的 `- 键: 值` 行。"""
    out: dict[str, str] = {}
    inside = False
    for line in content.splitlines():
        text = line.strip()
        if text == TEMPERATURE_SECTION:
            inside = True
            continue
        if text.startswith("#"):
            inside = False
            continue
        if not inside:
            continue
        match = _KV.match(text)
        if match:
            out[match.group(1)] = match.group(2)
    return out
