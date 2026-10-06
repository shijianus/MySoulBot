"""配对短句与打招呼：每次现生成，不用固定话术。

为什么不用固定激活语。原来那句「你好溟汐，我是管理员」**写在仓库里**——
任何读过代码的人都知道该喊什么。配对要证的其实是「谁能看到这台机器的控制台」，
而一句人尽皆知的固定话术证明不了这件事，只是给撞库的人递了密码本。

所以改成：**每一场配对现生成一句 10 字以内的短句**，只出现在这一次的控制台输出里，
2 分钟过期，答错即作废。上限 15 字是硬闸——超过就说明模型在写句子而不是给口令，
那种东西既难抄又容易撞。

生成失败也要能配对，所以有一条本地兜底：从语料里随机拼，同样短、同样一次性。
兜底不是降级到「用回那句固定的」——那等于把刚补上的洞又捅开。

兜底的随机必须是**密码学意义的那种**：这一句是防撞库的第一道，`random` 是梅森旋转，
观察过几次输出就能把内部状态推出来，所以这里每个选择都走 `secrets`。

成功后她得**主动跟管理者打个招呼**，这是「我认出来你了」的实测证据，
不是日志里一行 `paired_at`。招呼的话也是现生成的，不是模板。

模型这一路有**硬预算**（默认 5 秒）：免费小模型动不动先思考 40 秒不落正文，
而发起配对的人就站在控制台前等。到点没结果就本地现拼，不空等。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import secrets
from typing import Any, Final

logger: Final = logging.getLogger("mysoulbot.pairphrase")

__all__ = ["MAX_PHRASE_CHARS", "make_phrase", "make_greeting", "phrase_ok",
           "local_phrase", "short_ask"]

# 目标 10 字，硬上限 15 字。超过 15 的一律当模型没听懂，走兜底。
TARGET_PHRASE_CHARS: Final[int] = 10
MAX_PHRASE_CHARS: Final[int] = 15

_PHRASE_PROMPT: Final[str] = (
    "给我一句**配对口令**：由你现编，用于确认站在控制台前的是我主人。\n"
    "要求：不超过 10 个字，口语，像一句没头没尾但他说得出口的话；"
    "不要标点，不要引号，不要解释，不要输出候选，只给那一句。\n"
    "别用「管理员」「验证」「口令」这类词本身，也别用「你好」「在吗」这种谁都会说的。\n"
    "从「{seed}」这个意象出发去想，但别把它原样抄进来。\n"
)

# 提示词里必须带随机种子：同一个免费小模型拿到同一句提问，三次会给出同一句口令
# （实测就是「那把旧伞还没收」连出三遍）。口令每场不同才是防撞库的前提，
# 只靠 temperature 兜不住。
_SEEDS: Final[tuple[str, ...]] = ("深海灯", "末班船", "屋檐下的雨", "融化的冰", "旧车站",
                                  "半夜的厨房", "退潮后的沙滩", "没寄出的信", "天台的风",
                                  "第七节车厢", "结冰的湖面", "打烊的书店", "绿皮火车",
                                  "凌晨四点的码头", "忘在桌上的茶", "走调的口琴")


def _pick(pool: tuple[str, ...]) -> str:
    """每一次挑选都走 `secrets`。`random.choice` 是梅森旋转：
    看过若干输出就能推状态，下一个「随机」的句子是可预测的——
    而这句是要拿来挡撞库的。"""
    return pool[secrets.randbelow(len(pool))]


def short_ask(ask: Any, settings: Any) -> Any:  # noqa: ANN401 - 引擎的 ask_once 与它的配置
    """把引擎那句「问一条线路」配成口令/招呼语该用的样子。

    现生成十个字不该先排到一条 40 秒不落正文的主力线路上：
    没点名线路时**所有线路同时问**，谁先落正文用谁；点名了（`PAIR_PHRASE_ROUTE`）
    就只按那一条打头、按顺序退——一次便宜请求，不铺开打一枪。
    两种都受同一个硬预算管（超时由 `make_phrase` 兜）。
    """
    budget = float(settings.pair_phrase_deadline_seconds) or 5.0
    route = str(settings.pair_phrase_route or "")
    return functools.partial(ask, max_tokens=48, timeout=budget,
                             prefer=route, race=not route)


async def make_phrase(ask: Any = None, *, deadline: float = 5.0) -> str:  # noqa: ANN401 - async (str) -> str
    """要一句配对口令。`ask` 是可用的问模型函数；到点没拿到合规格的就本地现拼。

    `deadline` 是**整段预算**（含重试），不是每次尝试各给一份：
    人站在控制台前等，一条 40 秒才落正文的免费线路不该把配对卡在那儿。
    兜底绝不退回「那句固定的旧话术」——那等于把刚补上的洞又捅开。
    """
    loop = asyncio.get_running_loop()
    stop = loop.time() + max(0.0, deadline)
    if ask is not None and deadline > 0:
        prompt = _PHRASE_PROMPT.replace("{seed}", _pick(_SEEDS))
        for _ in range(2):   # 第一次不合规格就再要一次，比直接掉到本地兜底好
            left = stop - loop.time()
            if left <= 0:
                logger.info("口令生成没赶上 %.0f 秒预算，本地现拼", deadline)
                break
            try:
                raw = await asyncio.wait_for(ask(prompt), timeout=left)
            except asyncio.TimeoutError:
                logger.info("口令生成超过 %.0f 秒预算，本地现拼", deadline)
                break
            except Exception as exc:  # noqa: BLE001 - 问不出来就用兜底，别卡住配对
                logger.warning("现生成口令失败，用本地兜底：%s", exc)
                break
            body = _clean(str(raw))
            if phrase_ok(body):
                return body
            if body:
                logger.warning("现生成的口令不合规格（%d 字），再要一次", len(body))
    return local_phrase()

_GREET_PROMPT: Final[str] = (
    "配对成功了——站在控制台前的这个人确认是你主人。主动跟他打个招呼。\n"
    "要求：不超过 40 个字；只说你现在知道是他、以及接下来你打算怎么跟他配合；\n"
    "不许复述规则、不许报「配对完成」这种系统腔、不许问他身份证号之类的东西。\n"
    "按你自己的脾气说。\n"
)

# 兜底语料：槽位之间随机拼。刻意不含任何「管理员/验证/口令」字样，
# 也不与旧那句固定话术重叠。四个槽的取值数相乘（再乘上模板数）≈ 2^17，
# 手打的人不可能在 120 秒里试完——这才是这句要挡住的东西。
_A: Final[tuple[str, ...]] = ("热的", "半糖的", "昨天的", "第三趟", "没睡醒的", "倒着游",
                              "不打烊", "会发光", "没写完的", "赖床的", "逆流的", "迟到的",
                              "结霜的", "走调的", "空转的", "没校准的", "靠岸的", "没人要的",
                              "忘在桌上的", "记不清的")
_B: Final[tuple[str, ...]] = ("鲸鱼", "水族箱", "米饭", "尾鳍", "潮汐", "夜航船", "珊瑚",
                              "潜水钟", "海带", "声呐", "浮标", "深海灯", "灯塔", "末班车",
                              "车站", "雨伞", "炉子", "粥", "月亮", "锚", "缆绳", "贝壳",
                              "海图", "锅")
_C: Final[tuple[str, ...]] = ("不翻身", "在等雨", "先熄灯", "记得喂", "别吵它", "归位了",
                              "刚靠岸", "不上岸", "数到七", "留了灯", "不认路", "朝北游",
                              "还没凉", "该关了", "早收了", "没上锁", "在漏水", "换了位",
                              "认了人", "等潮来", "记错了", "靠得住", "没人管", "熄了一半")
_D: Final[tuple[str, ...]] = ("吧", "呢", "来着", "不成", "是吧", "呀", "嘛", "记得", "快",
                              "该管管", "还在", "没走")
_TEMPLATES: Final[tuple[tuple[str, ...], ...]] = (
    ("A", "B", "C"), ("B", "C"), ("B", "C", "D"), ("A", "B", "C", "D"), ("A", "B"))
_SLOTS: Final[dict[str, tuple[str, ...]]] = {"A": _A, "B": _B, "C": _C, "D": _D}

_STRIP: Final[re.Pattern[str]] = re.compile(r"[\"“”‘’「」『』《》【】\[\]()（）。，！？!?,.;；:：\s]+")


def _clean(raw: str) -> str:
    """只留第一行、去掉引号与标点。**不在这儿裁长度**——
    先裁再验长度等于把「硬上限 15 字」偷偷变成「取前 15 字」，
    模型写一整句长话就会被悄悄截断当成口令，那是最坏的一种"看起来通过了"。"""
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    return _STRIP.sub("", line).strip()


def phrase_ok(text: str) -> bool:
    """能不能当这一场的口令。太长的直接否——那是模型在写句子不是给口令。"""
    body = _clean(text)
    if len(body) > MAX_PHRASE_CHARS:
        return False
    if not 2 <= len(body) <= MAX_PHRASE_CHARS:
        return False
    # 撞库风险最高的就是那句写在仓库里的旧话术，以及任何带「管理员/验证/口令」的通用话
    for banned in ("管理员", "验证", "口令", "配对", "password", "token"):
        if banned in body:
            return False
    return True


def local_phrase() -> str:
    """本地现拼一句：短、一次性、每个选择都来自 `secrets`。"""
    for _ in range(30):
        body = "".join(_pick(_SLOTS[slot]) for slot in _pick(_TEMPLATES))
        if 4 <= len(body) <= TARGET_PHRASE_CHARS and phrase_ok(body):
            return body
    return _pick(_B) + _pick(_C)


_GREET_FALLBACK: Final[tuple[str, ...]] = (
    "行了，是我。往后的事你说，我看着办。",
    "认出你了。这台机器以后归咱俩管。",
    "嗯，是我。你直接说事，不用跟我客气。",
    "对上了。那我不装客气的了——有事就说。",
    "是我本人。刚那口令也就你说得出来。",
)


async def make_greeting(ask: Any = None, *, fallback: str = "",
                        deadline: float = 5.0) -> str:  # noqa: ANN401
    """配对成功后她跟管理者说的话。到点没生成出来，就随机取一句人话——
    「我认出你了」这件事不该被一条慢上游拖成一分钟的沉默。"""
    if ask is not None and deadline > 0:
        try:
            raw = str(await asyncio.wait_for(ask(_GREET_PROMPT), timeout=deadline) or "").strip()
        except asyncio.TimeoutError:
            logger.info("招呼语没赶上 %.0f 秒预算，随机取一句", deadline)
            raw = ""
        except Exception as exc:  # noqa: BLE001 - 打招呼失败不该让配对算失败
            logger.warning("打招呼没生成出来：%s", exc)
            raw = ""
        if raw:
            return re.sub(r"\s*\n\s*", " ", raw)[:120]
    return fallback or _pick(_GREET_FALLBACK)
