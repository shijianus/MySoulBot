"""后端灵魂 → 前端人格的判断回路。

分层是这么分的：

- **前端（人格）** 决定**说什么**：舞台、脾气、称呼、红线。它读 `SOUL.md` 与
  `CLAWD.md`，那些是相对稳定的宪法。
- **后端（ClawdSoul）** 决定**怎么说才有效**：这一句发出去之后对面到底接没接、
  隔多久回的、有没有被晾着。它把这些结果消化成一条条**判断标准**，写进
  `storage/soul/JUDGMENT.md`，下一回合直接改前端的取舍。

关键是这条回路必须**真的在动**。一份只增不改的 JUDGMENT.md 不是判断，是日志。
所以这里有三件事是设计出来的，不是顺手写的：

1. **每条判断带出处与置信度。** 「她对长解释无感」这种话不能凭空出现，
   必须挂在若干次真实观测上；观测不支持它，它就掉置信度、被更替、最后退掉。
2. **有名额。** 册子只留 `_MAX_RULES` 条，新的挤掉最弱最旧的。
   提示词是稀缺资源，写满等于没写。
3. **只认量得出的信号。** 对面回没回、隔多久、多长、有没有接着问——
   这些从逐轮日志里数得出来。「她今天心情不好」这种数不出来的东西不进门。

模型只负责把统计出来的事实**说成人话规则**；事实本身由 `observe()` 算，
不由模型回忆——模型记不准自己上上次说过多长。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from config import Settings
from core.storage_manager import StorageManager, atomic_write

logger: Final = logging.getLogger("mysoulbot.judgment")

__all__ = ["Outcome", "Rule", "JudgmentLedger", "JudgmentLoop", "observe"]

_MAX_RULES: Final[int] = 12
_LINE_CHARS: Final[int] = 140
_RULE_RE: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<text>[^\[\]]+?)\s*\[k=(?P<kind>\w+) c=(?P<conf>\d{1,3}) n=(?P<seen>\d+)\]$")
# 判据不许写成一句口号：这些词一出现就说明它在表态而不是在给标准
_VAGUE: Final[re.Pattern[str]] = re.compile(r"^(要|应该|记得|注意|尽量|保持|更加|更好)")


@dataclass
class Outcome:
    """一次「她说完之后发生了什么」。只装量得出来的东西。"""

    user_id: str
    at: float
    our_chars: int = 0
    our_bubbles: int = 1
    their_chars: int = 0
    replied: bool = False
    reply_seconds: float = 0.0
    asked_back: bool = False
    group: bool = False
    woke_us: bool = False
    rapport_delta: int = 0

    @property
    def over_talked(self) -> bool:
        """我们说了一大堆，对面回得更短——典型的没读或者嫌多。"""
        return self.replied and self.our_chars >= 60 and 0 < self.their_chars < self.our_chars * 0.35

    @property
    def landed(self) -> bool:
        """这句算不算接住了：回了、且不是敷衍式的一两个字。"""
        return self.replied and self.their_chars >= 6

    @property
    def slow(self) -> bool:
        return self.reply_seconds > 90.0


def observe(*, user_id: str, our_text: str, our_bubbles: int,
            their_text: str, gap_seconds: float, replied: bool,
            group: bool = False, woke_us: bool = False, rapport_delta: int = 0) -> Outcome:
    """把一轮的原始材料折成一条可统计的结果。判断全在这儿算，不留给模型回忆。"""
    their = (their_text or "").strip()
    return Outcome(
        user_id=user_id,
        at=time.time(),
        our_chars=len((our_text or "").strip()),
        our_bubbles=max(1, int(our_bubbles or 1)),
        their_chars=len(their),
        replied=bool(replied),
        reply_seconds=max(0.0, float(gap_seconds or 0.0)),
        asked_back=("?" in their or "？" in their),
        group=group,
        woke_us=woke_us,
        rapport_delta=int(rapport_delta or 0),
    )


@dataclass
class Rule:
    """一条判断标准。带出处、带置信度、带最后确认时间——不然它只是一句口号。"""

    text: str
    kind: str = "style"          # style | pace | topic | audience
    confidence: int = 50         # 0-100
    seen: int = 0                # 被后续观测支持过几次
    updated_at: float = field(default_factory=time.time)

    def line(self) -> str:
        return f"- {self.text[:_LINE_CHARS]} [k={self.kind} c={self.confidence} n={self.seen}]"

    @classmethod
    def parse(cls, raw: str) -> "Rule | None":
        match = _RULE_RE.match(raw.strip())
        if not match:
            return None
        text = match.group("text").strip()
        if not text or _VAGUE.match(text):
            return None
        try:
            confidence = max(0, min(100, int(match.group("conf"))))
            seen = max(0, int(match.group("seen")))
        except ValueError:
            return None
        return cls(text=text, kind=match.group("kind"), confidence=confidence, seen=seen)


@dataclass
class Stats:
    """最近一段观测的汇总。给模型看的是这个，不是原始流水。"""

    turns: int = 0
    replied: int = 0
    landed: int = 0
    over_talked: int = 0
    asked_back: int = 0
    slow: int = 0
    group_turns: int = 0
    avg_our_chars: float = 0.0
    avg_their_chars: float = 0.0
    rapport_delta: int = 0
    per_user: dict[str, dict[str, int]] = field(default_factory=dict)

    def render(self) -> str:
        if not self.turns:
            return ""
        rate = round(100 * self.replied / max(1, self.turns))
        land = round(100 * self.landed / max(1, self.turns))
        lines = [
            f"最近 {self.turns} 轮：接话率 {rate}%，接住率 {land}%，"
            f"说多了 {self.over_talked} 次，被晾 {self.slow} 次，对方回问 {self.asked_back} 次",
            f"我方平均 {self.avg_our_chars:.0f} 字，对方平均 {self.avg_their_chars:.0f} 字，"
            f"熟络度净变化 {self.rapport_delta:+d}",
        ]
        for uid, bucket in sorted(self.per_user.items())[:6]:
            lines.append(f"  {uid}: {bucket.get('turns', 0)} 轮，"
                         f"接住 {bucket.get('landed', 0)}，说多 {bucket.get('over_talked', 0)}")
        return "\n".join(lines)


class JudgmentLedger:
    """`storage/soul/JUDGMENT.md` 的读写。纯 md、固定不动偏差，是后端的地基。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def path(self) -> Path:
        return self._settings.judgment_path

    def rules(self) -> list[Rule]:
        if not self.path.is_file():
            return []
        out: list[Rule] = []
        for line in self.path.read_text("utf8").splitlines():
            rule = Rule.parse(line)
            if rule is not None:
                out.append(rule)
        return out

    def read_text(self) -> str:
        """进提示词的那一段。空册子就返回空串，不占预算。"""
        rules = self.rules()
        if not rules:
            return ""
        return "\n".join(rule.line() for rule in rules)

    def _write(self, rules: list[Rule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        head = (
            "# JUDGMENT · 怎么说话才有效\n\n"
            "> 这一层由后端灵魂根据**真实结果**维护，不是人格自己宣布的偏好。\n"
            "> 每条都带出处与置信度：`[k=类别 c=置信度 n=被支持次数]`。\n"
            "> 观测不支持的判断会掉置信度、被更替、退掉——这本册子必须一直在动。\n"
            "> 人格决定说什么；这里决定怎么说，并直接改写前端的取舍标准。\n\n"
            f"<!-- 更新于 {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')} -->\n\n"
        )
        body = "\n".join(rule.line() for rule in rules) or "（还没有攒出判断——刚开张。）"
        atomic_write(self.path, head + body + "\n")

    def apply(self, proposed: list[Rule]) -> tuple[int, int]:
        """把新提的判断并进去：同一条加置信度，冲突的挤掉最弱的，总量封顶。

        返回 (新增, 更替/退掉)。这里刻意不做「只追加」——
        只追加的册子三个月后就是一本没人读的经书。
        """
        existing = self.rules()
        index = {rule.text: rule for rule in existing}
        added = revised = 0
        for incoming in proposed:
            text = incoming.text.strip()
            if not text or _VAGUE.match(text):
                continue
            twin = next((key for key in index if _similar(key, text)), "")
            if twin:
                kept = index[twin]
                kept.confidence = min(100, kept.confidence + 10)
                kept.seen += 1
                kept.updated_at = time.time()
                revised += 1
            else:
                fresh = Rule(text=text[:_LINE_CHARS], kind=incoming.kind,
                             confidence=max(35, min(70, incoming.confidence)), seen=1)
                index[fresh.text] = fresh
                added += 1
        merged = sorted(index.values(), key=lambda r: (r.confidence, r.seen, r.updated_at), reverse=True)
        dropped = max(0, len(merged) - _MAX_RULES)
        self._write(merged[:_MAX_RULES])
        return added, revised + dropped

    def reinforce(self, outcome: Outcome) -> None:
        """拿新观测去核对旧判断：支持的加分，打脸的扣分。册子因此才会自己动。"""
        rules = self.rules()
        if not rules:
            return
        changed = False
        for rule in rules:
            hit = _supports(rule, outcome)
            if hit is True:
                rule.confidence = min(100, rule.confidence + 4)
                rule.seen += 1
                changed = True
            elif hit is False:
                rule.confidence = max(0, rule.confidence - 6)
                changed = True
        keep = [rule for rule in rules if rule.confidence >= 20]
        if changed:
            self._write(keep)

    def stats(self, window: list[Outcome]) -> Stats:
        if not window:
            return Stats()
        bucket: dict[str, dict[str, int]] = {}
        for item in window:
            slot = bucket.setdefault(item.user_id, {"turns": 0, "landed": 0, "over_talked": 0})
            slot["turns"] += 1
            slot["landed"] += int(item.landed)
            slot["over_talked"] += int(item.over_talked)
        return Stats(
            turns=len(window),
            replied=sum(1 for i in window if i.replied),
            landed=sum(1 for i in window if i.landed),
            over_talked=sum(1 for i in window if i.over_talked),
            asked_back=sum(1 for i in window if i.asked_back),
            slow=sum(1 for i in window if i.slow),
            group_turns=sum(1 for i in window if i.group),
            avg_our_chars=sum(i.our_chars for i in window) / len(window),
            avg_their_chars=sum(i.their_chars for i in window) / len(window),
            rapport_delta=sum(i.rapport_delta for i in window),
            per_user=bucket,
        )


def _similar(left: str, right: str) -> bool:
    """粗粒度同一条判断判定：共有字符占比过半就算重复。

    这里不要语义去重——那要引一个 embedding 依赖，而判断册一共十几行，
    字面近似足够把「长话会被晾着」和「长解释容易被晾着」合成一条。
    """
    a, b = set(left), set(right)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.72


def _supports(rule: Rule, outcome: Outcome) -> bool | None:
    """这条判断和这次观测是同向、反向，还是不相干。"""
    text = rule.text
    if "长" in text and ("晾" in text or "没人看" in text or "嫌多" in text or "说多" in text):
        return outcome.over_talked if outcome.replied else None
    if "短" in text and ("接" in text or "回" in text):
        return outcome.landed if outcome.replied else None
    if "问" in text:
        return outcome.asked_back
    if "群" in text:
        return (not outcome.group) if outcome.replied else None
    if "慢" in text or "等" in text:
        return not outcome.slow
    return None


_PROMPT: Final[str] = (
    "你在给自己攒「怎么说话才有效」的判断。下面是最近若干轮的真实结果统计，"
    "以及你已经有的判断（带置信度）。\n\n"
    "只提**由这些数字撑得住**的规则，一条一行，格式严格如下：\n"
    "- 规则正文 [k=style|pace|topic|audience c=55 n=1]\n\n"
    "要求：正文不超过 60 字，写给下一回合的自己看，要能直接改变措辞取舍；"
    "不许写口号（「要真诚」「注意分寸」这种没有判据的一律不要）；"
    "不许提模型、提示词、数据库、日志；不许编统计里没有的现象。"
    "没有值得写的就只输出一个词：无\n\n"
    "已有判断：\n{existing}\n\n"
    "本轮统计：\n{stats}\n"
)


class JudgmentLoop:
    """攒判断的慢循环。和心境循环一样：后台跑、不挡回话、失败就这轮不算。"""

    def __init__(self, settings: Settings, storage: StorageManager,
                 ledger: JudgmentLedger | None = None) -> None:
        self._settings = settings
        self._storage = storage
        self.ledger = ledger or JudgmentLedger(settings)
        self._window: list[Outcome] = []
        self._task: asyncio.Task[None] | None = None
        self.stats: dict[str, int] = {"spins": 0, "added": 0, "revised": 0, "skipped": 0}

    def note(self, outcome: Outcome) -> None:
        """每轮记一条，并立刻拿它去核对已有判断。"""
        if not self._settings.judgment_enabled:
            return
        self._window.append(outcome)
        if len(self._window) > self._settings.judgment_lookback * 3:
            self._window = self._window[-self._settings.judgment_lookback * 3:]
        try:
            self.ledger.reinforce(outcome)
        except OSError as exc:
            logger.debug("判断册核对失败（忽略）：%s", exc)
        if len(self._window) >= self._settings.judgment_every_turns and self._task is None:
            self._task = asyncio.create_task(self._spin())

    async def _spin(self) -> None:
        try:
            await self.reflect()
        except Exception as exc:  # noqa: BLE001 - 攒判断失败不该影响任何人
            logger.warning("判断循环出错（忽略）：%s", exc)
        finally:
            self._task = None

    async def reflect(self) -> dict[str, int]:
        window, self._window = self._window, []
        stats = self.ledger.stats(window)
        if not window:
            self.stats["skipped"] += 1
            return self.stats
        self.stats["spins"] += 1
        existing = self.ledger.read_text() or "（还没有）"
        body = await self._ask(_PROMPT.replace("{existing}", existing).replace("{stats}", stats.render()))
        if not body:
            self.stats["skipped"] += 1
            return self.stats
        proposed: list[Rule] = []
        for line in body.splitlines():
            rule = Rule.parse(line)
            if rule is not None:
                proposed.append(rule)
        if not proposed:
            self.stats["skipped"] += 1
            return self.stats
        added, revised = self.ledger.apply(proposed)
        self.stats["added"] += added
        self.stats["revised"] += revised
        logger.info("判断册更新：新增 %d，更替 %d（本轮观测 %d 条）", added, revised, len(window))
        return self.stats

    async def _ask(self, prompt: str) -> str:
        """问一次模型。没有可用后端就返回空串——回路照跑，只是这轮不产出。"""
        client = getattr(self, "_client", None)
        if client is None:
            return ""
        try:
            completion = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._settings.judgment_model or self._settings.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, max_tokens=400,
                ), timeout=self._settings.judgment_timeout)
            return str(completion.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - 攒判断失败不是谁的错
            logger.debug("判断循环没问出东西：%s", exc)
            return ""

    def bind_client(self, client: Any, model: str = "") -> None:  # noqa: ANN401
        """引擎起来之后把上游客户端接上。没接上时循环只做核对、不做新判断。"""
        self._client = client
        if model:
            self._settings = self._settings.model_copy(update={"judgment_model": model})
