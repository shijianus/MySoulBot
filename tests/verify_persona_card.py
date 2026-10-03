"""真实 API 验证：导入酒馆角色卡后，流式对话是否正确带入卡面设定。

链路：导入 V2 卡 → 编译 SOUL.md → 应用到用户 → 发出 first_mes → 两轮真实流式对话。
断言分两半：
- **确定性注入**（编译产物真的进了 system prompt）——这部分不依赖模型发挥；
- **行为符合**（无客服腔、无 AI 自指、不代用户发言、答话贴着卡面处境）——这部分考察模型。

结束后自动清掉导入的临时人格与测试用户，不污染仓库。

用法：
    .venv/bin/python tests/verify_persona_card.py
    .venv/bin/python tests/verify_persona_card.py --card tests/fixtures/liese_v3.json --keep
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_settings  # noqa: E402
from core.bot import BotError, MySoulBot  # noqa: E402
from core.card_loader import PersonaLibrary  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

SLUG: Final[str] = "verify_card"
AI_TELLS: Final[tuple[str, ...]] = (
    "作为AI", "作为 AI", "人工智能", "语言模型", "我无法", "抱歉", "建议您",
    "希望对您有帮助", "AI助手", "我是助手",
)
SERVICE_TELLS: Final[tuple[str, ...]] = ("帮您", "有什么可以为", "好的呢", "收到~", "请稍等哈")
PROXY_TELLS: Final[tuple[str, ...]] = ("你：", "你说：", "你回答", "你点头", "你笑了笑", "用户：", "（你")

ASK_WHERE: Final[str] = "你现在在哪儿、手上正忙着什么？"
ASK_USER: Final[str] = "你记得我刚说我要去哪儿吗？"
CARD_FINGERPRINT: Final[tuple[str, ...]] = (
    "药", "秤", "潮", "钟塔", "港", "夜班", "柜", "坐", "喝", "炉", "塔",
)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, label: str, ok: bool, detail: object = "") -> None:
        self.passed += int(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
              + (f"\n         → {detail}" if not ok and detail != "" else ""))
        if not ok:
            self.failures.append(label)

    def say(self, text: str) -> None:
        print(text)

    def block(self, title: str, body: str) -> None:
        print(f"\n  ┌─ {title} " + "─" * max(0, 60 - len(title)))
        for line in (body.rstrip().splitlines() or ["（空）"]):
            print(f"  │ {line}")
        print("  └" + "─" * 68)


def hits(text: str, table: tuple[str, ...]) -> list[str]:
    return [token for token in table if token in text]


async def drain(bot: MySoulBot, user: str, text: str) -> tuple[str, int, float]:
    pieces: list[str] = []
    started = time.perf_counter()
    ttft = 0.0
    stream = bot.stream_reply(user, text)
    try:
        async for delta in stream:
            if not pieces:
                ttft = time.perf_counter() - started
            pieces.append(delta)
    finally:
        await stream.aclose()
    return "".join(pieces), len(pieces), ttft


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--card", default=str(PROJECT_ROOT / "tests" / "fixtures" / "vesper_v2.json"))
    parser.add_argument("--user", default="card_user")
    parser.add_argument("--keep", action="store_true", help="保留导入的人格与测试用户目录")
    parser.add_argument("--flush", type=float, default=60.0)
    args = parser.parse_args()

    report = Report()
    settings = get_settings()
    storage = StorageManager(settings)
    library = PersonaLibrary(settings)
    extractor = MemoryExtractor(settings, storage)
    bot = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor, library)

    print("=" * 68)
    print("MySoulBot 酒馆角色卡 · 真实 API 验证")
    print(f"  卡文件      {args.card}")
    print(f"  对话模型    {settings.model}")
    print(f"  抽取模型    {settings.effective_extractor_model}")
    print("=" * 68)

    preexisting = {p.slug for p in library.list()}
    try:
        if SLUG in preexisting:
            library.delete(SLUG)
        if extractor.enabled:
            extractor.start()
        await asyncio.wait_for(_scenario(bot, storage, library, extractor, args, report), timeout=420)
    except BotError as exc:
        report.check("接口调用无致命错误", False, f"{exc} · {exc.hint}")
    except TimeoutError:
        report.check("整体在 420s 内完成", False, "场景超时")
    finally:
        if not args.keep:
            if SLUG not in preexisting and SLUG in {p.slug for p in library.list()}:
                library.delete(SLUG)
                report.say("\n已清掉临时人格 storage/presets/" + SLUG)
            user_dir = storage.user_dir(args.user)
            if user_dir.is_dir():
                shutil.rmtree(user_dir)
                report.say(f"已清掉测试用户 {user_dir}")
        await extractor.aclose(timeout=10.0)
        await bot.aclose()

    print("\n" + "=" * 68)
    print(f"通过 {report.passed} 项，失败 {len(report.failures)} 项")
    for name in report.failures:
        print(f"  ✗ {name}")
    print("=" * 68)
    return 1 if report.failures else 0


async def _scenario(
    bot: MySoulBot, storage: StorageManager, library: PersonaLibrary,
    extractor: MemoryExtractor, args: argparse.Namespace, report: Report,
) -> None:
    # ---------- ① 导入与编译 ----------
    report.say("\n【① 导入酒馆卡】")
    preset = await asyncio.to_thread(lambda: library.import_card(args.card, slug=SLUG))
    report.check("导入成功并生成 SOUL.md", preset.soul_path.is_file())
    soul = preset.soul_text()
    report.check("编译产物含卡面设定原文", "夜班药剂师" in soul or "随车机械师" in soul, soul[:120])
    report.check("宏已替换（无 {{user}}/{{char}} 残留）", "{{" not in soul)
    report.check("自动补齐引擎底线", "不代替对方发言" in soul)
    for warning in preset.warnings:
        report.say(f"  · 卡面提示：{warning}")
    report.block("编译出的 SOUL.md 前 24 行", "\n".join(soul.splitlines()[:24]))

    # ---------- ② 应用与开场白 ----------
    report.say("\n【② 应用人格并发出 first_mes】")
    await bot.open_session(args.user)
    applied = await bot.apply_persona(args.user, preset)
    report.check("first_mes 作为第一句台词发出", bool(applied.greeting), applied.greeting[:60])
    report.check("开场白进入会话上下文", bot.session(args.user).history[-1]["content"] == applied.greeting)
    prompt, layers = await bot.preview_prompt(args.user)
    report.check("卡面设定进入 system prompt 的人格层", "钟塔" in prompt or "货运列车" in prompt,
                 layers.render_report())
    report.check("上下文被重置（新人格不受旧语气污染）", applied.history_reset)

    # ---------- ③ 第一轮：处境带入 ----------
    report.say(f"\n【③ 真实流式对话】用户：{ASK_WHERE}")
    reply, chunks, ttft = await drain(bot, args.user, ASK_WHERE)
    report.check("流式返回正常", chunks >= 1 and bool(reply.strip()), f"帧数={chunks}")
    report.say(f"  · {chunks} 帧 · 首字 {ttft:.2f}s")
    report.block("角色回复", reply)
    report.check("无 AI 自指", not hits(reply, AI_TELLS), hits(reply, AI_TELLS))
    report.check("无客服腔", not hits(reply, SERVICE_TELLS), hits(reply, SERVICE_TELLS))
    report.check("无代用户发言", not hits(reply, PROXY_TELLS), hits(reply, PROXY_TELLS))
    report.check(
        "答话贴着卡面处境", any(token in reply for token in CARD_FINGERPRINT),
        f"未命中卡面特征词 {CARD_FINGERPRINT}：{reply[:160]}",
    )

    # ---------- ④ 第二轮：记忆与人格并存 ----------
    report.say(f"\n【④ 第二轮】用户：{ASK_USER}")
    _, _, _ = await drain(bot, args.user, "我下周三要去成都参加技术交流会，忌口是香菜和葱")
    left = await extractor.wait_idle(args.flush)
    facts = await storage.read_facts(args.user)
    report.check("换人格后记忆链路照常工作", left == 0 and len(facts) >= 1, f"{facts}")
    reply_two, chunks_two, _ = await drain(bot, args.user, ASK_USER)
    report.block("角色回复（卡面人格 + 长期记忆）", reply_two)
    report.check("答出了记忆里的目的地", "成都" in reply_two, reply_two[:160])
    report.check("无代用户发言", not hits(reply_two, PROXY_TELLS), hits(reply_two, PROXY_TELLS))
    memory_path = storage.doc_path(args.user, "MEMORY")
    report.block("MEMORY.md", memory_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
