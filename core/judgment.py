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
4. **「没回」必须被主动量出来。** 只在对面开口那一刻记账的话，`replied` 永远是
   真的、接话率永远 100%——那本册子会拿假统计攒出「没人理我也要把话说透」这种
   被伪数据撑腰的规则。所以每一句说出去的话先挂在 `_pending` 里等结果：
   等来了话就记「接住」，等超时了就记「被晾着」（见 `track_reply` / `sweep_silence`）。

模型只负责把统计出来的事实**说成人话规则**；事实本身由 `observe()` 算，
不由模型回忆——模型记不准自己上上次说过多长。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator

from config import Settings
from core.storage_manager import StorageManager, atomic_write

logger: Final = logging.getLogger("mysoulbot.judgment")

__all__ = ["Outcome", "Rule", "OpenReply", "JudgmentLedger", "JudgmentLoop", "observe"]

_MAX_RULES: Final[int] = 12
_LINE_CHARS: Final[int] = 140
# `_PROMPT` 让模型把正文压在 60 字内，但那只是请求、不是强制。
# 收进来这一步得自己封顶：一句 140 字的「判断」进提示词，挤掉的是一段真话。
_RULE_CHARS: Final[int] = 96
# 掉到这条线以下的判断在下一趟 `apply` 里退场。原来 `reinforce` 自己会删条：
# 一次打脸就足以把一条 20 分上下的判断静默抹掉，册子于是变成「谁最巧谁活」。
_RETIRE_CONFIDENCE: Final[int] = 15
_RULE_RE: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<text>[^\[\]]+?)\s*\[k=(?P<kind>\w+) c=(?P<conf>\d{1,3}) n=(?P<seen>\d+)"
    r"(?: w=(?P<who>[A-Za-z0-9_.\-]{0,64}))?\]$")
# 判据不许写成一句口号：这些词一出现就说明它在表态而不是在给标准
_VAGUE: Final[re.Pattern[str]] = re.compile(r"^(要|应该|记得|注意|尽量|保持|更加|更好)")
# 「答非所问/闹了误会」是能数出来的：对面会**纠正**你。这些纠正话术就是信号。
# 只认第二人称的指正，不认疑问句里的「是不是」——那是在问，不是在驳。
_OFF_TARGET: Final[re.Pattern[str]] = re.compile(
    r"不是(?:这个|我问的|这意思|说要)|我(?:是说|问的是|的意思是)|你没(?:听懂|听明白|懂我)|"
    r"听错了|理解错了|答非所问|文不对题|跟那个没关系|我不是这个意思|跑题了")


@dataclass
class Outcome:
    """一次「她说完之后发生了什么」。只装量得出来的东西。

    两个最该抓住的失败模式（这是「自我成长」的正经定义，不是玄学）：
    - `reused`：同一套句式被拿去答不同的问题——人一眼就看出来是套话；
    - `off_target`：答非所问、会错意，对面当场纠正。
    """

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
    reused: bool = False
    off_target: bool = False

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
            group: bool = False, woke_us: bool = False, rapport_delta: int = 0,
            reused: bool = False, off_target: bool = False) -> Outcome:
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
        reused=bool(reused),
        off_target=bool(off_target or bool(_OFF_TARGET.search(their))),
    )


@dataclass
class OpenReply:
    """她说完了、还没等到结果的那一句。挂着，事后结掉。

    为什么要单独一个结构而不是当场记账：这一层的信号有一半是「没发生的事」，
    而没发生的事不会触发任何调用——不主动去收，「被晾着」这一格就永远是空的。
    """

    user_id: str
    started_at: float
    our_chars: int
    our_bubbles: int = 1
    group: bool = False
    woke_us: bool = False
    rapport_delta: int = 0
    reused: bool = False


def _echo(text: str, recent: Sequence[str], *, threshold: float = 0.78) -> bool:
    """这句是不是把最近说过的某句又端了一遍。

    字符集合重叠的粗判够用了：要抓的不是「措辞相似」，是**同一套句式被拿去答不同的问题**——
    那种句子轮廓一模一样的复用，字面重叠本来就高。阈值压到 0.78 以下就会把
    「本鲸」这种自称也算成重复，那是误伤。
    """
    body = re.sub(r"\s+", "", text or "")
    if len(body) < 8:
        return False
    shape = set(body)
    for prev in recent:
        other = re.sub(r"\s+", "", prev or "")
        if len(other) < 8:
            continue
        if len(shape & set(other)) / max(1, min(len(shape), len(set(other)))) >= threshold:
            return True
    return False


def _close(item: OpenReply, *, their_text: str, at: float, replied: bool) -> Outcome:
    """把挂着的那一句结掉：接住了、还是没接。"""
    their = (their_text or "").strip()
    return Outcome(
        user_id=item.user_id, at=at,
        our_chars=item.our_chars, our_bubbles=item.our_bubbles,
        their_chars=len(their), replied=replied,
        reply_seconds=max(0.0, at - item.started_at),
        asked_back=bool(replied and ("?" in their or "？" in their)),
        group=item.group, woke_us=item.woke_us, rapport_delta=item.rapport_delta,
        reused=item.reused, off_target=bool(replied and _OFF_TARGET.search(their)),
    )


@dataclass
class Rule:
    """一条判断标准。带出处、带置信度、带最后确认时间——不然它只是一句口号。

    `who` 是这条判断**对谁成立**：空是对所有人，非空就是「对 qq_group_950689514
    这个人，这样说不灵」。人群之间分寸不一样，把「对凯子要短句」和「对龙腾可以贫」
    混成一条全局规则，等于两条都写错。
    """

    text: str
    kind: str = "style"          # style | pace | topic | audience
    confidence: int = 50         # 0-100
    seen: int = 0                # 被后续观测支持过几次
    who: str = ""                # 生效范围：空=所有人，否则某个 engine_user_id
    updated_at: float = field(default_factory=time.time)

    def line(self) -> str:
        tail = f" w={self.who}]" if self.who else "]"
        return f"- {self.text[:_LINE_CHARS]} [k={self.kind} c={self.confidence} n={self.seen}{tail}"

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
        return cls(text=text, kind=match.group("kind"), confidence=confidence, seen=seen,
                   who=match.group("who") or "")


@dataclass
class Stats:
    """最近一段观测的汇总。给模型看的是这个，不是原始流水。"""

    turns: int = 0
    replied: int = 0
    landed: int = 0
    over_talked: int = 0
    asked_back: int = 0
    slow: int = 0
    reused: int = 0
    off_target: int = 0
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
            f"重复句式 {self.reused} 次，答非所问被纠正 {self.off_target} 次",
            f"我方平均 {self.avg_our_chars:.0f} 字，对方平均 {self.avg_their_chars:.0f} 字",
        ]
        for uid, bucket in sorted(self.per_user.items())[:6]:
            lines.append(f"  {uid}: {bucket.get('turns', 0)} 轮，"
                         f"接住 {bucket.get('landed', 0)}，说多 {bucket.get('over_talked', 0)}，"
                         f"重复 {bucket.get('reused', 0)}，跑题 {bucket.get('off_target', 0)}")
        return "\n".join(lines)


class JudgmentLedger:
    """`storage/soul/JUDGMENT.md` 的读写。纯 md、固定不动偏差，是后端的地皮。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def path(self) -> Path:
        return self._settings.judgment_path

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """跨进程互斥。控制台那头和守护进程那头共用这一本册子。

        为什么在这里自己拿 flock 而不走 `StorageManager._critical`：那把锁是 async 的，
        而 `read_text()` 在提示词装配的同步段里被调用（`prompt_builder.py:599`）——
        为了塞进一把锁把整条读取链改成 async，代价比这笔买卖大。
        临界区只有几 KB 的读写，和 `atomic_write` 自己在事件循环里 fsync 是同一量级。
        """
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        handle = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "w", encoding="utf8")  # noqa: SIM115 - 交给 finally
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle = None                      # 拿不到锁不拦她说话：册子照常读写
        try:
            yield
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()

    def rules(self) -> list[Rule]:
        if not self.path.is_file():
            return []
        out: list[Rule] = []
        for line in self.path.read_text("utf8").splitlines():
            rule = Rule.parse(line)
            if rule is not None:
                out.append(rule)
        return out

    def read_text(self, user_id: str = "") -> str:
        """进提示词的那一段。空册子就返回空串，不占预算。

        带 `who` 的规则只对那一个人说：把「对甲要短」端给乙看，
        她会拿乙试出来的打法去打所有人——那正好是逐人微调的反面。
        """
        rules = [rule for rule in self.rules() if not rule.who or rule.who == user_id]
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

    def seed(self) -> bool:
        """册子还不存在时先立一个空壳。返回是否真的新建了。

        空文件比缺文件诚实：这一层要能在 `git status` 里看见，
        才谈得上「她真的在改自己」，而不是某句写在人格里的自我宣称。
        """
        if self.path.is_file():
            return False
        self._write([])
        return True

    def apply(self, proposed: list[Rule]) -> tuple[int, int]:
        """把新提的判断并进去：同一条加置信度，冲突的挤掉最弱的，总量封顶。

        返回 (新增, 更替/退掉)。这里刻意不做「只追加」——
        只追加的册子三个月后就是一本没人读的经书。
        退场也只在这一步发生：`reinforce` 只改分不删条，删条得连着统计一起走，
        免得一次打脸就把一条刚攒起来的判断静默抹掉。
        """
        with self._locked():
            existing = self.rules()
            index = {(rule.who, rule.text): rule for rule in existing}
            added = revised = 0
            for incoming in proposed:
                text = incoming.text.strip()
                if not text or _VAGUE.match(text) or len(text) > _RULE_CHARS:
                    continue
                who = (incoming.who or "").strip()
                # 同一条措辞对不同人是两条规则，不许被「字面相近」合成一条
                twin = next((key for key in index
                             if key[0] == who and _similar(key[1], text)), "")
                if twin:
                    kept = index[twin]
                    kept.confidence = min(100, kept.confidence + 10)
                    kept.seen += 1
                    kept.updated_at = time.time()
                    revised += 1
                else:
                    fresh = Rule(text=text[:_LINE_CHARS], kind=incoming.kind,
                                 confidence=max(35, min(70, incoming.confidence)), seen=1,
                                 who=who)
                    index[(who, fresh.text)] = fresh
                    added += 1
            alive = [rule for rule in index.values() if rule.confidence >= _RETIRE_CONFIDENCE]
            merged = sorted(alive, key=lambda r: (r.confidence, r.seen, r.updated_at), reverse=True)
            dropped = len(index) - len(merged)
            dropped += max(0, len(merged) - _MAX_RULES)
            self._write(merged[:_MAX_RULES])
        return added, revised + dropped

    def reinforce(self, outcome: Outcome) -> None:
        """拿新观测去核对旧判断：支持的加分，打脸的扣分。册子因此才会自己动。"""
        with self._locked():
            rules = self.rules()
            if not rules:
                return
            changed = False
            for rule in rules:
                # 这条判断不是对这个人说的，这一轮的成败就与它无关
                if rule.who and rule.who != outcome.user_id:
                    continue
                hit = _supports(rule, outcome)
                if hit is True:
                    rule.confidence = min(100, rule.confidence + 4)
                    rule.seen += 1
                    changed = True
                elif hit is False:
                    rule.confidence = max(0, rule.confidence - 6)
                    changed = True
            if changed:
                # 全量写回，不在这一步删条：退场由 apply 的置信度下限统一裁
                self._write(rules)

    def stats(self, window: list[Outcome]) -> Stats:
        if not window:
            return Stats()
        bucket: dict[str, dict[str, int]] = {}
        for item in window:
            slot = bucket.setdefault(
                item.user_id,
                {"turns": 0, "landed": 0, "over_talked": 0, "reused": 0, "off_target": 0})
            slot["turns"] += 1
            slot["landed"] += int(item.landed)
            slot["over_talked"] += int(item.over_talked)
            slot["reused"] += int(item.reused)
            slot["off_target"] += int(item.off_target)
        return Stats(
            turns=len(window),
            replied=sum(1 for i in window if i.replied),
            landed=sum(1 for i in window if i.landed),
            over_talked=sum(1 for i in window if i.over_talked),
            asked_back=sum(1 for i in window if i.asked_back),
            slow=sum(1 for i in window if i.slow),
            reused=sum(1 for i in window if i.reused),
            off_target=sum(1 for i in window if i.off_target),
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
    """这条判断和这次观测是同向、反向，还是不相干。

    顺序按「特殊到一般」排：一句带「长」又带「问」的判断，先按长短算——
    原来 `"问"` 这一支排在群/慢之前，等于把一条讲篇幅的判断拿去对上回没回问，
    同一条规则会被两个不相干的信号来回打分。
    """
    text = rule.text
    # 「重复句式」「答非所问」是两个独立的失败模式，判据先走它们：
    # 这两类判断的价值就是「别再犯」，所以本轮没犯才算支持
    if ("重复" in text or "套话" in text or "同一句" in text or "同一套" in text
            or "同一个说法" in text or "端一遍" in text or "复读" in text):
        return not outcome.reused
    if "答非所问" in text or "会错意" in text or "跑题" in text or "听错" in text:
        return not outcome.off_target
    if "长" in text and ("晾" in text or "没人看" in text or "嫌多" in text or "说多" in text):
        return outcome.over_talked if outcome.replied else None
    if "短" in text and ("接" in text or "回" in text):
        return outcome.landed if outcome.replied else None
    if "群" in text:
        return (not outcome.group) if outcome.replied else None
    if "慢" in text or "等" in text:
        return not outcome.slow
    if "问" in text:
        return outcome.asked_back
    return None


_PROMPT: Final[str] = (
    "你在给自己攒「怎么说话才有效」的判断。下面是最近若干轮的真实结果统计，"
    "以及你已经有的判断（带置信度）。\n\n"
    "只提**由这些数字撑得住**的规则，一条一行，格式严格如下：\n"
    "- 规则正文 [k=style|pace|topic|audience c=55 n=1]\n"
    "- 只对某个人成立的判断，末尾带上他是谁：[k=audience c=55 n=1 w=那个id]\n\n"
    "要求：正文不超过 60 字，写给下一回合的自己看，要能直接改变措辞取舍；"
    "先攻两个最要命的失败模式：**同一套句式端去答不同的问题**（「重复句式」那一栏），"
    "以及**答非所问、会错意被对方纠正**（「跑题」那一栏）——这两栏不为零时，别的都不算改进。"
    "规则要写「怎么改」不是「别怎样」：「开场别再问今天过得怎么样，直接接他上一句里那件具体的事」"
    "这种才算可执行。"
    "人群之间分寸不一样——「对甲要一次说完一件事」不该当成对所有人都对的规则，"
    "能从统计里看出他只对某类打法有反应，就写成带 w= 的那一条。"
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
        # 不动的流水（最多 120 条）：给试验期对比、给控制台「她最近怎么样」看
        self._trail: deque[Outcome] = deque(maxlen=120)
        # 等着核对旧判断的观测：回复路上只入队，核对在后台做（见 note()）
        self._queued: deque[Outcome] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        # 她自己最近说出口的话（每人 6 条）：判「重复句式」的唯一依据，不交给模型回忆
        self._spoken: dict[str, deque[str]] = {}
        # 说出去还没等到结果的那些话：user_id → 那一句
        self._pending: dict[str, OpenReply] = {}
        self._task: asyncio.Task[None] | None = None
        self.stats: dict[str, int] = {"spins": 0, "added": 0, "revised": 0, "skipped": 0,
                                      "silenced": 0}

    # ------------------------------------------------------------ 记账
    def track_reply(self, *, user_id: str, our_text: str, our_bubbles: int,
                    group: bool = False, woke_us: bool = False,
                    rapport_delta: int = 0, now: float | None = None) -> None:
        """这一句说完了，挂起来等结果。结果可能是对面的话，也可能是一片安静。"""
        if not self._settings.judgment_enabled or not user_id:
            return
        moment = time.time() if now is None else float(now)
        self.sweep_silence(now=moment)
        body = (our_text or "").strip()
        recent = list(self._spoken.get(user_id, ()))
        reused = _echo(body, recent)
        # 记 herself 最近说过的话（只 6 条）：抓「同一套句式答不同问题」要有可比的对象，
        # 而模型记不准自己上上次说过什么——这一格不能交给它回忆
        bucket = self._spoken.setdefault(user_id, deque(maxlen=6))
        if body:
            bucket.append(body)
        self._pending[user_id] = OpenReply(
            user_id=user_id, started_at=moment,
            our_chars=len(body),
            our_bubbles=max(1, int(our_bubbles or 1)),
            group=group, woke_us=woke_us, rapport_delta=int(rapport_delta or 0),
            reused=reused,
        )

    def note_arrived(self, *, user_id: str, their_text: str,
                     now: float | None = None) -> Outcome | None:
        """对面来话了：把挂着的那一句结掉。没挂着东西（第一回合、或她没接）就返回 None。"""
        open_item = self._pending.pop(user_id, None)
        self.sweep_silence(now=now)
        if open_item is None:
            return None
        moment = time.time() if now is None else float(now)
        outcome = _close(open_item, their_text=their_text, at=moment, replied=True)
        self.note(outcome)
        return outcome

    def sweep_silence(self, *, now: float | None = None) -> int:
        """太久没人接的那些，结掉记为「没接」。被晾着这件事只有在这儿量得出来。"""
        if not self._pending:
            return 0
        moment = time.time() if now is None else float(now)
        horizon = float(self._settings.judgment_silence_seconds)
        stale = [uid for uid, item in self._pending.items() if moment - item.started_at >= horizon]
        for uid in stale:
            item = self._pending.pop(uid)
            self.note(_close(item, their_text="", at=moment, replied=False))
            self.stats["silenced"] += 1
        return len(stale)

    def pending_count(self) -> int:
        return len(self._pending)

    def note(self, outcome: Outcome) -> None:
        """每轮记一条。核对旧判断这件事**不在这条路上做**。

        `reinforce` 是一次读盘 + 一次带 flock 的写盘。原来它跟在回复的 `finally` 里同步跑：
        成长不该占用对方等回话的那段时间。所以只入队，真正的核对由后台一趟一趟排掉——
        要影响的永远是**下一次**的措辞，不是这一次的延迟。
        """
        if not self._settings.judgment_enabled:
            return
        self._window.append(outcome)
        # 另一条不动的流水：`_window` 会被 `reflect` 消费掉，而人格自改的试验期
        # 要拿「改之前 vs 改之后」的同一条序列比，不能是被抽干过的那一份
        self._trail.append(outcome)
        self._queued.append(outcome)
        cap = max(self._settings.judgment_lookback, self._settings.judgment_every_turns)
        if len(self._window) > cap:
            self._window = self._window[-cap:]
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())
        if len(self._window) >= self._settings.judgment_every_turns and self._task is None:
            self._task = asyncio.create_task(self._spin())

    async def _drain(self) -> None:
        """把攒下的观测一条条拿去核对已有判断。FIFO，一趟跑完就收工。"""
        while self._queued:
            outcome = self._queued[0]
            try:
                self.ledger.reinforce(outcome)
            except OSError as exc:
                logger.debug("判断册核对失败（忽略）：%s", exc)
            else:
                self._queued.popleft()
            await asyncio.sleep(0)          # 每核一条让出一次：后台的事不挤回话

    async def flush(self, timeout: float = 10.0) -> int:
        """把没核完的账跑完。退出前、以及测试与面板要看准数时用。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while (self._queued or self._drain_task is not None) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            if self._drain_task is not None and self._drain_task.done():
                self._drain_task = None
                if self._queued:
                    self._drain_task = asyncio.create_task(self._drain())
        return len(self._queued)

    def pending_notes(self) -> int:
        return len(self._queued)

    def trail(self, user_id: str = "", *, limit: int = 24) -> list[Outcome]:
        """最近这些观测。给试验期前后对比用，不被任何消费方清空。"""
        items = [item for item in self._trail if not user_id or item.user_id == user_id]
        return items[-max(1, int(limit)):]

    async def _spin(self) -> None:
        try:
            await self.reflect()
        except Exception as exc:  # noqa: BLE001 - 攒判断失败不该影响任何人
            logger.warning("判断循环出错（忽略）：%s", exc)
        finally:
            self._task = None

    async def reflect(self) -> dict[str, int]:
        take = max(1, self._settings.judgment_lookback)
        window = self._window[-take:]
        # 这一趟把看过的消费掉，下一趟只算新观测：同一条结果不该被统计两次
        del self._window[:len(self._window) - len(window)]
        stats = self.ledger.stats(window)
        if not window:
            self.stats["skipped"] += 1
            return self.stats
        self.stats["spins"] += 1
        # 册子第一次跑就落盘，哪怕一条都没有：这一层要能在 git 里看见，
        # 才谈得上「她真的在改自己」。空文件比缺文件诚实。
        self.ledger.seed()
        # 给模型看的是全量（含带 w= 的逐人条），不然它会照着全局条再提一遍同人同话
        existing = "\n".join(rule.line() for rule in self.ledger.rules()) or "（还没有）"
        body = await self._ask(_PROMPT.replace("{existing}", existing).replace("{stats}", stats.render()))
        if not body:
            self.stats["skipped"] += 1
            return self.stats
        seen_ids = {item.user_id for item in window}
        proposed: list[Rule] = []
        for line in body.splitlines():
            rule = Rule.parse(line)
            if rule is None:
                continue
            # 逐人条只认「这一批观测里真出现过的人」：凭空写一个 w= 就是给不存在的人定打法
            if rule.who and rule.who not in seen_ids:
                rule.who = ""
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
        ask = getattr(self, "_ask_fn", None)
        if ask is not None:
            try:
                body = await asyncio.wait_for(
                    ask(prompt, max_tokens=400, temperature=0.3,
                        timeout=self._settings.judgment_timeout),
                    timeout=self._settings.judgment_timeout + 2.0)
                return str(body or "").strip()
            except Exception as exc:  # noqa: BLE001 - 攒判断失败不是谁的错
                logger.debug("判断循环没问出东西：%s", exc)
                return ""
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

    def bind_ask(self, ask: Any) -> None:  # noqa: ANN401 - 「按提示词问一句」的可调用
        """把攒判断那一问接到引擎的上游池上去。

        生产路径该用这个而不是 `bind_client`：换家、按实测速度挑线、同时赛跑
        都在 `bot.ask_once` 那条链上，直接绑一个 `AsyncOpenAI` 等于把这一层的
        延迟工程整个绕过去——攒一次判断堵住后台二十秒，得不偿失。
        """
        self._ask_fn = ask

    def bind_client(self, client: Any, model: str = "") -> None:  # noqa: ANN401
        """引擎起来之后把上游客户端接上。没接上时循环只做核对、不做新判断。"""
        self._client = client
        if model:
            self._settings = self._settings.model_copy(update={"judgment_model": model})
