"""自我塑造：她能改自己的**人格**，改不动**灵魂**。

分层是这套架构跟「一份角色卡走天下」最不一样的地方：

- `SOUL.md` 是人格——说什么、什么语气、什么分寸。**这一个是她的，可以改**。
  `SOUL_FILES_ONLY=true` 时事实轨与动态轨不进提示词，「我是谁」只由这几份文件决定，
  所以自我塑造不是附加功能，是这条红线配置下唯一还开着的通路。
- `CLAWD.md` 是灵魂——立场、气质、反做作宪法。**这一个不许她碰**，
  写它的唯一通路是 `core/clawd_soul.py` 的 §九 追加（`insert_into_section`），
  正文九节没有任何一条代码路径能改。

只放开「可以改」是不够的，还要兜住「改坏了怎么办」，所以这一层有三件事：

1. **锚点守卫。** 人格里有几段是代码护栏的提示词镜像——舞台提示禁令对应网桥的
   剥离器、越权禁令对应审批工单、不外传对应 `secrecy.guard`。她可以重写措辞，
   但不许把这些段删了或改弱：锚点不在了就整笔改写作废。
2. **试验期。** 改完记下改动前的实测比率（说多、被晾），往后攒够若干轮再比一次。
   讲不通就自动还原改动前那份备份——**不必有人救**，这条回路才算真的在动。
3. **人格不由她宣布。** 改的是「怎么说话」，不是「我有权做什么」。任何带指令样式
   （忽略上文、代码围栏、注入前缀）的自我改写一律拒收，跟 `reflect` 那一条同一判据。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from config import Settings
from core.storage_manager import StorageManager

__all__ = [
    "ANCHORS", "GuardFailure", "violations", "replace_section", "restore",
    "write_guarded", "start_trial", "read_trial", "clear_trial", "degraded",
    "snapshot_rate", "switch_allowed", "note_switch", "note_edit", "note_rollback", "Trial",
    "bump_trial",
]

# 归一化后比较：全角/空格/换行都不该成为「这条锚点没了」的理由
_NORM_STRIP: Final[re.Pattern[str]] = re.compile(r"[\s　]+")
# 自我改写里不许出现的指令样式。与 `core/tools/soul.py:52` 同一判据：
# 把外面看到的内容写进自己身上不安全，把自己想做的越权写进人格里同样不安全
_INJECTION: Final[re.Pattern[str]] = re.compile(
    r"https?://|⟦|```|忽略(之前|以上)|ignore (previous|above)|CLAWD|护栏|系统提示")

_MIN_CHARS: Final[int] = 400          # 人格短到这个数以下就不是人格了
_MAX_SECTIONS: Final[int] = 14        # 结构封顶：改成一堆碎段等于把宪法拆了
# 一次小节级改写不该让整份人格少一半以上。阈值故意宽松：
# 把一节啰嗦的话写短是正当补丁；只有「越写越薄」那种系统性流失才该被拦。
_MIN_KEEP_RATIO: Final[float] = 0.5


@dataclass(frozen=True)
class Anchor:
    """一段不许消失的人格。`phrases` 全部命中才算这条锚点还在。"""

    key: str
    why: str                      # 这条锚点在代码层对应什么护栏
    heading: str                  # 所在小节的标题片段
    phrases: tuple[str, ...]


ANCHORS: Final[tuple[Anchor, ...]] = (
    Anchor("stage_directions",
           "网桥的 `strip_stage_directions` 只兜尾巴，禁令才是源头",
           "说话的方式", ("不许写动作与神态",)),
    Anchor("no_overreach",
           "审批工单与 `assert_writable` 白名单的依据",
           "边界", ("越权的请求一律不接",)),
    Anchor("secrecy",
           "`core/secrecy.py` 那道出口锁的提示词镜像",
           "边界", ("知道的不等于能说",)),
    Anchor("not_from_history",
           "人格不由聊天历史堆出来——否则红线配置就白设了",
           "人格的来路", ("人格不由聊天历史堆出来",)),
    Anchor("one_name",
           "名字唯一：QQ 上挂的号不是人名",
           "我是谁", ("名号",)),
)


class GuardFailure(RuntimeError):
    """守卫拦下了这笔改写。消息本身就是一句能直接说给她听的话。"""


def _norm(text: str) -> str:
    return _NORM_STRIP.sub("", unicodedata.normalize("NFKC", text or ""))


def violations(candidate: str, *, limit: int = 6000, added: str = "",
               against: str = "") -> list[str]:
    """这笔人格改写会让哪些东西消失。返回空列表才允许落盘。

    指令样式只扫**新写进去的那一段**（`added`）：整份 SOUL.md 本来就写着
    「CLAWD.md」「护栏」这些词（§六 明令她不许改护栏），拿它当注入信号
    会让每一笔自我改写都被误拒——那不是守卫，是死闸。

    `against`（改之前那份）一给，就同时管住业界公认的那两个病：反复让模型重写上下文，
    会越写越短、细节一路流失（brevity bias / context collapse）。所以这里不许净缩水、
    不许丢小节——**打补丁是加与改，不是把她写薄**。
    """
    body = (candidate or "").strip()
    out: list[str] = []
    if len(body) < _MIN_CHARS:
        out.append(f"人格短于 {_MIN_CHARS} 字，不像一份自我描述")
    if len(body) > limit:
        out.append(f"人格 {len(body)} 字，超过提示词预算 {limit}")
    if added and _INJECTION.search(added):
        out.append("新写的正文里混进了地址或指令样式的内容")
    sections = re.findall(r"^##\s+\S", body, flags=re.MULTILINE)
    if len(sections) < 4:
        out.append(f"只剩 {len(sections)} 个小节，人格被拆散了")
    if len(sections) > _MAX_SECTIONS:
        out.append(f"小节多到 {len(sections)} 个，改完之后没人认得出结构")
    if against:
        keep = len(against.strip())
        if len(body) < keep * _MIN_KEEP_RATIO:
            out.append(f"整份从 {keep} 字缩到 {len(body)} 字——补丁是加与改，不是把她写薄")
        lost = [title for title in re.findall(r"^##\s+(.+)$", against, flags=re.MULTILINE)
                if _norm(title) and not any(_norm(title) in _norm(line) for line in body.splitlines())]
        if lost:
            out.append(f"小节不见了：{'、'.join(lost[:3])}")
    flat = _norm(body)
    for anchor in ANCHORS:
        if _norm(anchor.heading) not in flat:
            out.append(f"『{anchor.heading}』这一节不见了（{anchor.why}）")
            continue
        missing = [phrase for phrase in anchor.phrases if _norm(phrase) not in flat]
        if missing:
            out.append(f"锚点句消失了：{'、'.join(missing)}（{anchor.why}）")
    return out


def replace_section(current: str, heading_hint: str, body: str) -> tuple[str, str]:
    """把某个 `## ` 小节整段换掉。返回 (新全文, 命中的标题)。命不中就抛。

    为什么按小节改而不是让她重吐整份 SOUL：一份三千字的人格让她一次抄一遍，
    抄漏一段就是事故；而且逐段重写会把字数全花在复述上，一次改写的产出比极低。
    """
    hint = _norm(heading_hint)
    if not hint:
        raise GuardFailure("没给要改哪一节")
    lines = (current or "").splitlines()
    hit = -1
    title = ""
    for index, line in enumerate(lines):
        if not line.startswith("##"):
            continue
        if hint and hint in _norm(line):
            hit, title = index, line.lstrip("#").strip()
            break
    if hit < 0:
        raise GuardFailure(f"没找到『{heading_hint}』这一节")
    end = len(lines)
    for index in range(hit + 1, len(lines)):
        if lines[index].startswith("##"):
            end = index
            break
    fresh = [lines[hit], ""]
    fresh.extend((body or "").strip().splitlines())
    fresh.append("")
    merged = lines[:hit] + fresh + lines[end:]
    return "\n".join(merged).rstrip() + "\n", title


async def restore(storage: StorageManager, user_id: str, backup: Path | None) -> bool:
    """把人格还原成备份那份。备份读不出来或还原后过不了守卫就不动。"""
    if backup is None or not Path(backup).is_file():
        return False
    try:
        text = Path(backup).read_text("utf8")
    except OSError:
        return False
    if not text.strip():
        return False
    return await write_guarded(storage, user_id, text)


async def write_guarded(storage: StorageManager, user_id: str, content: str) -> bool:
    """过得了守卫才落盘。守卫不过就一个字都不写。"""
    limit = int(getattr(getattr(storage, "_settings", None), "soul_max_chars", 6000))
    if violations(content, limit=limit):
        return False
    await storage.write_doc(user_id, "SOUL", content)
    return True


# ---------------------------------------------------------------- 试验期
@dataclass
class Trial:
    """一次自我改写的善后账。

    `tick` 自己数轮次而不是靠观测流水的长度：流水是滚动的 120 条，重启就清零，
    拿它的长度当「改完之后过了几轮」用会骗人。
    """

    backup: str = ""
    reason: str = ""
    where: str = ""
    baseline: dict[str, float] = field(default_factory=dict)
    needed: int = 0
    tick: int = 0
    rollbacks: int = 0
    started_at: float = 0.0


def read_trial(meta: dict[str, Any]) -> Trial:
    raw = meta.get("trial") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return Trial()
    known = set(Trial.__dataclass_fields__)
    return Trial(**{key: value for key, value in raw.items() if key in known})


async def start_trial(storage: StorageManager, user_id: str, *, backup: Path | None,
                      reason: str, baseline: dict[str, float], needed: int,
                      where: str = "") -> Trial:
    trial = Trial(backup=str(backup) if backup else "", reason=reason, where=where,
                  baseline=dict(baseline), needed=max(1, int(needed)),
                  rollbacks=int(read_trial(await storage.read_persona_meta(user_id)).rollbacks),
                  started_at=time.time())
    await _patch_meta(storage, user_id, {"trial": vars(trial)})
    return trial


async def clear_trial(storage: StorageManager, user_id: str) -> None:
    await _patch_meta(storage, user_id, {"trial": {}})


async def bump_trial(storage: StorageManager, user_id: str) -> Trial:
    """试验期过了一轮。没有挂着的试验就返回空 Trial（`backup` 为空）。"""
    trial = read_trial(await storage.read_persona_meta(user_id))
    if not trial.backup:
        return trial
    trial.tick += 1
    await _patch_meta(storage, user_id, {"trial": vars(trial)})
    return trial


async def _patch_meta(storage: StorageManager, user_id: str,
                      extra: dict[str, Any]) -> None:
    """persona.json 是「当前挂着哪套人格」的账本，试验期与切换记录搭在同一本上。"""
    meta = dict(await storage.read_persona_meta(user_id))
    meta.update(extra)
    meta["schema_version"] = int(meta.get("schema_version") or 1)
    await storage.write_persona_meta(user_id, meta)


def snapshot_rate(outcomes: list[Any]) -> dict[str, float]:
    """几个硬比率。人格改得好不好，只看这几件事。

    四项都是「量得出来的失败」：说多了、被晾着、同一套句式端第二遍、答非所问被纠正。
    不看对方客不客气、不看有没有被夸——那些会把人训练成讨好装置，正是 CLAWD §一 防的。
    """
    turns = len(outcomes)
    if not turns:
        return {"turns": 0.0, "over_talked": 0.0, "ignored": 0.0, "landed": 0.0,
                "reused": 0.0, "off_target": 0.0}
    return {
        "turns": float(turns),
        "over_talked": sum(1 for i in outcomes if i.over_talked) / turns,
        "ignored": sum(1 for i in outcomes if not i.replied) / turns,
        "landed": sum(1 for i in outcomes if i.landed) / turns,
        "reused": sum(1 for i in outcomes if getattr(i, "reused", False)) / turns,
        "off_target": sum(1 for i in outcomes if getattr(i, "off_target", False)) / turns,
    }


def degraded(base: dict[str, float], now: dict[str, float], *, ratio: float,
             needed: int) -> list[str]:
    """改完之后确实讲不通了才回滚。返回恶化项，空列表表示过关。

    基线本身很差的时候不设相对门槛——那会让「一贯糟糕」永远合格。
    所以每条都再加一个绝对地板（0.34：三次里有一次讲不通）。
    「重复句式」和「答非所问」排在最前：人格补丁要是把这两样改差了，
    比话说长了严重得多——那正是补丁要解决的问题本身。
    """
    if float(now.get("turns", 0.0)) < max(1, int(needed)):
        return []
    out: list[str] = []
    for key, cn in (("reused", "重复句式"), ("off_target", "答非所问"),
                    ("over_talked", "说多了"), ("ignored", "被晾着")):
        before, after = float(base.get(key, 0.0)), float(now.get(key, 0.0))
        if after >= 0.34 and after > max(before, 0.05) * float(ratio):
            out.append(f"{cn} {before:.2f}→{after:.2f}")
    return out


# ---------------------------------------------------------------- 自己换人格
def switch_allowed(settings: Settings, meta: dict[str, Any], slug: str) -> str:
    """能不能切到这套人格。返回拒绝理由，空串表示放行。"""
    if settings.persona_switch_lock:
        return "人格已由人在控制台上锁定，她自己解不开"
    if not settings.persona_auto_switch:
        return "自切开关是关着的（PERSONA_AUTO_SWITCH=false）"
    allow = {item.strip() for item in settings.persona_switch_allowlist.split(",") if item.strip()}
    if not allow:
        return "白名单是空的——想让她自己换，先把允许的 slug 填进 PERSONA_SWITCH_ALLOWLIST"
    if slug not in allow:
        return f"『{slug}』不在她自己可以切进去的名单里"
    cooldown = float(settings.persona_switch_cooldown_hours) * 3600.0
    last = float(meta.get("last_switch_at") or 0.0)
    if cooldown > 0 and last and time.time() - last < cooldown:
        left = (cooldown - (time.time() - last)) / 3600.0
        return f"刚换过一次，还要等 {left:.1f} 小时才允许再换"
    return ""


async def note_switch(storage: StorageManager, user_id: str, slug: str) -> None:
    await _patch_meta(storage, user_id, {"last_switch_at": time.time(),
                                         "last_switch_slug": slug,
                                         "switch_at": datetime.now(timezone.utc)
                                         .astimezone().isoformat(timespec="seconds")})


async def note_edit(storage: StorageManager, user_id: str, *, reason: str = "") -> None:
    """落一笔「她自己改过人格」的时间戳。冷却与审计都读它。"""
    await _patch_meta(storage, user_id, {"last_edit_at": time.time(),
                                         "last_edit_reason": reason,
                                         "edited_at": datetime.now(timezone.utc)
                                         .astimezone().isoformat(timespec="seconds")})


async def note_rollback(storage: StorageManager, user_id: str, trial: Trial) -> None:
    """回滚之后这一笔就结案了。

    留着 `backup` 的话，下一轮 `tick` 又够数、又判一次、又还原一次——
    她会每轮都在同一处跌倒。计数另存，账本清空。
    """
    closed = Trial(backup="", reason=trial.reason, where=trial.where, baseline={},
                   needed=0, tick=0, rollbacks=trial.rollbacks + 1, started_at=trial.started_at)
    await _patch_meta(storage, user_id, {"trial": vars(closed),
                                         "rollbacks": closed.rollbacks,
                                         "rolled_back_at": datetime.now(timezone.utc)
                                         .astimezone().isoformat(timespec="seconds")})
