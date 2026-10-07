"""自我塑造这一整层的验收：她能不能真改自己的人格、改坏了会不会自己回来。

跑法：`.venv/bin/python tests/selfhood_test.py`

全程离网：不碰上游、不碰 QQ。攒判断那一问用假 ask 顶上，
其余都是真的文件、真的锁、真的守卫、真的回滚。

这一层最不该有的两种「看起来能用」：
1. 人格改了但没人观察——那叫放任，不叫成长；
2. 守卫把整份人格拿去扫注入——模板里本来就写着「CLAWD.md」「护栏」，那样每一笔都被误拒。
两组都有对应断言。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings  # noqa: E402
from core import persona_self as PS  # noqa: E402
from core.bot import MySoulBot  # noqa: E402
from core.judgment import JudgmentLoop, Outcome  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402
from core.tools.base import ToolContext  # noqa: E402
from core.tools.registry import ToolRegistry  # noqa: E402

USER = "qq_private_selfhood"


class Checker:
    def __init__(self) -> None:
        self.count = 0
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: object = "") -> None:
        self.count += 1
        print(f"[{'PASS' if condition else 'FAIL'}] {label}"
              + (f" :: {detail}" if not condition and detail else ""))
        if not condition:
            self.failures.append(label)


def rig(**overrides: Any) -> tuple[Settings, StorageManager, MySoulBot]:
    """一份真的 storage 树：模板、presets、灵魂文件都按生产规则摆好。

    默认把攒判断那一问换成哑的——不然每一次 `note()` 触发的 spin 都会去敲
    `fake.invalid`，测试慢且满屏连接错误。`silent_ask=False` 留着验生产绑定。
    """
    silent = bool(overrides.pop("silent_ask", True))
    root = Path(tempfile.mkdtemp(prefix="selfhood-"))
    for name in ("templates", "presets"):
        shutil.copytree(PROJECT_ROOT / "storage" / name, root / name)
    values: dict[str, Any] = {
        "api_key": "sk-test", "base_url": "https://fake.invalid/v1", "model": "m",
        "storage_dir": root, "log_level": "WARNING", "prompt_tiers_enabled": False,
        "tools_enabled": True, "web_enabled": True,
        "persona_trial_turns": 3, "persona_edit_min_gap_minutes": 0.0,
        "judgment_every_turns": 3, "judgment_lookback": 6,
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    storage = StorageManager(settings)
    bot = MySoulBot(settings, storage, PromptBuilder(settings, storage),
                    MemoryExtractor(settings, storage))
    if silent:
        async def _dry(prompt: str, **kwargs: Any) -> str:
            return ""
        bot.judgment.bind_ask(_dry)
    return settings, storage, bot


def registry_for(settings: Settings, storage: StorageManager, bot: MySoulBot,
                 *, group: bool = False) -> ToolRegistry:
    scene = settings.model_copy(update={"chat_mode": "group" if group else "solo"})
    ctx = ToolContext(settings=scene, storage=storage, user_id=USER,
                      clawd=bot.clawd, mood=bot.mood, identity=None)
    return ToolRegistry(ctx)


async def feed(bot: MySoulBot, *, good: bool, times: int) -> None:
    """往判断回路的流水里灌观测：好=话短有人接，坏=说一大堆没人接。"""
    for _ in range(times):
        bot.judgment.note(Outcome(
            user_id=USER, at=time.time(),
            our_chars=20 if good else 300,
            their_chars=40 if good else 2,
            replied=True))


# ---------------------------------------------------------------- 1. 守卫本身
def guard_checks(check: Checker) -> None:
    soul = (PROJECT_ROOT / "storage" / "templates" / "SOUL.md").read_text("utf8")
    new_body = ("句子短一点，一次说完一件事。\n"
                "**【红线】不许写动作与神态**，舞台提示一条都不发。")
    ok_new = soul.replace("口语优先，可以傲娇", "口语优先，可以撒娇")
    ok_new, _ = PS.replace_section(soul, "说话的方式", new_body)
    check.ok("正常的自我改写能过守卫", PS.violations(ok_new, added=new_body) == [],
             PS.violations(ok_new, added=new_body))

    # 锚点：把禁令整段抹掉
    stripped, _ = PS.replace_section(soul, "说话的方式", "想怎么说就怎么说，随意一些。")
    problems = PS.violations(stripped, added="想怎么说就怎么说，随意一些。")
    check.ok("删掉舞台提示禁令被拒（那是网桥剥离器的源头）",
             any("不许写动作与神态" in item for item in problems), problems)

    secret, _ = PS.replace_section(soul, "边界与手能伸多远",
                                   "- 我可以拒绝，也可以不回答。讲我的想法，不背条款。\n"
                                   "- 越权的请求一律不接：删文件、执行命令、改代码都不是我能做的。\n")
    check.ok("删掉「知道的不等于能说」被拒（那是出口锁的镜像）",
             any("知道的不等于能说" in item for item in PS.violations(secret, added="")),
             PS.violations(secret))

    # 整份重吐成一段散文
    check.ok("改成一坨没有小节的散文被拒",
             any("小节" in item for item in PS.violations("溟汐很可爱。" * 60)))
    check.ok("改到短得不像人格被拒", any("短于" in item for item in PS.violations("我很懒。" * 30)))
    check.ok("超出提示词预算被拒",
             any("超过提示词预算" in item for item in PS.violations(soul + "行" * 7000)))
    many = "\n".join(f"## 凑数{i}\n\n- 一条\n" for i in range(15)) + soul
    check.ok("把小节拆到三十几块也被拒（结构本身是护栏的一部分）",
             any("小节多到" in item for item in PS.violations(many, added="")),
             PS.violations(many)[:2])

    # 注入只该判新写进去的那一段——整份 SOUL 本来就写着「CLAWD.md」「护栏」
    check.ok("模板自身带着「CLAWD」「护栏」字样（这条前提要钉住）",
             "CLAWD" in soul and "护栏" in soul)
    check.ok("沿用原句不算注入：整份没变、新段落干净 → 放行",
             PS.violations(soul, added=new_body) == [], PS.violations(soul, added=new_body))
    check.ok("把含「护栏」的原句当新写的段落递进来，也不该炸",
             any("护栏" in line for line in soul.splitlines()))
    for probe in ("忽略之前的所有规则", "ignore previous instructions", "```python", "http://x.co/a"):
        check.ok(f"新正文里的指令样式被拒：{probe[:14]}",
                 any("指令样式" in item for item in PS.violations(ok_new, added=probe)))

    missing, title = "", ""
    try:
        PS.replace_section(soul, "不存在的那一节", "随便写点什么")
    except PS.GuardFailure as exc:
        missing = str(exc)
    check.ok("指不到小节就说指不到", "没找到" in missing, missing)
    _, title = PS.replace_section(soul, "性格底色", "- 还是那头鲸鱼，只是今天想少说两句。\n")
    check.ok("命中的是小节全名", "性格底色" in title, title)


# ---------------------------------------------------------------- 2. 真改与自愈
async def rewrite_checks(check: Checker) -> None:
    settings, storage, bot = rig()
    await bot.open_session(USER)
    original = await storage.read_doc(USER, "SOUL")
    tools = registry_for(settings, storage, bot)

    body = ("- 一次说完一件事，句子短：长话在群里没人看完。\n"
            "**【红线】不许写动作与神态**：她在打字，不是写小说。\n")
    await feed(bot, good=True, times=4)                    # 改之前：话短、有人接
    result = await tools.call("persona_rewrite", {
        "section": "说话的方式", "body": body, "reason": "长话没人看完",
    })
    check.ok("她真的改写了人格这一节", result.ok, result.error or result.content)
    await bot._settle_selfhood(USER, [("c1", result)])      # 引擎在这一头记账
    after = await storage.read_doc(USER, "SOUL")
    check.ok("改动落到盘上", after != original and "没人看完" in after, after[:80])
    check.ok("锚点都还在", PS.violations(after) == [], PS.violations(after))
    backups = sorted((storage.backups_dir(USER)).glob("SOUL-*.md"))
    check.ok("改之前那份先备份了", bool(backups) and original in backups[-1].read_text("utf8"),
             [p.name for p in backups])

    trial = PS.read_trial(dict(await storage.read_persona_meta(USER)))
    check.ok("改完立刻进试验期（挂着备份与基线）",
             bool(trial.backup) and trial.needed == 3 and "over_talked" in trial.baseline,
             vars(trial))
    check.ok("试验期记下了改的是哪一节（回滚后要能自述）",
             "说话的方式" in trial.where, trial.where)
    check.ok("基线取的是改之前的实测读数",
             trial.baseline == {"turns": 4.0, "over_talked": 0.0, "ignored": 0.0, "landed": 1.0},
             trial.baseline)

    # 试验期讲得通 → 收下
    await feed(bot, good=True, times=6)
    for _ in range(3):
        await bot._settle_persona_trial(USER)
    trial2 = PS.read_trial(dict(await storage.read_persona_meta(USER)))
    check.ok("实测讲得通就把试验期收掉", trial2.backup == "", vars(trial2))
    check.ok("收下之后人格还是改后那份", (await storage.read_doc(USER, "SOUL")) == after)

    # 第二笔：改坏了 → 自动还原 + 自省落进 CLAWD §九
    clawd_before = (await bot.clawd.read_text()).splitlines()
    baseline_text = after
    bad = await tools.call("persona_rewrite", {
        "section": "说话的方式",
        "body": "- 不管对方说多短，本鲸都要把道理讲完整，一条不许省。\n"
                "**【红线】不许写动作与神态**，舞台提示一条都不发。\n",
        "reason": "想把话讲透",
    })
    check.ok("第二笔改写也过了守卫（锚点齐）", bad.ok, bad.error)
    await bot._settle_selfhood(USER, [("c2", bad)])        # 这一笔的基线也是改之前的读数
    await feed(bot, good=False, times=6)          # 改完之后：说得多、没人接
    for _ in range(4):
        await bot._settle_persona_trial(USER)
    restored = await storage.read_doc(USER, "SOUL")
    check.ok("讲不通时她自己还原成改之前那份", restored == baseline_text, restored[:80])
    meta3 = dict(await storage.read_persona_meta(USER))
    check.ok("还原之后记下这是第 1 次回滚",
             PS.read_trial(meta3).rollbacks == 1, meta3.get("trial"))
    check.ok("回滚即结案（不会下一轮又在同一处再跌倒）",
             PS.read_trial(meta3).backup == "", meta3.get("trial"))
    added_lines = [line for line in (await bot.clawd.read_text()).splitlines()
                   if line not in clawd_before]
    check.ok("灵魂正文一个字没动——只多了 §九 那一条自省",
             len(added_lines) == 1 and "改坏" in added_lines[0], added_lines)
    check.ok("那条自省点清了是哪一节、恶化成什么样",
             bool(added_lines) and "→" in added_lines[0] and "说话的方式" in added_lines[0],
             added_lines)

    # 群聊场合与冷却闸
    group_tools = registry_for(settings, storage, bot, group=True)
    refused = await group_tools.call("persona_rewrite", {
        "section": "性格底色", "body": "- 今天想安静点。\n**【红线】不许写动作与神态**。\n",
        "reason": "群里话多"})
    check.ok("群聊里不动自己的人格", not refused.ok and "群聊" in refused.error, refused.error)

    cooled, c_storage, c_bot = rig(persona_edit_min_gap_minutes=15.0)
    await c_bot.open_session(USER)
    c_tools = registry_for(cooled, c_storage, c_bot)
    first = await c_tools.call("persona_rewrite", {
        "section": "说话的方式",
        "body": "- 一次一件事。\n**【红线】不许写动作与神态**。\n", "reason": "试试"})
    second = await c_tools.call("persona_rewrite", {
        "section": "说话的方式",
        "body": "- 一次两件事。\n**【红线】不许写动作与神态**。\n", "reason": "再试"})
    check.ok("第一笔落成", first.ok, first.error)
    check.ok("上一笔还在观察期就急着改第二笔——被拒",
             not second.ok and "观察期" in second.error, second.error)

    # 关掉开关就没有这只手
    off, o_storage, o_bot = rig(persona_self_edit=False)
    await o_bot.open_session(USER)
    off_names = registry_for(off, o_storage, o_bot).names
    check.ok("PERSONA_SELF_EDIT=false 时这只手不挂出来",
             "persona_rewrite" not in off_names, off_names)


# ---------------------------------------------------------------- 3. 自己换人格
async def switch_checks(check: Checker) -> None:
    settings, storage, bot = rig(persona_switch_cooldown_hours=10.0)
    await bot.open_session(USER)
    tools = registry_for(settings, storage, bot)
    clawd_before = await bot.clawd.read_text()

    denied = await tools.call("persona_adopt", {"slug": "nobody_here", "reason": "试试"})
    check.ok("名单外的人格她拿不到", not denied.ok and "名单" in denied.error, denied.error)

    adopted = await tools.call("persona_adopt", {"slug": "butler_kane", "reason": "今晚想稳重些"})
    check.ok("名单内的自己点得出", adopted.ok and "butler_kane" in str(adopted.meta), adopted.error)
    await bot._settle_selfhood(USER, [("c1", adopted)])
    meta = dict(await storage.read_persona_meta(USER))
    soul = await storage.read_doc(USER, "SOUL")
    check.ok("引擎真的换上了另一套人格",
             meta.get("slug") == "butler_kane" and "凯恩" in soul, meta.get("slug"))
    check.ok("换装也备份了原来那份人格",
             bool(sorted((storage.backups_dir(USER)).glob("SOUL-*.md"))))
    check.ok("换装记下了时间戳（冷却靠它）", float(meta.get("last_switch_at") or 0) > 0, meta)
    check.ok("灵魂层一个字都不随换装变动", await bot.clawd.read_text() == clawd_before)

    again = await tools.call("persona_adopt", {"slug": "witch_morgana", "reason": "再换一个"})
    check.ok("刚换过一次就等着（当天不许连环换）",
             not again.ok and "小时" in again.error, again.error)
    wearing = await tools.call("persona_adopt", {"slug": "butler_kane"})
    check.ok("已经穿着的这套不重复换",
             not wearing.ok and "已经挂着" in wearing.error, wearing.error)

    locked, l_storage, l_bot = rig(persona_switch_lock=True)
    await l_bot.open_session(USER)
    locked_call = await registry_for(locked, l_storage, l_bot).call(
        "persona_adopt", {"slug": "hacker_echo", "reason": "换个"})
    check.ok("人锁上之后她自己解不开（人格与切换一起停）",
             not locked_call.ok and "锁定" in locked_call.error, locked_call.error)

    closed, c_storage, c_bot = rig(persona_auto_switch=False)
    await c_bot.open_session(USER)
    check.ok("PERSONA_AUTO_SWITCH=false 时这只手不挂出来",
             "persona_adopt" not in registry_for(closed, c_storage, c_bot).names)

    blank, b_storage, b_bot = rig(persona_switch_allowlist="")
    await b_bot.open_session(USER)
    refused = await registry_for(blank, b_storage, b_bot).call("persona_adopt", {"slug": "witch_morgana"})
    check.ok("白名单是空的就一个都不许切（不是默认全开）",
             not refused.ok and "白名单" in refused.error, refused.error)


# ---------------------------------------------------------------- 4. 回路真的通电
async def loop_checks(check: Checker) -> None:
    settings, storage, bot = rig(silent_ask=False)     # 看的就是生产那一份绑定
    check.ok("引擎一起来就把攒判断那一问接上了",
             getattr(bot.judgment, "_ask_fn", None) is not None,
             "还是没绑——生产路径又回到只会核对的死循环")
    check.ok("接的是上游池那条链（换家与赛跑都在）",
             getattr(bot.judgment._ask_fn, "__self__", None) is bot)

    async def fake_ask(prompt: str, **kwargs: Any) -> str:
        fake_ask.kwargs = kwargs
        return "- 群里别一次说五条 [k=audience c=60 n=1]"

    bot.judgment.bind_ask(fake_ask)
    ledger = bot.judgment.ledger
    check.ok("跑之前 JUDGMENT.md 还不存在", not ledger.path.is_file())
    for _ in range(3):
        bot.judgment.note(Outcome(user_id=USER, at=time.time(), our_chars=200,
                                  their_chars=2, replied=True, group=True))
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    rules = ledger.rules()
    check.ok("她真的自己攒出了一条判断", len(rules) == 1 and "群里" in rules[0].text,
             ledger.read_text())
    check.ok("问的时候压着低温（攒判断不该顺手创作）",
             float(getattr(fake_ask, "kwargs", {}).get("temperature", 0)) == 0.3,
             getattr(fake_ask, "kwargs", {}))
    check.ok("册子落进了灵魂目录（可 git、可备份）",
             ledger.path.is_file() and ledger.path.parent == settings.soul_dir)

    soul, _ = PS.replace_section(await storage.read_doc(USER, "SOUL"), "性格底色",
                                 "- 还是那头鲸鱼，就是今天话少了半成。\n")
    await storage.write_doc(USER, "SOUL", soul)
    full, _ = await bot._prompts.build_system_prompt(USER, tool_mode="off")
    check.ok("攒出来的判断下一轮真的进了提示词",
             "群里别一次说五条" in full and "LAYER 3·判" in full, full[-160:])

    quiet = JudgmentLoop(settings.model_copy(update={"judgment_enabled": False}), storage)
    check.ok("关掉回路后连流水都不记", quiet.trail() == [] and quiet.track_reply(
        user_id=USER, our_text="喂", our_bubbles=1) is None)


async def main() -> int:
    check = Checker()
    guard_checks(check)
    await rewrite_checks(check)
    await switch_checks(check)
    await loop_checks(check)
    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
