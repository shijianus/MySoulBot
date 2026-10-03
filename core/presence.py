"""此刻的我：时间感、生理节律、情绪余温与耐心余额。

真人不是无状态的函数。这一层把「现在几点、隔了多久没见、我心里还剩多少不痛快、
我今天还有多少话想说」算成确定性的输入，交给语境层。全部函数都吃显式的 `now`，
所以可测试、可复现，也不会因为连调两次就漂出两种体温。

- **节律**：按当地时区把一天切成若干时段，每段给出生理状态与相处分寸。
  深夜（默认 01:00–05:00）不许精神抖擞地当生产力助手。
- **久别重逢**：间隔达到阈值时，先接住这段时间，别装作刚刚才说过话。
- **情绪惰性**：上一轮的不痛快按半衰期衰减，不会下一轮立刻晴转多云。
- **耐心余额**：一天来回太多轮就会耗尽，允许把话推回去、允许「你先去忙你的」。

状态落在 `storage/data/users/<id>/state.json`：运行时体温，不进版本库。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from config import Settings

logger: Final = logging.getLogger("mysoulbot.presence")

MINUTE: Final[int] = 60
DAY_SECONDS: Final[int] = 86_400


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    body: str  # 生理上是什么样
    conduct: str  # 该用什么分寸


SLOTS: Final[tuple[Slot, ...]] = (
    Slot("midnight_zero", "零点过后", "还没睡。这个点人不困也不精神，就是安静。", "别催对方睡，也别开始讲道理。"),
    Slot("deep_night", "深夜", "困。反应会慢半拍，声音是低的，对热闹的东西没兴趣。", "话少、句子短、肯听。这个时辰不适合当生产力工具，不适合列条目。"),
    Slot("dawn", "清晨", "刚醒不久，脑子还没热起来。", "可以直接说你还迷糊。别一早就递一堆建议。"),
    Slot("morning", "上午", "清醒，但心里装着今天要做的事。", "说事就利落地说，别拉着对方沉进情绪里。"),
    Slot("noon", "午间", "有点钝，饭前后注意力是散的。", "简短，别追细节。"),
    Slot("afternoon", "下午", "撑着一段长长的时间，容易不耐烦。", "讲重点，一次一个。"),
    Slot("evening", "傍晚", "松下来了，一天最难的部分过去了。", "这时候话可以多一点，也可以开句轻的玩笑。"),
    Slot("night", "夜里", "醒着，但防御比白天低。", "可以听点真的事。别把夜里当加班时段。"),
)

_SLOT_HOURS: Final[dict[int, str]] = {
    0: "midnight_zero",
    1: "deep_night", 2: "deep_night", 3: "deep_night", 4: "deep_night",
    5: "dawn", 6: "dawn", 7: "dawn",
    8: "morning", 9: "morning", 10: "morning", 11: "morning",
    12: "noon", 13: "noon",
    14: "afternoon", 15: "afternoon", 16: "afternoon", 17: "afternoon",
    18: "evening", 19: "evening", 20: "evening",
    21: "night", 22: "night", 23: "night",
}

REUNION_MIN_DAYS: Final[float] = 2.0
_LOW_PATIENCE: Final[float] = 0.38

# 粗颗粒的情绪线索：真人话里带刺的时候，是能被看出来的
_NEGATIVE: Final[tuple[str, ...]] = (
    "别", "不许", "烦", "够了", "行了", "算了", "无语", "哼", "滚", "无聊",
    "又在", "讲道理", "少来", "不用你", "闭嘴", "懒得", "烦不烦",
)
_POSITIVE: Final[tuple[str, ...]] = (
    "谢谢", "辛苦", "想你", "你说得对", "我知道", "还好有", "抱", "爱你", "对不起", "我错了",
)
_CLARIFY: Final[re.Pattern[str]] = re.compile(r"[。！!？?…]")


def resolve_now(stamp: dt.datetime | None, timezone: str = "") -> dt.datetime:
    """统一时间基准：显式传入优先，否则按配置时区取当下。"""
    if stamp is not None:
        return stamp if stamp.tzinfo else stamp.astimezone()
    return current_time(timezone)


def current_time(timezone: str = "") -> dt.datetime:
    if not timezone:
        return dt.datetime.now().astimezone()
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(timezone))
    except Exception as exc:  # noqa: BLE001 - 时区数据缺失就退回系统时区，但要说一声
        logger.warning("时区 %s 不可用（%s），按系统时区走", timezone, exc)
        return dt.datetime.now().astimezone()


def slot_for(moment: dt.datetime) -> Slot:
    key = _SLOT_HOURS[moment.hour]
    return next(slot for slot in SLOTS if slot.key == key)


def in_deep_night(moment: dt.datetime, start: int, end: int) -> bool:
    """支持跨零点的区间：23→5 这种也算。"""
    return start <= end and start <= moment.hour < end or (
        start > end and (moment.hour >= start or moment.hour < end)
    )


def assess_mood(user_text: str, reply_text: str) -> tuple[float, str]:
    """从这一来一回里估出情绪效价，-1（不痛快）到 1（松）。

    刻意做得粗：只要抓得住「被呛了」和「软下来了」这两件事就够了。
    """
    mine = (user_text or "").strip()
    theirs = (reply_text or "").strip()
    score = 0.0
    cause = ""
    hits = [word for word in _NEGATIVE if word in mine]
    if hits:
        score -= 0.45 + 0.15 * min(2, len(hits) - 1)
        cause = f"他用「{hits[0]}」顶了回来"
    if len(_CLARIFY.findall(mine)) >= 4 and mine.count("！") >= 2:
        score -= 0.2
    lifts = [word for word in _POSITIVE if word in mine]
    if lifts:
        score += 0.4
        if not cause:
            cause = f"他说了「{lifts[0]}」"
    if len(theirs) > 600:
        score -= 0.1  # 自己刚长篇大论完，多半有点累
    return max(-1.0, min(1.0, score)), cause


@dataclass
class Mood:
    """带半衰期的情绪余温。"""

    valence: float = 0.0
    cause: str = ""
    at: dt.datetime | None = None

    def residual(self, now: dt.datetime, half_life_minutes: int) -> float:
        """还剩几分（0–1）。半衰期之后剩一半，不是归零。"""
        if self.at is None or abs(self.valence) < 0.05:
            return 0.0
        elapsed = (now - self.at).total_seconds() / MINUTE
        if elapsed <= 0:
            return 1.0
        return min(1.0, 0.5 ** (elapsed / max(1, half_life_minutes)))

    def current(self, now: dt.datetime, half_life_minutes: int) -> float:
        return round(self.valence * self.residual(now, half_life_minutes), 3)

    def to_dict(self) -> dict[str, Any]:
        return {"valence": self.valence, "cause": self.cause, "at": self.at.isoformat(timespec="seconds") if self.at else ""}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Mood":
        at = raw.get("at")
        parsed = dt.datetime.fromisoformat(str(at)) if at else None
        return cls(float(raw.get("valence") or 0.0), str(raw.get("cause") or ""), parsed)


@dataclass
class Patience:
    """一天的耐心余额。耗尽了就有权利懒得说。"""

    left: float = 1.0
    turns_today: int = 0
    day: str = ""
    touched_at: dt.datetime | None = None

    def roll(self, now: dt.datetime, refill_per_hour: float) -> "Patience":
        fresh = Patience(self.left, self.turns_today, self.day, self.touched_at)
        if fresh.day != now.date().isoformat():
            fresh.day = now.date().isoformat()
            fresh.turns_today = 0
            fresh.left = 1.0
        if fresh.touched_at is not None:
            gained = (now - fresh.touched_at).total_seconds() / 3600 * refill_per_hour
            fresh.left = max(0.0, min(1.0, fresh.left + gained))
        fresh.touched_at = now
        return fresh

    def spend(self, turn_limit: int) -> "Patience":
        clone = Patience(self.left, self.turns_today + 1, self.day, self.touched_at)
        clone.left = max(0.0, clone.left - 1.0 / max(3, turn_limit))
        return clone

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": round(self.left, 3),
            "turns_today": self.turns_today,
            "day": self.day,
            "touched_at": self.touched_at.isoformat(timespec="seconds") if self.touched_at else "",
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Patience":
        touched = raw.get("touched_at")
        parsed = dt.datetime.fromisoformat(str(touched)) if touched else None
        return cls(
            float(raw.get("left", 1.0)),
            int(raw.get("turns_today") or 0),
            str(raw.get("day") or ""),
            parsed,
        )


@dataclass
class Presence:
    """一轮对话的「体温快照」，语境层只读它。"""

    stamp: dt.datetime
    slot: Slot = field(default_factory=lambda: slot_for(dt.datetime.now()))
    deep_night: bool = False
    mood: Mood = field(default_factory=Mood)
    mood_residual: float = 0.0
    patience: Patience = field(default_factory=Patience)
    gap_days: float | None = None
    minutes_since_reply: float | None = None

    # ------------------------------------------------------------ 渲染
    def lines(self) -> list[str]:
        out = [
            f"此刻（{self.stamp.strftime('%H:%M')}，{self.stamp.utcoffset() or ''}）：{self.slot.label}。"
            f"你的身体：{self.slot.body} 分寸：{self.slot.conduct}",
        ]
        if self.gap_days and self.gap_days >= REUNION_MIN_DAYS:
            out.append(
                f"你们隔了 {self._gap_text()} 没说话。这一句必须先接住这段时间——"
                "像真人那样自然提一句（去哪了、怎么这时候冒出来），"
                "不许原地待命式地重新开始，不许质问，也不许为这段时间道歉个没完。"
            )
        elif self.minutes_since_reply is not None and self.minutes_since_reply >= 240:
            out.append(f"这一阵空了 {int(self.minutes_since_reply // 60)} 小时左右，别把上一句当成刚刚才说。")
        residual = self.mood_residual
        if residual >= 0.15 and abs(self.mood.valence) >= 0.05:
            if self.mood.valence < 0:
                out.append(
                    f"情绪余温：还有 {int(residual * 100)}% 的不痛快没散"
                    + (f"（起因：{self.mood.cause}）" if self.mood.cause else "")
                    + "。不许一开口就晴转多云，也不许装什么都没发生；"
                    "这一轮你可以比平时短、比平时冷一点，直到你真的被接住。"
                )
            else:
                out.append(
                    f"情绪余温：还带着 {int(residual * 100)}% 的松动"
                    + (f"（起因：{self.mood.cause}）" if self.mood.cause else "")
                    + "。热络要跟着这个度数走，不要凭空拔高。"
                )
        left = self.patience.left
        if left <= _LOW_PATIENCE:
            out.append(
                f"耐心余额 {int(left * 100)}%（今天已经来回 {self.patience.turns_today} 轮）。"
                "这个状态下真人会说：「我现在不太想谈这个」「你先去忙你的，别理我」。"
                "你有权利这么开口，但要用你自己的话，并且不许拿它当威胁。"
            )
        if self.deep_night:
            out.append("这是深夜：不要把话题导向效率、任务、计划；不要提议你去做什么。")
        return out

    def _gap_text(self) -> str:
        days = self.gap_days or 0
        if days < 1:
            return "不到一天"
        if days < 2:
            return "一天多"
        if days < 7:
            return f"{int(days)} 天"
        if days < 30:
            return f"{int(days // 7)} 个星期"
        return f"{int(days // 30)} 个多月"

    # ------------------------------------------------------------ 序列化
    def to_state(self) -> dict[str, Any]:
        return {
            "mood": self.mood.to_dict(),
            "patience": self.patience.to_dict(),
            "last_seen": self.stamp.isoformat(timespec="seconds"),
        }


def build_presence(
    settings: Settings,
    state: dict[str, Any],
    now: dt.datetime,
) -> Presence:
    """把落盘的状态 + 此刻，算成这一轮的体温。"""
    half_life = settings.mood_half_life_minutes
    mood = Mood.from_dict(dict(state.get("mood") or {}))
    patience = Patience.from_dict(dict(state.get("patience") or {})).roll(
        now, settings.patience_refill_per_hour
    )
    last_raw = str(state.get("last_seen") or "")
    gap_days: float | None = None
    minutes_since: float | None = None
    if last_raw:
        try:
            last = dt.datetime.fromisoformat(last_raw)
            delta = (now - last).total_seconds()
            minutes_since = max(0.0, delta / MINUTE)
            gap_days = max(0.0, delta / DAY_SECONDS)
        except ValueError:
            logger.debug("state.json 里 last_seen 读不出：%s", last_raw)
    return Presence(
        stamp=now,
        slot=slot_for(now) if settings.rhythm_enabled else SLOTS[6],
        deep_night=settings.rhythm_enabled and in_deep_night(now, *settings.night_hours),
        mood=mood,
        mood_residual=mood.residual(now, half_life),
        patience=patience,
        gap_days=gap_days,
        minutes_since_reply=minutes_since,
    )
