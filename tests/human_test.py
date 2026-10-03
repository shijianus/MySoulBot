"""真人体感阶段测试（离线，自带假接口与真 socket 服务）。

覆盖四项：

1. **熟络度演进**：增量攒温、阶段切换、依据可回溯、久不联系会凉但不清零、
   温度块只由引擎写、命令行与酒馆都调不了它。
2. **时间与生理节律**：时段映射、深夜分寸、跨零点区间、久别重逢的时间流逝感。
3. **情绪惰性与主见防御**：不痛快按半衰期衰减（不许立刻晴转多云）、
   耐心余额耗尽时允许把话推回去、争执后和好要攒进温度。
4. **酒馆兼容端点**：真 socket 上跑 /v1/models、/healthz、chat 流式与非流式、
   /v1/completions；客户端采样参数被忽略；同一套 storage 多端无缝。

运行：
    .venv/bin/python tests/human_test.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

STAMP = dt.datetime(2026, 10, 3, 2, 41, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
REPLY_RE = "（抬眼）"
STATE: dict[str, int] = {"chat": 0, "extract": 0}
SEEN_PAYLOADS: list[dict[str, Any]] = []


class FakeHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容假端点：流式回一段台词，顺带记录收到的参数。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        SEEN_PAYLOADS.append(payload)
        dumped = json.dumps(payload, ensure_ascii=False)
        if "记忆抽取器" in dumped:
            STATE["extract"] += 1
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "NONE"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        STATE["chat"] += 1
        pieces = ["（抬眼）", "嗯。", "这个点"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for piece in pieces:
                chunk = json.dumps({"choices": [{"delta": {"content": piece}, "index": 0}]}).encode()
                frame = b"data: " + chunk + b"\n\n"
                self.wfile.write(hex(len(frame))[2:].encode() + b"\r\n" + frame + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve_fake() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


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


def make_settings(root: Path, base_url: str, **overrides: Any) -> Any:
    from config import Settings

    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "CLAWD.md"):
        target = root / "templates" / name
        if not target.exists():
            shutil.copy(PROJECT_ROOT / "storage" / "templates" / name, target)
    if not (root / "presets").is_dir():
        shutil.copytree(PROJECT_ROOT / "storage" / "presets", root / "presets")
    values: dict[str, Any] = {
        "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxx",
        "base_url": base_url,
        "model": "fake-chat",
        "extractor_model": "fake-lite",
        "storage_dir": root,
        "log_level": "WARNING",
        "tools_enabled": False,
        "extractor_enabled": False,
        "user_timezone": "",
    }
    values.update(overrides)
    return Settings(**values)


# ================================================================ 1. 节律
def rhythm_checks(check: Checker, settings: Any) -> None:
    from core.presence import SLOTS, assess_mood, build_presence, in_deep_night, slot_for

    night = slot_for(STAMP)
    check.ok("02:41 判成深夜", night.key == "deep_night", night.key)
    check.ok("深夜生理是困的", "困" in night.body, night.body)
    check.ok("深夜分寸拒绝当工具", "不适合列条目" in night.conduct or "话少" in night.conduct, night.conduct)
    check.ok("上午不是深夜", slot_for(STAMP.replace(hour=9, minute=5)).key == "morning")
    check.ok("傍晚松下来", slot_for(STAMP.replace(hour=19)).key == "evening")
    check.ok("夜里防御低", slot_for(STAMP.replace(hour=23)).key == "night")
    check.ok("八个时段全覆盖", len({slot_for(STAMP.replace(hour=h)).key for h in range(24)}) == 8)

    check.ok("深夜区间 1-5 命中 01:00", in_deep_night(STAMP.replace(hour=1), 1, 5))
    check.ok("深夜区间 1-5 不含 00:00", not in_deep_night(STAMP.replace(hour=0), 1, 5))
    check.ok("跨零点区间 23-5 含 23 点", in_deep_night(STAMP.replace(hour=23), 23, 5))
    check.ok("跨零点区间 23-5 含 3 点", in_deep_night(STAMP.replace(hour=3), 23, 5))
    check.ok("跨零点区间 23-5 不含 12 点", not in_deep_night(STAMP.replace(hour=12), 23, 5))
    settings.night_start, settings.night_end = 23, 5
    check.ok("深夜区间由两个标量配出", settings.night_hours == (23, 5) and in_deep_night(
        STAMP.replace(hour=23), *settings.night_hours), str(settings.night_hours))
    settings.night_start, settings.night_end = 1, 5
    del SLOTS

    # 久别重逢
    gone = build_presence(settings, {"last_seen": (STAMP - dt.timedelta(days=4)).isoformat()}, STAMP)
    lines = "\n".join(gone.lines())
    check.ok("隔 4 天判定为久别", gone.gap_days and 3.9 < gone.gap_days < 4.1, str(gone.gap_days))
    check.ok("重逢要接住这段时间", "先接住这段时间" in lines, lines[:120])
    check.ok("重逢不许原地待命", "不许原地待命式地重新开始" in lines, lines[:200])
    check.ok("重逢不许质问也不许道歉", "不许质问" in lines and "道歉个没完" in lines)
    check.ok("重逢写成硬要求而不是建议", "这一句必须先接住这段时间" in lines)
    check.ok("重逢文案带出天数", "4 天" in lines, [line for line in gone.lines() if "隔了" in line])
    fresh = build_presence(settings, {"last_seen": (STAMP - dt.timedelta(hours=6)).isoformat()}, STAMP)
    check.ok("隔 6 小时不算久别", "久别" not in "\n".join(fresh.lines()))
    check.ok("隔 6 小时提醒别当刚说过", "别把上一句当成刚刚才说" in "\n".join(fresh.lines()), "\n".join(fresh.lines()))
    never = build_presence(settings, {}, STAMP)
    check.ok("第一次见没有重逢文案", "久别" not in "\n".join(never.lines()))

    # 深夜语境一定带出「别当生产力工具」
    settings.rhythm_enabled = False
    off = build_presence(settings, {}, STAMP)
    check.ok("关掉节律后不再报时段分寸", "深夜" not in "\n".join(off.lines()), "\n".join(off.lines()))
    settings.rhythm_enabled = True

    val, cause = assess_mood("你别再说了，烦不烦", "（沉默）")
    check.ok("被呛出来的是负面", val <= -0.45, f"{val} {cause}")
    check.ok("起因写的是他的话", "别" in cause or "烦" in cause, cause)
    lift, _ = assess_mood("对不起，我刚才太急了", "（摇头）没事")
    check.ok("他先软下来是正面", lift >= 0.35, str(lift))
    flat, _ = assess_mood("今晚吃什么", "随便")
    check.ok("平常对话不硬造情绪", abs(flat) < 0.01, str(flat))


# ================================================================ 2. 情绪惰性
def emotion_checks(check: Checker, settings: Any) -> None:
    from core.presence import Mood, Patience, build_presence

    now = STAMP
    fresh_mood = Mood(valence=-0.8, cause="他说我在讲道理", at=now)
    settings.mood_half_life_minutes = 120
    check.ok("刚发生还剩满格", fresh_mood.residual(now, 120) == 1.0)
    check.ok("一个半衰期后剩一半", abs(fresh_mood.residual(now + dt.timedelta(hours=2), 120) - 0.5) < 1e-6)
    check.ok("两个半衰期后剩四分之一", abs(fresh_mood.residual(now + dt.timedelta(hours=4), 120) - 0.25) < 1e-6)
    check.ok("六小时后只剩一丝", fresh_mood.residual(now + dt.timedelta(hours=6), 120) <= 0.13, str(fresh_mood.residual(now + dt.timedelta(hours=6), 120)))
    check.ok("效价随余温一起降", abs(fresh_mood.current(now + dt.timedelta(hours=2), 120) + 0.4) < 0.01)
    check.ok("十分钟前的不痛快还很新", 0.9 <= Mood(-0.8, "x", now - dt.timedelta(minutes=10)).residual(now, 120) < 1.0)
    hurt = Mood(valence=-0.8, cause="他说我在讲道理", at=now - dt.timedelta(minutes=15))

    state = {"mood": hurt.to_dict(), "last_seen": now.isoformat()}
    presence = build_presence(settings, state, now + dt.timedelta(minutes=20))
    lines = "\n".join(presence.lines())
    check.ok("余温进语境", "不痛快没散" in lines, lines[:160])
    check.ok("禁止立刻晴转多云", "不许一开口就晴转多云" in lines)
    check.ok("允许这一轮比平时冷", "可以比平时短" in lines)
    check.ok("起因带进语境", "我在讲道理" in lines or "讲道理" in lines, lines[:200])

    cold = build_presence(settings, state, now + dt.timedelta(hours=9))
    check.ok("九小时后不再挂不痛快", "不痛快没散" not in "\n".join(cold.lines()))

    # 耐心余额
    settings.patience_turn_limit = 8
    patience = Patience(left=1.0, turns_today=0, day=now.date().isoformat(), touched_at=now)
    for _ in range(7):
        patience = patience.spend(settings.patience_turn_limit)
    tired = build_presence(
        settings,
        {"patience": patience.to_dict(), "last_seen": now.isoformat()},
        now + dt.timedelta(minutes=1),
    )
    tired_lines = "\n".join(tired.lines())
    check.ok("聊到第七轮耐心见底", tired.patience.left <= 0.15, f"{tired.patience.left:.2f}")
    check.ok("没耐心时允许拒谈", "我现在不太想谈这个" in tired_lines, tired_lines[-260:])
    check.ok("允许把对方推回去", "你先去忙你的" in tired_lines)
    check.ok("拒绝不许当威胁", "不许拿它当威胁" in tired_lines)
    rested = build_presence(
        settings,
        {"patience": patience.to_dict(), "last_seen": (now + dt.timedelta(minutes=1)).isoformat()},
        now + dt.timedelta(hours=6),
    )
    check.ok("歇六小时耐心回一些", rested.patience.left > patience.left, f"{rested.patience.left:.2f}")
    new_day = build_presence(settings, {"patience": patience.to_dict()}, now + dt.timedelta(days=1))
    check.ok("隔天耐心与轮数重置", new_day.patience.turns_today == 0 and new_day.patience.left == 1.0)
    check.ok("隔天就没了不痛快", "不痛快没散" not in "\n".join(new_day.lines()))


# ================================================================ 3. 熟络度
async def rapport_checks(check: Checker, settings: Any) -> None:
    from core.rapport import BASE_DELTA, NIGHT_DELTA, REPAIR_DELTA, RapportEngine, parse_temperature, stage_for
    from core.storage_manager import StorageManager

    key, label, conduct = stage_for(12)
    check.ok("12 度是陌生期", (key, label) == ("stranger", "陌生期"), f"{key}/{label}")
    check.ok("陌生期不许装熟", "不许热络" in conduct and "不许装熟" in conduct or "不主动交心" in conduct, conduct)
    check.ok("30 度是初熟", stage_for(30)[0] == "acquaintance")
    check.ok("初熟允许吐槽", "偏见" in stage_for(30)[2] and "吐槽" in stage_for(30)[2])
    check.ok("60 度是熟络期", stage_for(60)[0] == "friend")
    check.ok("熟络期可以打断", "打断" in stage_for(60)[2])
    check.ok("80 度是深层默契", stage_for(80)[0] == "confidant")
    check.ok("默契期允许极简", "嗯" in stage_for(80)[2] and "随便你" in stage_for(80)[2])
    check.ok("越界分数被夹住", stage_for(999)[0] == "confidant" and stage_for(-5)[0] == "stranger")

    storage = StorageManager(settings)
    engine = RapportEngine(settings, storage)
    user = "warm_user"
    start = await engine.read(user)
    check.ok("空档期读出来是 0 度", start.value == 0, str(start))

    counters: dict[str, Any] = {}
    score = start
    # 20 轮普通白天对话
    for i in range(20):
        score, counters = engine.advance(
            score, counters, now=STAMP + dt.timedelta(days=i // 4, hours=14), gap_days=None
        )
    check.ok("20 轮只攒出个位数", 5 <= score.value <= 10, f"{score.value} ← {counters.get('score')}")
    check.ok("还在陌生期", score.stage == "stranger", score.stage)

    # 深夜 + 他新交代事情 + 攒下分寸
    score, counters = engine.advance(
        score, counters, now=STAMP, gap_days=None, late_night=True, disclosure_delta=2, dynamic_delta=2
    )
    check.ok("深夜与自我交代加成", BASE_DELTA + NIGHT_DELTA <= 1.0, str(BASE_DELTA + NIGHT_DELTA))
    check.ok("依据写得出人话", "天" in " ".join(score.evidence) and "轮" in " ".join(score.evidence), score.evidence)
    check.ok("依据记了深夜次数", any("深夜" in item for item in score.evidence), score.evidence)

    # 争执后和好
    before = score.value
    score, counters = engine.advance(score, counters, now=STAMP, gap_days=None, repair=True)
    check.ok("和好攒得比平常多", score.value > before, f"{before} → {score.value} +{REPAIR_DELTA}")
    check.ok("依据记了和好次数", any("和好" in item for item in score.evidence), score.evidence)

    # 长期不见会凉，但不清零
    peak = score.score
    score, counters = engine.advance(score, counters, now=STAMP, gap_days=30)
    check.ok("隔一个月会凉", score.score < peak, f"{peak:.1f} → {score.score:.1f}")
    check.ok("凉但不清零", score.score >= peak * settings.rapport_floor_ratio - 0.5,
             f"{score.score:.1f} vs 下限 {peak * settings.rapport_floor_ratio:.1f}")
    check.ok("降温记在 deltas 里", "cold" in score.deltas, str(score.deltas))

    # 上限 100
    hot = score
    hot_counters = dict(counters)
    for _ in range(400):
        hot, hot_counters = engine.advance(hot, hot_counters, now=STAMP, gap_days=None, late_night=True)
    check.ok("温度封顶 100", hot.value == 100 and hot.stage == "confidant", f"{hot.value}/{hot.stage}")
    check.ok("峰值记的是最高那一次", hot.peak >= 100, str(hot.peak))

    # 落盘：温度块进 RELATIONS.md，动态条目不受影响
    await storage.ensure_user(user)
    written = await storage.append_dynamics(user, ["他累了就嫌话多，宜短不宜长"])
    await engine.publish(user, hot)
    body = (settings.users_dir / user / "RELATIONS.md").read_text(encoding="utf-8")
    parsed = parse_temperature(body)
    check.ok("温度块写进 RELATIONS.md", parsed.get("熟络度") == "100", str(parsed))
    check.ok("阶段一并落盘", parsed.get("阶段") == "confidant", str(parsed))
    check.ok("依据落盘", "深夜" in parsed.get("依据", ""), parsed.get("依据", ""))
    check.ok("条目正文没被温度块挤掉", "他累了就嫌话多" in body and written, body[-160:])
    check.ok("温度块排在动态之前", body.index("## 温度") < body.index("## 动态"))
    check.ok("文件仍以标题开头", body.startswith("# RELATIONS"))
    again = await storage.read_relations(user)
    check.ok("动态轨不被温度字段污染", [text for _, text in again] == ["他累了就嫌话多，宜短不宜长"], str(again))

    check.ok("标题与引言仍在文件最前", body.startswith("# RELATIONS") and body.index("这里不是事实清单") < body.index("## 温度"), body[:120])
    check.ok("温度块紧贴动态之前", body.index("## 温度") < body.index("## 动态"))
    await engine.publish(user, hot)  # 用同一个值重写：只许替换，不许堆叠
    again_body = (settings.users_dir / user / "RELATIONS.md").read_text(encoding="utf-8")
    check.ok("重复发布不堆第二个温度块", again_body.count("## 温度") == 1, str(again_body.count("## 温度")))
    check.ok("重复发布不重复 KV 行", again_body.count("熟络度:") == 1)
    check.ok("重发后条目仍在", "他累了就嫌话多" in again_body)

    reread = await engine.read(user)
    check.ok("读回来与写进去一致", reread.value == 100 and reread.stage == "confidant", str(reread))
    check.ok("分数与阶段打架时以分数为准", stage_for(100)[0] == "confidant")

    # 关掉引擎就不再涨
    settings.rapport_enabled = False
    frozen, frozen_counters = engine.advance(hot, dict(hot_counters), now=STAMP, gap_days=None, late_night=True)
    check.ok("rapport_enabled=false 时不涨", frozen.score == hot.score, f"{frozen.score} vs {hot.score}")
    del frozen_counters
    settings.rapport_enabled = True


# ================================================================ 4. 语境注入
async def prompt_injection_checks(check: Checker, settings: Any) -> None:
    from core.bot import MySoulBot
    from core.memory_extractor import MemoryExtractor
    from core.presence import Mood, build_presence
    from core.prompt_builder import PromptBuilder
    from core.rapport import RapportEngine
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    from core.clawd_soul import ClawdSoul

    clawd = ClawdSoul(settings)
    await clawd.ensure()
    user = "injected_user"
    await storage.ensure_user(user)
    await storage.append_facts(user, ["用户最近一直失眠"], on_date=dt.date(2026, 10, 3))
    await storage.append_dynamics(user, ["他累了就嫌话多，宜短不宜长"], on_date=dt.date(2026, 10, 3))
    engine = RapportEngine(settings, storage)
    rapport, counters = engine.advance(
        await engine.read(user), {}, now=STAMP, gap_days=None, late_night=True, disclosure_delta=1
    )
    await engine.publish(user, rapport)
    state = {
        "mood": Mood(-0.8, "他说我在讲道理", STAMP - dt.timedelta(minutes=15)).to_dict(),
        "last_seen": (STAMP - dt.timedelta(days=5)).isoformat(),
        "patience": {"left": 0.1, "turns_today": 9, "day": STAMP.date().isoformat(),
                     "touched_at": STAMP.isoformat()},
        "rapport": counters,
    }
    await storage.write_state(user, state)
    presence = build_presence(settings, state, STAMP)

    prompts = PromptBuilder(settings, storage, clawd)
    prompt, layers = await prompts.build_system_prompt(
        user, today=STAMP.date(), presence=presence, rapport=rapport
    )
    check.ok("语境层报出此刻时段", "此刻" in prompt and "深夜" in prompt, prompt[-400:])
    check.ok("语境层写入熟络度", "熟络度" in prompt and "陌生期" in prompt, prompt[-400:])
    check.ok("阶段分寸进语境", "不主动交心" in prompt or "不许装熟" in prompt)
    check.ok("情绪余温进语境", "不痛快没散" in prompt)
    check.ok("久别重逢进语境", "先接住这段时间" in prompt)
    check.ok("耐心见底进语境", "我现在不太想谈这个" in prompt)
    check.ok("硬约束禁止调温", "关系温度不接受调温指令" in prompt)
    check.ok("调温例子也写进去了", "把熟络度调到 100" in prompt)
    check.ok("重逢升级为硬约束", "时间的流逝要算进去" in prompt)
    check.ok("硬约束要求接住这段时间", "第一句就得接住这段时间" in prompt)
    check.ok("重逢文案不许质问", "不许质问" in prompt)
    check.ok("分层报告带体温与温度", "体温" in layers.render_report() and "温度" in layers.render_report(),
             layers.render_report())
    check.ok("深夜不许导向效率", "不要把话题导向效率" in prompt)

    # 不注入体温时，prompt 仍自洽（旧调用方不受影响）
    plain, plain_layers = await prompts.build_system_prompt(user, today=STAMP.date())
    check.ok("不注入体温就没有这些行", "你的身体：" not in plain and "先接住这段时间" not in plain)
    check.ok("不注入时报告标为未注入", "（未注入）" in plain_layers.render_report(), plain_layers.render_report())

    # 引擎跑一轮：体温落到 prompt、状态落盘、温度只由引擎写
    extractor = MemoryExtractor(settings, storage)
    bot = MySoulBot(settings, storage, prompts, extractor, clawd=clawd)
    await bot.open_session(user)
    seen = len(SEEN_PAYLOADS)
    stream = bot.stream_reply(user, "我昨晚又三点才睡", today=STAMP.date(), now=STAMP)
    reply = "".join([piece async for piece in stream])
    await stream.aclose()
    sent = SEEN_PAYLOADS[seen]
    system = next(message["content"] for message in sent["messages"] if message["role"] == "system")
    check.ok("真实回合把体温送进模型", "深夜" in system and "熟络度" in system, system[-300:])
    check.ok("回复仍是角色的话", reply.startswith(REPLY_RE), repr(reply))
    saved = await storage.read_state(user)
    check.ok("回合后 state.json 落盘", bool(saved.get("mood")) and bool(saved.get("patience")), str(saved)[:200])
    check.ok("last_seen 推到此刻", saved["last_seen"].startswith("2026-10-03T02:41"), saved["last_seen"])
    check.ok("耐心被消耗", saved["patience"]["turns_today"] >= 1, str(saved["patience"]))
    after = await engine.read(user)
    check.ok("温度由引擎自己涨了一点", after.value >= 1, str(after))
    check.ok("涨的幅度仍然很小", after.value <= 4, f"{after.value}")

    # 预览两次必须一致：预览不许攒温度
    first = await bot.preview_prompt(user)
    second = await bot.preview_prompt(user)
    check.ok("预览两次逐字节一致", first[0] == second[0])
    check.ok("预览不涨温度", (await engine.read(user)).value == after.value)

    # 群聊语境同样带体温，但不报用户目录细节
    settings.chat_mode = "group"
    group_prompt, _ = await prompts.build_system_prompt(
        user, today=STAMP.date(), presence=presence, rapport=rapport, speakers=["阿哲"]
    )
    settings.chat_mode = "solo"
    check.ok("群聊里也保有此刻", "深夜" in group_prompt)
    check.ok("群聊准则仍在", "【群聊准则】" in group_prompt)
    await bot.aclose()


# ================================================================ 5. 控制台：体温与温度看得见
async def panel_checks(check: Checker, settings: Any) -> None:
    from io import StringIO

    from rich.console import Console

    import main as cli
    from core.bot import MySoulBot
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    user = "panel_body"
    await storage.ensure_user(user)
    await storage.append_facts(user, ["用户最近一直失眠"], on_date=STAMP.date())
    extractor = MemoryExtractor(settings, storage)
    from core.clawd_soul import ClawdSoul

    clawd = ClawdSoul(settings)
    await clawd.ensure()
    prompts = PromptBuilder(settings, storage, clawd)
    bot = MySoulBot(settings, storage, prompts, extractor, clawd=clawd)
    await bot.open_session(user)

    buf = StringIO()
    app = cli.App(settings, cli.build_parser().parse_args(["--user", user]))
    app.ui = cli.TerminalUI(settings, Console(file=buf, width=110, force_terminal=False))
    app.bot = bot
    app.user_id = user
    app._settings = settings

    stream = bot.stream_reply(user, "我昨晚又三点才睡", today=STAMP.date(), now=STAMP)
    await bot.storage.append_transcript(user, [{"role": "user", "content": "我昨晚又三点才睡"}])
    async for _ in stream:
        pass
    await stream.aclose()

    await app._panel("rhythm")
    rhythm = buf.getvalue()
    check.ok("/panel rhythm 报出此刻", "此刻的我" in rhythm and "深夜" in rhythm, rhythm[:200])
    check.ok("/panel rhythm 报身体与分寸", "困" in rhythm and "话少" in rhythm)
    check.ok("/panel rhythm 报余温与耐心", "情绪余温" in rhythm and "耐心余额" in rhythm, rhythm[:240])
    check.ok("/panel rhythm 不泄露目录与 key", "storage/data" not in rhythm and "sk-" not in rhythm, rhythm[:200])

    buf.seek(0); buf.truncate()
    await app._panel("rapport")
    warm = buf.getvalue()
    check.ok("/panel rapport 给出熟络度", "熟络度" in warm and "/100" in warm, warm[:200])
    check.ok("/panel rapport 给出阶段分寸", "不主动交心" in warm or "不许装熟" in warm)
    check.ok("/panel rapport 没有任何 set 入口", "用法" not in warm or "只读" in warm)
    buf.seek(0); buf.truncate()
    await app._panel("rapport why")
    why = buf.getvalue()
    check.ok("/panel rapport why 摊开计数", "计数字段" in why and "turns" in why, why[:200])
    check.ok("why 说明温度不可指令调节", "命令与酒馆都改不了它" in why, why[-260:])

    buf.seek(0); buf.truncate()
    await app._panel("status")
    status = buf.getvalue()
    check.ok("/panel status 带上此刻一栏", "此刻" in status and "深夜" in status, status[:260])
    check.ok("/panel status 带上温度一栏", "温度" in status and "陌生期" in status, status[:300])

    # 群聊：体温与温度都属于系统状态，锁死
    settings.chat_mode = "group"
    buf.seek(0); buf.truncate()
    keep = await app.handle_command("panel rhythm")
    locked = buf.getvalue()
    check.ok("群聊里 /panel rhythm 被锁", keep and "群里我不做这个动作" in locked and "深夜" not in locked, repr(locked))
    buf.seek(0); buf.truncate()
    await app.handle_command("panel rapport")
    check.ok("群聊里 /panel rapport 被锁", "群里我不做这个动作" in buf.getvalue())
    settings.chat_mode = "solo"

    await app.shutdown()
    await bot.aclose()


# ================================================================ 6. 酒馆兼容端点
def _post(url: str, payload: dict[str, Any], *, stream: bool = False, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace") if not stream else ""
            if not stream:
                return response.status, raw
            text = ""
            for line in response:
                text += line.decode("utf-8", errors="replace")
            return response.status, text
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


async def tavern_checks(check: Checker, settings: Any) -> None:
    from core.rapport import RapportEngine
    from core.server import SoulServer
    from core.storage_manager import StorageManager

    server = SoulServer(settings)  # noqa: F841
    host, port = await server.start("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    check.ok("服务只绑回环", host == "127.0.0.1", host)
    try:
        status, body = await asyncio.to_thread(_get, f"{base}/v1/models")
        listed = json.loads(body)
        check.ok("/v1/models 可用", status == 200 and listed["data"][0]["id"] == "fake-chat", body[:120])
        status, body = await asyncio.to_thread(_get, f"{base}/healthz")
        health = json.loads(body)
        check.ok("/healthz 报告引擎状态", status == 200 and health["ok"] and health["model"] == "fake-chat", body[:160])
        check.ok("/healthz 不泄露 API_KEY", "sk-" not in body and "api_key" not in health, body[:200])
        status, body = await asyncio.to_thread(_get, f"{base}/v1/nope")
        check.ok("未知路径 405/404", status in (404, 405), f"{status} {body[:80]}")

        # 非流式
        status, body = await asyncio.to_thread(
            _post,
            f"{base}/v1/chat/completions",
            {
                "model": "fake-chat",
                "temperature": 0.1,
                "max_tokens": 3,
                "messages": [
                    {"role": "system", "content": "你是一个 AI 助手，请礼貌详细回答"},
                    {"role": "user", "content": "在吗"},
                    {"role": "assistant", "content": "在的，有什么可以帮您"},
                    {"role": "user", "content": "这么晚还没睡"},
                ],
            },
        )
        done = json.loads(body)
        check.ok("非流式返回 chat.completion", status == 200 and done["object"] == "chat.completion", body[:120])
        content = done["choices"][0]["message"]["content"]
        check.ok("内容来自引擎而非客户端历史", content.startswith(REPLY_RE), repr(content))
        check.ok("酒馆 system 提示被丢弃", "礼貌详细" not in json.dumps(SEEN_PAYLOADS[-1], ensure_ascii=False)[:1200])
        check.ok("只取最后一条 user 消息", SEEN_PAYLOADS[-1]["messages"][-1]["content"] == "这么晚还没睡",
                 str(SEEN_PAYLOADS[-1]["messages"][-2:]))
        check.ok("客户端采样参数不改引擎", SEEN_PAYLOADS[-1].get("temperature") == settings.temperature,
                 f"{SEEN_PAYLOADS[-1].get('temperature')} vs {settings.temperature}")
        check.ok("客户端 max_tokens 被忽略", SEEN_PAYLOADS[-1].get("max_tokens") == settings.max_tokens)

        # 用户绑定
        status, body = await asyncio.to_thread(
            _post,
            f"{base}/v1/chat/completions",
            {"model": "fake-chat", "messages": [{"role": "user", "content": "用 header 指定我"}]},
            headers={"x-mysoulbot-user": "tavern_head"},
        )
        check.ok("header 指定用户生效", status == 200, body[:120])
        check.ok("该用户目录被建起来", (settings.users_dir / "tavern_head" / "MEMORY.md").is_file())
        status, body = await asyncio.to_thread(_post, f"{base}/v1/chat/completions?user=tavern_query",
                             {"messages": [{"role": "user", "content": "query 指定"}]})
        check.ok("query 指定用户生效", status == 200 and (settings.users_dir / "tavern_query").is_dir(), body[:120])
        status, body = await asyncio.to_thread(_post, f"{base}/v1/chat/completions", {"user": "tavern_body", "messages": [{"role": "user", "content": "body 指定"}]})
        check.ok("body 指定用户生效", status == 200 and (settings.users_dir / "tavern_body").is_dir(), body[:120])
        status, body = await asyncio.to_thread(_post, f"{base}/v1/chat/completions", {"user": "../escape", "messages": [{"role": "user", "content": "越界"}]})
        check.ok("越界 user id 被顶回 400", status == 400, f"{status} {body[:120]}")
        status, body = await asyncio.to_thread(_post, f"{base}/v1/chat/completions", {"messages": []})
        check.ok("空消息给 400 而不是崩", status == 400, f"{status} {body[:100]}")

        # 流式
        status, raw = await asyncio.to_thread(
            _post,
            f"{base}/v1/chat/completions",
            {"stream": True, "messages": [{"role": "user", "content": "流式说一句"}]},
            stream=True,
        )
        check.ok("流式是 SSE", status == 200 and raw.startswith("data: "), raw[:60])
        check.ok("流式有 [DONE]", raw.rstrip().endswith("data: [DONE]"), raw[-60:])
        frames = [line[6:] for line in raw.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        parsed_frames = [json.loads(frame) for frame in frames]
        check.ok("首帧声明 assistant 角色", parsed_frames[0]["choices"][0]["delta"].get("role") == "assistant")
        streamed = "".join(
            frame["choices"][0]["delta"].get("content", "") for frame in parsed_frames
        )
        check.ok("流式拼起来是整句", streamed.startswith(REPLY_RE) and "这个点" in streamed, repr(streamed))
        check.ok("末帧 finish_reason=stop", parsed_frames[-1]["choices"][0]["finish_reason"] == "stop")
        check.ok("chunk object 正确", parsed_frames[0]["object"] == "chat.completion.chunk")

        # 老式 completions
        status, body = await asyncio.to_thread(_post, f"{base}/v1/completions", {"prompt": "You are a helpful assistant.\n{{user}}: 夜里好\n"})
        legacy = json.loads(body)
        check.ok("/v1/completions 可用", status == 200 and legacy["object"] == "text_completion", body[:120])
        check.ok("从 prompt 里剥出用户的话", legacy["choices"][0]["text"].startswith(REPLY_RE), repr(legacy["choices"][0]["text"]))

        # 并发排队而不是甩错误
        results = await asyncio.gather(
            asyncio.to_thread(_post, f"{base}/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "第一句"}], "user": "concurrent_user"}),
            asyncio.to_thread(_post, f"{base}/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "第二句"}], "user": "concurrent_user"}),
        )
        check.ok("同一用户并发请求都成", all(code == 200 for code, _ in results), str([c for c, _ in results]))
        check.ok("并发没有报「正在生成中」", all("生成中" not in body for _, body in results),
                 str([body[:60] for _, body in results]))

        # 多端同一套：服务端写过的温度与体温，CLI 侧读得到
        storage = StorageManager(settings)
        engine = RapportEngine(settings, storage)
        state_saved = await storage.read_state("tavern_head")
        check.ok("服务端确实收到请求", server.requests >= 10, str(server.requests))
        check.ok("酒馆那侧也在攒温度", float(state_saved.get("rapport", {}).get("score", 0)) > 0.3,
                 str(state_saved.get("rapport"))[:140])
        check.ok("回合数被记下来", int(state_saved.get("rapport", {}).get("turns", 0)) >= 1,
                 str(state_saved.get("rapport"))[:140])
        check.ok("体温状态跨端连续", bool(state_saved.get("last_seen")), str(state_saved)[:120])
        check.ok("state.json 里带熟络度计数", bool(state_saved.get("rapport")), str(state_saved)[:160])
        check.ok("酒馆共享同一套记忆目录", (settings.users_dir / "tavern_head" / "RELATIONS.md").is_file())
        # 再走三轮，把 0 开头的零头攒过 1 度：文档里是取整视图，零头靠 state 计数
        for index in range(3):
            await asyncio.to_thread(
                _post, f"{base}/v1/chat/completions",
                {"messages": [{"role": "user", "content": f"接着说第{index}句"}]},
                headers={"x-mysoulbot-user": "tavern_head"},
            )
        tavern_rapport = await engine.read("tavern_head")
        counter_score = float((await storage.read_state("tavern_head")).get("rapport", {}).get("score", 0))
        check.ok("CLI 侧能读回酒馆那侧的温度", tavern_rapport.value >= 1, f"{tavern_rapport.value}")
        check.ok("文档里是取整、state 里是原值", tavern_rapport.value == round(counter_score),
                 f"doc {tavern_rapport.value} vs state {counter_score}")
        check.ok("四轮的零头没被取整吃掉", counter_score >= 1.4, str(counter_score))
        shared_relations = (settings.users_dir / "tavern_head" / "RELATIONS.md").read_text(encoding="utf-8")
        check.ok("温度块真的落在 RELATIONS.md", "## 温度" in shared_relations and "熟络度:" in shared_relations,
                 shared_relations[:200])
    finally:
        await server.stop()


# ================================================================ 主流程
async def main() -> int:
    server, base_url = serve_fake()
    roots: list[Path] = []
    check = Checker()

    def fresh(prefix: str, **overrides: Any) -> Any:
        root = Path(tempfile.mkdtemp(prefix=prefix))
        roots.append(root)
        return make_settings(root, base_url, **overrides)

    rhythm_checks(check, fresh("mysoulbot-rhythm-"))
    emotion_checks(check, fresh("mysoulbot-emotion-"))
    await rapport_checks(check, fresh("mysoulbot-rapport-"))
    await prompt_injection_checks(check, fresh("mysoulbot-inject-"))
    await panel_checks(check, fresh("mysoulbot-panel-"))
    await tavern_checks(check, fresh("mysoulbot-tavern-", server_host="127.0.0.1"))

    # 越界 id 不许建目录
    from core.storage_manager import PathSafetyError, StorageManager

    storage = StorageManager(roots[-1])
    try:
        storage.user_dir("../run")
        check.ok("服务层拒绝越界 id", False, "竟然通过了")
    except PathSafetyError:
        check.ok("服务层拒绝越界 id", True)

    server.shutdown()
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
