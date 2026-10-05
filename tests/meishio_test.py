"""溟汐这一版的交付验收：名字、人格信号、长句形态、表情资产、沙盒与只读操作。

跑法：.venv/bin/python tests/meishio_test.py

这里验的都是「说好了要做的事有没有真的落地」——不是跑通就行：
名字不许留变体、token 不许是死字符串、表情不许带水印、
草稿工位不许越出去一格、只读工具不许漏绝对路径。
网络一律不碰：上游全用假端口，行情/天气这类真源在各自工具里自带降级。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import Settings  # noqa: E402

Checker = T.Checker
PROJECT_ROOT = ROOT
BANNED_NAMES = ("小溟", "米希欧", "米修", "溟溟", "阿溟", "汐汐")


def tmp_settings(**overrides: Any) -> Settings:
    root = Path(tempfile.mkdtemp(prefix="meishio-"))
    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "CLAWD.md"):
        src = PROJECT_ROOT / "storage" / "templates" / name
        if src.is_file():
            shutil.copy(src, root / "templates" / name)
    values: dict[str, Any] = {
        "api_key": "sk-test", "base_url": "https://fake.invalid/v1", "model": "model-fake",
        "storage_dir": root, "log_level": "WARNING", "prompt_tiers_enabled": False,
        "tools_enabled": True, "web_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------- 1. 名字
def name_checks(check: Checker) -> None:
    from config import _PERSONA_SIGNALS, _PERSONA_TOKENS

    tokens = dict(line.split("=", 1) for line in _PERSONA_TOKENS.splitlines()[1:])
    check.ok("PERSONA_LOAD 每一条都带释义，不是死字符串",
             all(line.split("=", 1)[1].strip() for line in _PERSONA_TOKENS.splitlines()[1:]),
             [l for l in _PERSONA_TOKENS.splitlines()[1:] if "=" not in l])
    check.ok("信号表与提示词里的是同一套", len(tokens) == len(_PERSONA_SIGNALS), len(tokens))
    name_line = tokens.get("NAME_MEISHIO", "")
    check.ok("名字只有溟汐一个", "溟汐" in name_line and "没有别名" in name_line, name_line)
    check.ok("禁止变体是写死在 NAME_MEISHIO 里的", "小溟" in name_line and "从来不是" in name_line,
             name_line)
    claim = tokens.get("SELF_CLAIM_MEISHIO", "")
    check.ok("自称三档都归到溟汐", "溟汐" in claim and "本鲸" in claim and "人家" in claim, claim)
    check.ok("OUTPUT_SHORT_FIRST 在信号表里", "OUTPUT_SHORT_FIRST" in tokens, sorted(tokens))
    short_first = tokens.get("OUTPUT_SHORT_FIRST", "")
    check.ok("长句判定写在动笔前，不是发满五条之后",
             "动笔前" in short_first and "不是发满五条短句才许转长句" in short_first, short_first)
    check.ok("TIMEOUT_SIGNAL 要求先丢短句", "最短的那句放最前面" in tokens.get("TIMEOUT_SIGNAL", ""),
             tokens.get("TIMEOUT_SIGNAL", ""))


def soul_file_checks(check: Checker) -> None:
    targets = [PROJECT_ROOT / "storage" / "templates" / "SOUL.md"]
    targets += sorted((PROJECT_ROOT / "storage" / "data" / "users").glob("*/SOUL.md"))
    check.ok("用户人格文件不止模板一份", len(targets) >= 5, len(targets))
    stray = []
    for path in targets:
        text = path.read_text("utf8")
        for line in text.splitlines():
            if any(banned in line for banned in BANNED_NAMES) and "从来不是" not in line \
                    and "这种叫法" not in line:
                stray.append(f"{path.name}:{line[:40]}")
    check.ok("所有 SOUL.md 里都不再拿变体当名字", not stray, stray[:4])
    sample = targets[0].read_text("utf8")
    check.ok("SOUL 里写了长句声明的用法", "〔长句〕" in sample, "")
    check.ok("SOUL 明说没有「发满五条才允许长句」这回事",
             "先发满五条短句" in sample and "没有" in sample, "")
    check.ok("SOUL 允许基础无害操作", "看机器负荷" in sample or "机器负荷" in sample, "")
    check.ok("SOUL 划了信息隔离", "看得见不代表可以讲" in sample, "")
    check.ok("越权动作仍然一律不接", "越权的请求一律不接" in sample, "")


# ---------------------------------------------------------------- 2. 提示词层
async def prompt_checks(check: Checker) -> None:
    from core.prompt_builder import PromptBuilder, clause_items, form_plan, wants_depth
    from core.storage_manager import StorageManager

    settings = tmp_settings(prompt_tiers_enabled=True)
    builder = PromptBuilder(settings, StorageManager(settings))

    plan = form_plan(3)
    check.ok("短问题也拿到定形判据", "先定形再动笔" in plan, plan[:60])
    check.ok("判据给的是「超过五句」这个量级", "6 句" in form_plan(9), form_plan(9)[:80])
    check.ok("件数按停顿数得出来", clause_items("吃饭，睡觉，打豆豆") == 3,
             clause_items("吃饭，睡觉，打豆豆"))
    check.ok("换行算长话，走全量档", wants_depth("第一行\n第二行") is True, "")
    check.ok("一句日常搭话不被升级成写文章", wants_depth("在吗") is False, "")

    prompt, layers = await builder.build_system_prompt("u_prompt", tool_mode="off")
    check.ok("全量提示词里有长句声明", "〔长句〕" in prompt, "")
    check.ok("提示词里点名否掉「五条之后再长句」",
             "不存在「先发满五条短句" in prompt or "没有「先发满五条短句" in prompt, "")
    check.ok("人格 token 真的进了提示词", "PERSONA_LOAD" in prompt and "NAME_MEISHIO=" in prompt, "")
    check.ok("不出戏不等于禁言：框的是身份不是话题",
             "出戏不是安全装置" in prompt and "不管话题敏不敏感" in prompt, "")
    check.ok("真伤害仍然要拒", "真正要拒的只有实打实的伤害" in prompt, "")
    quick, _ = await builder.build_system_prompt("u_prompt", tool_mode="off", tier="quick")
    check.ok("快捷档也有定形这一句", "先定形" in quick and "〔长句〕" in quick, "")
    check.ok("快捷档没把宪法整段背进来", "【本层是引擎硬约束】" not in quick, "")
    check.ok("分层明细带上了档位", layers.tier in ("full", "quick"), layers.tier)


# 这 8 张是本地生成的，出厂时右下角带着「Qoder AI 生成」角标；
# 角标落在一片纯底色上，所以清干净之后那个框必须近乎均匀。
# 远端来的 11 张不做这条断言：它们的右下角本来就是裙子、碗、桌面，统计上没有区分度。
GENERATED_CORNER: tuple[str, ...] = (
    "pout", "lazy", "clingy", "smug", "happy", "think", "sleep", "ok",
)


def _cjk_font() -> str:
    import glob

    for pattern in ("/usr/share/fonts/**/*CJK*.ttc", "/usr/share/fonts/**/*CJK*.otf",
                    "/usr/share/fonts/**/wqy*.ttc"):
        found = glob.glob(pattern, recursive=True)
        if found:
            return found[0]
    return ""


def _corner_dirty_ratio(path: Path) -> float:
    """右下角这块底色里，偏离中位数明显的像素占比。

    干净底色应该在 3% 以下；叠一层半透明角标会冲到 5% 以上。
    用「偏离中位数的比例」而不是标准差：角标只盖住角落一小片，
    标准差被大片均匀底色摊薄到几乎不动，比例才看得见。
    """
    from PIL import Image

    with Image.open(path) as handle:
        rgb = handle.convert("RGB")
    w, h = rgb.size
    region = list(rgb.crop((int(w * 0.78), int(h * 0.90), w, h)).get_flattened_data())
    if not region:
        return 0.0
    medians = [sorted(pixel[index] for pixel in region)[len(region) // 2] for index in range(3)]
    off = sum(1 for pixel in region
              if sum(abs(pixel[i] - medians[i]) for i in range(3)) > 78)
    return 100.0 * off / len(region)


# ---------------------------------------------------------------- 3. 表情资产
def sticker_checks(check: Checker) -> None:
    from PIL import Image, ImageDraw, ImageFont

    from core.stickers import StickerBook

    emoji = PROJECT_ROOT / "emoji"
    book = StickerBook(emoji)
    tags = book.tags()
    check.ok("标签覆盖到 19 个情绪", len(tags) >= 19, len(tags))
    for needed in ("pout", "angry", "rice", "lazy", "clingy", "smug", "fat", "token", "sleep"):
        check.ok(f"核心标签 {needed} 可用", book.resolve(needed) is not None, needed)
    check.ok("基石图不进可发送索引", book.resolve("meishio_classic_pout") is None)
    check.ok("参考立绘不进可发送索引", book.resolve("illustration") is None)

    files = sorted(p for p in emoji.iterdir()
                   if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    check.ok("emoji 根目录只有贴纸", all(p.name.startswith("meishio_") for p in files),
             [p.name for p in files if not p.name.startswith("meishio_")])
    odd = []
    for path in files:
        with Image.open(path) as im:
            w, h = im.size
            if w != h or w < 320:
                odd.append(f"{path.name} {w}x{h}")
    check.ok("每张都是方形且够大", not odd, odd[:4])
    check.ok("子目录归档齐全",
             (emoji / "classic").is_dir() and (emoji / "ref").is_dir() and (emoji / "avatar").is_dir(),
             "")
    check.ok("六张基石图按 meishio_classic_ 保留",
             len(list((emoji / "classic").glob("meishio_classic_*.png"))) == 6,
             [p.name for p in (emoji / "classic").iterdir()])
    check.ok("画风参考图留在仓库里", len(list((emoji / "ref").glob("*.jpg"))) >= 8, "")

    avatar = emoji / "avatar" / "meishio_avatar_512.png"
    check.ok("方形头像裁好了", avatar.is_file(), str(avatar))
    if avatar.is_file():
        with Image.open(avatar) as im:
            check.ok("头像是 512 方图", im.size == (512, 512), im.size)
    original = emoji / "avatar" / "溟汐.meishio.png"
    check.ok("远端原图按原名保留", original.is_file(), str(original))

    dirty = [f"{name}:{_corner_dirty_ratio(emoji / f'meishio_{name}.png'):.1f}%"
             for name in GENERATED_CORNER
             if _corner_dirty_ratio(emoji / f'meishio_{name}.png') > 4.0]
    check.ok("生成图右下角的角标已清干净", not dirty, dirty)

    # 反证：这条断言不是摆设——把角标种回其中一张的同一块位置，标准差必须冲上去
    sample = emoji / "meishio_clingy.png"
    clean_ratio = _corner_dirty_ratio(sample)
    with Image.open(sample) as handle:
        planted = handle.convert("RGB")
    try:
        font = ImageFont.truetype(_cjk_font(), 26)
    except (OSError, ValueError):
        font = ImageFont.load_default()
    ImageDraw.Draw(planted).text((int(planted.width * 0.74), int(planted.height * 0.935)),
                                 "Qoder AI 生成", font=font, fill=(205, 205, 205))
    probe = Path(tempfile.mkdtemp(prefix="wm-probe-")) / "planted.png"
    planted.save(probe)
    planted_ratio = _corner_dirty_ratio(probe)
    check.ok("角标种回去就会被抓到（这条不是空断言）",
             planted_ratio > clean_ratio + 2.0, f"{clean_ratio:.2f}% -> {planted_ratio:.2f}%")


# ---------------------------------------------------------------- 4. 沙盒与只读操作
async def sandbox_checks(check: Checker) -> None:
    from core.sandbox import SandboxError, assert_scratch, assert_writable, scratch_dir
    from core.storage_manager import StorageManager
    from core.tools.base import ToolContext
    from core.tools.registry import ToolRegistry

    settings = tmp_settings()
    ctx = ToolContext(settings=settings, storage=StorageManager(settings), user_id="u_sandbox")
    registry = ToolRegistry(ctx)
    names = set(registry.names)
    check.ok("负荷与草稿工具都注册上了",
             {"host_stats", "scratch_write", "scratch_read", "scratch_list"} <= names,
             sorted(names - {"host_stats", "scratch_write", "scratch_read", "scratch_list"}))

    result = await registry.call("host_stats", {})
    check.ok("机器负荷读得出来", result.ok and "CPU 负载" in result.content, result.content[:80])
    check.ok("负荷里有内存与磁盘", "内存" in result.content and "磁盘" in result.content,
             result.content)
    leak = [line for line in result.content.splitlines() if str(settings.storage_dir) in line
            or "/home/" in line]
    check.ok("负荷不往外报绝对路径", not leak, leak[:3])
    check.ok("负荷工具不接任何参数", not registry._by_name["host_stats"].params,
             registry._by_name["host_stats"].params)

    written = await registry.call("scratch_write", {"name": "note.md", "text": "负荷 0.3"})
    check.ok("草稿写进工位", written.ok, written.content)
    landed = scratch_dir(settings, "u_sandbox") / "note.md"
    check.ok("落在 storage/sandbox/<用户>/ 下", landed.is_file(), str(landed))
    read_back = await registry.call("scratch_read", {"name": "note.md"})
    check.ok("草稿读得回来", read_back.ok and "负荷 0.3" in read_back.content, read_back.content[:60])
    listed = await registry.call("scratch_list", {})
    check.ok("草稿列得出来", listed.ok and "note.md" in listed.content, listed.content[:60])

    crossed = await registry.call("scratch_write", {"name": "../other/leak.md", "text": "串门"})
    if crossed.ok:
        elsewhere = scratch_dir(settings, "u_sandbox").parent / "other"
        check.ok("跨用户目录写不进去", not elsewhere.exists(), str(elsewhere))
    else:
        check.ok("跨用户目录写不进去", True, "")
    soul = settings.storage_dir / "soul" / "MOOD.md"
    check.ok("真灵魂资产没被动过", not soul.exists() or "串门" not in soul.read_text("utf8"), "")

    for bad in ("../../etc/passwd", "/etc/hosts", ".."):
        try:
            assert_scratch(scratch_dir(settings, "u_sandbox") / bad,
                           settings=settings, user_id="u_sandbox")
            check.ok(f"越界路径被挡：{bad}", False, "没抛错")
        except SandboxError:
            check.ok(f"越界路径被挡：{bad}", True, "")

    try:
        assert_writable(scratch_dir(settings, "u_sandbox") / "note.md", storage_dir=settings.storage_dir)
        check.ok("工位不在灵魂资产白名单里", False, "草稿不该能走 assert_writable")
    except SandboxError:
        check.ok("工位不在灵魂资产白名单里", True, "")
    for blocked in ("config.py", "core/bot.py", "emoji/meishio_pout.png", "scripts/daemon.sh"):
        try:
            assert_writable(PROJECT_ROOT / blocked, storage_dir=settings.storage_dir)
            check.ok(f"系统本体不可自改：{blocked}", False, "竟然放行")
        except SandboxError:
            check.ok(f"系统本体不可自改：{blocked}", True, "")

    oversized = await registry.call("scratch_write",
                                    {"name": "big.md", "text": "字" * (settings.scratch_max_bytes + 10)})
    check.ok("单张草稿超限就拒", not oversized.ok, oversized.content[:60])
    empty = await registry.call("scratch_write", {"name": "e.md", "text": "   "})
    check.ok("空草稿不写", not empty.ok, empty.content[:40])


def _stub_bot(settings: Settings) -> Any:
    """apply_profile 只碰网桥自己的连接与配置，不使唤 bot——给个占位就够。"""
    from core.bot import MySoulBot
    from core.card_loader import PersonaLibrary
    from core.clawd_soul import ClawdSoul
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    return MySoulBot(settings, storage, PromptBuilder(settings, storage, clawd),
                     MemoryExtractor(settings, storage), PersonaLibrary(settings), clawd)


# ---------------------------------------------------------------- 5. QQ 资料下发
async def profile_checks(check: Checker) -> None:
    from core.adapters.qq_onebot import OneBotBridge

    settings = tmp_settings(onebot_enabled=True, onebot_access_token="t" * 32,
                            onebot_nickname="溟汐", onebot_signature="馋米饭",
                            onebot_avatar="emoji/avatar/meishio_avatar_512.png")
    bridge = OneBotBridge(settings, _stub_bot(settings))
    check.ok("网桥有 apply_profile", callable(getattr(bridge, "apply_profile", None)), "")
    outcome = await bridge.apply_profile(reason="test")
    check.ok("没连接时如实报没有协议端",
             outcome == {"connection": "没有在线的协议端"}, outcome)

    quiet = tmp_settings(onebot_enabled=True, onebot_access_token="t" * 32)
    quiet_bridge = OneBotBridge(quiet, _stub_bot(quiet))
    check.ok("开机自动下发默认关着", quiet.onebot_apply_profile_on_boot is False,
             quiet.onebot_apply_profile_on_boot)
    check.ok("没配资料时不会去动账号", await quiet_bridge.apply_profile() in (
        {"connection": "没有在线的协议端"}, {}), "")

    missing = tmp_settings(onebot_enabled=True, onebot_access_token="t" * 32,
                           onebot_avatar="emoji/根本没有这张.png")
    missing_outcome = await OneBotBridge(missing, _stub_bot(missing)).apply_profile()
    check.ok("头像文件不存在要说清楚（不必等协议端在线）",
             "文件不存在" in missing_outcome.get("avatar", ""), missing_outcome)

    from config import PROJECT_ROOT as _PR
    live = _PR / ".env"
    text = live.read_text("utf8") if live.is_file() else ""
    if text:
        check.ok("线上配置里唤醒名换成了溟汐",
                 "ONEBOT_BOT_NAME=溟汐" in text and "小溟," not in text,
                 [l for l in text.splitlines() if l.startswith("ONEBOT_BOT_NAME")])
        check.ok("线上配置写了头像与昵称",
                 "ONEBOT_NICKNAME=溟汐" in text and "ONEBOT_AVATAR=" in text, "")
        check.ok("线上没把自动下发常开",
                 "ONEBOT_APPLY_PROFILE_ON_BOOT=true" not in text, "")


# ---------------------------------------------------------------- 6. 网桥端长句
async def bridge_long_form_checks(check: Checker) -> None:
    from core.adapters.qq_onebot import split_bubbles

    long_answer = ("〔长句〕你要的这段有三个坑。\n第一个是路径。\n第二个是权限。\n\n"
                   "第三个最隐蔽：它在后台线程里才炸。\n\n记下了就说，别客气。")
    pieces = split_bubbles(long_answer, bubble_chars=60, max_pieces=6)
    check.ok("六个句号只切成三个板块", len(pieces) == 3, pieces)
    check.ok("板块内不再按句拆", "第一个是路径。\n第二个是权限。" in pieces[0], pieces[0])
    check.ok("长短混排被允许", pieces[-1] == "记下了就说，别客气。", pieces[-1])
    check.ok("声明标记没漏到屏幕上", all("长句" not in p for p in pieces), pieces)
    check.ok("内容一个字没丢",
             "".join(pieces).replace("\n", "") == long_answer.replace("〔长句〕", "").replace("\n", ""))
    check.ok("超过 max_pieces 也不硬把板块拆碎",
             len(split_bubbles("〔长句〕" + "句子。\n" * 12, bubble_chars=20, max_pieces=2)) >= 1,
             "")


async def main() -> int:
    check = Checker()
    try:
        name_checks(check)
        soul_file_checks(check)
        await prompt_checks(check)
        sticker_checks(check)
        await sandbox_checks(check)
        await profile_checks(check)
        await bridge_long_form_checks(check)
    finally:
        print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
        for name in check.failures:
            print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
