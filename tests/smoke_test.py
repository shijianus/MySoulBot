"""端到端冒烟测试（不需要真实 API Key）。

内置一个假的 OpenAI 兼容服务：对话接口返回 SSE 流，抽取接口返回固定事实。
用它在离网环境下验证全链路：模板初始化 / 流式回复 / 非阻塞记忆落盘 / 去重 /
上下文恢复 / 分层 Prompt / 路径防护 / 错误翻译 / 抽取开关。

运行：
    .venv/bin/python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

TODAY = "2026-10-03"
FACT_LINE_RE = re.compile(r"^- \[\d{4}-\d{2}-\d{2}\] ", re.M)
STATE = {"extract_calls": 0, "chat_calls": 0, "extract_empty_once": False}
LAST_CHAT_PARAMS: dict[str, object] = {}


class FakeHandler(BaseHTTPRequestHandler):
    """最小可用的 OpenAI 兼容端点：/chat/completions 支持 stream 与非 stream。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def do_POST(self) -> None:  # noqa: N802
        payload = self._body()
        wants_stream = bool(payload.get("stream"))
        if "记忆抽取器" in json.dumps(payload, ensure_ascii=False):
            STATE["extract_calls"] += 1
            if "触发抽取空正文" in json.dumps(payload, ensure_ascii=False) and not STATE["extract_empty_once"]:
                STATE["extract_empty_once"] = True  # 第一次回空正文，验证抽取侧重试
                self._reply(
                    {"choices": [{"message": {"role": "assistant", "content": ""}}]},
                    stream=False,
                )
                return
            if "慢抽取" in json.dumps(payload, ensure_ascii=False):
                time.sleep(0.6)  # 制造在途窗口，用于验证 backlog/wait_idle 的竞态修复
                # 同时模拟真实网关上出现过的残缺括号，验证格式修复分支
                self._reply(
                    {"choices": [{"message": {"role": "assistant",
                                              "content": f"-{TODAY}] 用户养了一只叫花卷的猫"}}]},
                    stream=False,
                )
                return
            content = (
                f"- [{TODAY}] 用户习惯在深夜十一点之后聊天\n"
                f"- [{TODAY}] 用户不喜欢被连续追问细节\n"
                "- 这一行格式不合规范应当被丢弃\n"
                "NONE 混在里面也必须被忽略\n"
            )
            self._reply(
                {"choices": [{"message": {"role": "assistant", "content": content}}]},
                stream=False,
            )
            return

        STATE["chat_calls"] += 1
        LAST_CHAT_PARAMS.clear()
        LAST_CHAT_PARAMS.update(
            {
                "temperature": payload.get("temperature"),
                "max_tokens": payload.get("max_tokens"),
                "frequency_penalty": payload.get("frequency_penalty"),
                "stream": wants_stream,
                "system_chars": sum(
                    len(str(m.get("content", ""))) for m in payload.get("messages", [])
                    if m.get("role") == "system"
                ),
                "system_head": next(
                    (str(m.get("content", ""))[:40] for m in payload.get("messages", [])
                     if m.get("role") == "system"),
                    "",
                ),
            }
        )
        pieces = ["（把", "台灯拧", "暗了一", "档）\n这么", "晚还", "没睡。"]
        last_user = next(
            (str(m.get("content", "")) for m in reversed(payload.get("messages", []))
             if m.get("role") == "user"),
            "",
        )
        if "触发空正文" in last_user:  # 模拟推理模型把 max_tokens 全耗在思考上
            pieces = []
        if not wants_stream:
            self._reply(
                {"choices": [{"message": {"role": "assistant", "content": "".join(pieces)}}]},
                stream=False,
            )
            return
        self._reply({"choices": pieces}, stream=True)

    def _reply(self, obj: dict, *, stream: bool) -> None:
        try:
            self._write_reply(obj, stream=stream)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中途挂断（互斥测试会这样做），不是引擎缺陷，安静收掉
            pass

    def _write_reply(self, obj: dict, *, stream: bool) -> None:
        if not stream:
            data = json.dumps({"model": "fake", **obj}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in obj["choices"]:
            chunk = json.dumps({"choices": [{"delta": {"content": piece}, "index": 0}]}).encode()
            body = b"data: " + chunk + b"\n\n"
            self.wfile.write(hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.count = 0

    def ok(self, label: str, condition: bool, detail: object = "") -> None:
        self.count += 1
        print(f"[{'PASS' if condition else 'FAIL'}] {label}"
              + (f" :: {detail}" if not condition and detail else ""))
        if not condition:
            self.failures.append(label)


async def drain(stream: AsyncGenerator[str, None]) -> str:
    parts = [chunk async for chunk in stream]
    await stream.aclose()
    return "".join(parts)


async def _persona_checks(
    settings: Any, storage: StorageManager, extractor: MemoryExtractor, check: Checker
) -> None:
    """预设人格库 + 酒馆卡导入 + 人格切换。"""
    from core.bot import BotError as _BotError
    from core.bot import MySoulBot
    from core.card_loader import (
        CardError,
        PersonaLibrary,
        PresetError,
        compile_soul,
        load_card_file,
        parse_card,
        slugify,
        validate_gen_config,
    )
    from core.prompt_builder import PromptBuilder

    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    lib = PersonaLibrary(settings)  # type: ignore[arg-type]
    user = "persona_user"

    # ---------- 预设库 ----------
    slugs = [p.slug for p in lib.list()]
    for wanted in ("default", "butler_kane", "hacker_echo", "witch_morgana"):
        check.ok(f"预设库含 {wanted}", wanted in slugs, str(slugs))
    kane = lib.get("butler_kane")
    check.ok("预设带 first_mes", bool(kane.first_mes) and "银托盘" in kane.first_mes, kane.first_mes[:40])
    check.ok("预设带特征配置", kane.config.get("temperature") == 0.62, str(kane.config))
    check.ok("内置预设排序稳定", slugs[:4] == ["default", "butler_kane", "hacker_echo", "witch_morgana"], str(slugs))
    check.ok("SOUL 正文可读且非空", len(kane.soul_text()) > 800)
    try:
        lib.get("nope")
        check.ok("未知人格报错", False, "没报错")
    except PresetError:
        check.ok("未知人格报错", True)

    # ---------- 卡解析 ----------
    v2 = load_card_file(fixtures / "vesper_v2.json")
    check.ok("V2 卡识别 spec", v2.spec == "chara_card_v2" and v2.spec_version == "2.0", f"{v2.spec}/{v2.spec_version}")
    check.ok("V2 六个字段全部解析", all([v2.name, v2.description, v2.personality, v2.scenario,
                                        v2.first_mes, v2.mes_example]), str([v2.name, len(v2.description)]))
    check.ok("宏 {{user}} 已替换", "{{user}}" not in v2.description and "你是她在钟塔下捡到的" in v2.description,
             v2.description[-40:])
    check.ok("宏 {{char}} 在示例台词中替换", "{{char}}" not in v2.mes_example and "维斯珀:" in v2.mes_example.replace("：", ":"))
    check.ok("<START> 标记被清理", "<START>" not in v2.mes_example.upper(), v2.mes_example[:60])
    check.ok("备选开场保留 2 条", len(v2.alternate_greetings) == 2, str(len(v2.alternate_greetings)))
    check.ok("lorebook 被识别并告警", v2.lorebook_entries == 2
             and any("lorebook" in w for w in v2.warnings), str(v2.warnings))

    v3 = load_card_file(fixtures / "liese_v3.json")
    check.ok("V3 卡识别 spec_version", v3.spec == "chara_card_v3" and v3.spec_version == "3.0")
    check.ok("V3 无 lorebook 不误报", v3.lorebook_entries == 0, str(v3.warnings))

    bare = parse_card({"name": "裸卡", "description": "没有 spec 字段的历史导出格式"})
    check.ok("裸格式（无 spec）可解析", bare.name == "裸卡" and bare.spec.startswith("bare"), bare.spec)
    wrapped = parse_card({"chara": {"name": "包裹卡", "description": "旧导出"}})
    check.ok("chara 包裹格式可解析", wrapped.name == "包裹卡")

    for label, payload in (
        ("缺 name", {"description": "x"}),
        ("顶层非对象", None),
        ("空卡", {}),
    ):
        try:
            parse_card(payload)  # type: ignore[arg-type]
            check.ok(f"{label} 被拒", False, "竟然通过")
        except (CardError, AttributeError):
            check.ok(f"{label} 被拒", True)

    bad_dir = settings.storage_dir / "bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "notjson.json").write_text('{"data": {"name": "x"', encoding="utf-8")
    (bad_dir / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    (bad_dir / "nameless.json").write_text('{"spec":"chara_card_v2","data":{"description":"无名字"}}', encoding="utf-8")
    for label, target, needle in (
        ("坏 JSON 被拒", bad_dir / "notjson.json", "JSON 解析失败"),
        ("PNG 卡被拒并提示导出 JSON", bad_dir / "fake.png", "导出"),
        ("缺 name 被拒", bad_dir / "nameless.json", "name"),
        ("不存在的路径被拒", bad_dir / "ghost.json", "不存在"),
    ):
        try:
            load_card_file(target)
            check.ok(label, False, "竟然通过")
        except CardError as exc:
            check.ok(label, needle in str(exc), str(exc))

    # ---------- 编译映射 ----------
    soul = compile_soul(v2, slug="vesper")
    check.ok("编译产物含人格标题", soul.startswith("# SOUL · 人格内核 · 维斯珀"))
    for needle in ("设定原文", "性格底色", "说话的方式", "台词参照", "场景与处境", "开场", "引擎底线"):
        check.ok(f"分层含「{needle}」", needle in soul)
    check.ok("自动补齐反客服腔禁例", "客服腔" in soul and "首先/其次/综上所述" in soul)
    check.ok("自动补齐不代对方发言底线", "不代替对方发言" in soul)
    check.ok("章节编号无「七·五」这类缺陷", "七·五" not in soul)
    check.ok("卡面 creator_notes 进入引言", "适合陪伴向" in soul.splitlines()[2])
    check.ok("编译后无残留宏", "{{" not in soul, [ln for ln in soul.splitlines() if "{{" in ln])

    check.ok("slug：ASCII 名直转", slugify("Echo Bot") == "echo_bot", slugify("Echo Bot"))
    check.ok("slug：中文名走稳定哈希", slugify("维斯珀") == slugify("维斯珀") and slugify("维斯珀").startswith("card-"),
             slugify("维斯珀"))
    check.ok("slug：不同中文名不撞车", slugify("维斯珀") != slugify("莉泽"))
    check.ok("参数白名单生效", validate_gen_config({"temperature": 0.5, "nonsense": 9}) == {"temperature": 0.5})
    check.ok("越界参数被夹取", validate_gen_config({"temperature": 9}) == {"temperature": 2.0})

    # ---------- 导入落库 ----------
    imported = lib.import_card(fixtures / "vesper_v2.json", slug="vesper")
    check.ok("导入生成预设", imported.slug == "vesper" and imported.source == "tavern")
    check.ok("导入目录含三件套", all((lib.root / "vesper" / n).is_file()
                                    for n in ("SOUL.md", "preset.json", "card.json")))
    check.ok("原始卡可回导出", json.loads((lib.root / "vesper" / "card.json").read_text(encoding="utf-8"))["data"]["name"] == "维斯珀")
    check.ok("导入告警透传给调用方", any("lorebook" in w for w in imported.warnings), str(imported.warnings))
    try:
        lib.import_card(fixtures / "vesper_v2.json", slug="vesper")
        check.ok("重复导入被拒", False, "竟然覆盖")
    except PresetError:
        check.ok("重复导入被拒", True)
    lib.import_card(fixtures / "vesper_v2.json", slug="vesper", force=True)
    check.ok("force 允许覆盖", (lib.root / "vesper" / "SOUL.md").is_file())
    try:
        lib.import_card(fixtures / "liese_v3.json", slug="../evil")
        check.ok("非法 slug 被拒", False, "竟然通过")
    except PresetError:
        check.ok("非法 slug 被拒", True)
    try:
        lib.delete("butler_kane")
        check.ok("内置预设不可删除", False, "竟然删了")
    except PresetError:
        check.ok("内置预设不可删除", True)

    # ---------- 切换与状态衔接 ----------
    bot = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor, lib)
    await bot.open_session(user)
    original_soul = await storage.read_doc(user, "SOUL")
    await storage.append_facts(user, ["用户下周要去成都出差"])
    before_facts = await storage.read_facts(user)

    applied = await bot.apply_persona(user, kane)
    after_soul = await storage.read_doc(user, "SOUL")
    check.ok("切换替换 SOUL.md", after_soul != original_soul and "凯恩" in after_soul)
    check.ok("切换前 SOUL 已备份", applied.backup_path is not None and applied.backup_path.is_file()
             and original_soul in applied.backup_path.read_text(encoding="utf-8"))
    check.ok("记忆跨人格保留", await storage.read_facts(user) == before_facts)
    check.ok("persona.json 记录来源", (await storage.read_persona_meta(user))["slug"] == "butler_kane")
    check.ok("发出 first_mes 作为开场", applied.greeting.startswith("（把银托盘"))
    check.ok("开场白进入上下文", bot.session(user).history[-1]["content"].startswith("（把银托盘"))
    check.ok("开场白写入日志", bool(bot.storage.logs_dir(user).glob("*.jsonl")))
    check.ok("人格级参数进入会话", bot.session(user).gen_params.get("temperature") == 0.62)

    await drain(bot.stream_reply(user, "今晚有点睡不着"))
    check.ok("人格参数真的发到接口", LAST_CHAT_PARAMS.get("temperature") == 0.62, str(LAST_CHAT_PARAMS))
    check.ok("max_tokens 也被覆盖", LAST_CHAT_PARAMS.get("max_tokens") == 800, str(LAST_CHAT_PARAMS))

    reopened = await bot.open_session(user)
    check.ok("重开会话恢复人格", reopened.persona_slug == "butler_kane"
             and reopened.gen_params.get("temperature") == 0.62, str(reopened.gen_params))

    kept = await bot.apply_persona(user, lib.get("witch_morgana"), keep_history=True)
    check.ok("keep 模式保留上下文", len(kept.greeting) == 0 and len(bot.session(user).history) >= 3,
             str(bot.session(user).history[-1]))
    check.ok("keep 模式仍更新人格", bot.session(user).persona_slug == "witch_morgana")

    reset = await bot.apply_persona(user, lib.get("hacker_echo"))
    check.ok("默认切换重置上下文且重发开场", reset.history_reset
             and len(bot.session(user).history) == 1 and reset.greeting.startswith("（没抬头"),
             str(bot.session(user).history))
    check.ok("previous_slug 正确回传", reset.previous_slug == "witch_morgana", reset.previous_slug)

    await drain(bot.stream_reply(user, "这个报错什么意思"))
    check.ok("Echo 的 max_tokens 生效", LAST_CHAT_PARAMS.get("max_tokens") == 800, str(LAST_CHAT_PARAMS))

    back = await bot.apply_persona(user, lib.get("default"))
    check.ok("可切回默认模板", "夜汐" in await storage.read_doc(user, "SOUL") and back.greeting == "",
             (await storage.read_doc(user, "SOUL"))[:40])
    check.ok("SOUL 备份累积且受限", len(list((storage.backups_dir(user)).glob("SOUL-*.md"))) <= 5)

    try:
        await bot.apply_persona("persona_user", _EmptyPreset())  # type: ignore[arg-type]
        check.ok("空 SOUL 的人格拒绝应用", False, "竟然通过")
    except _BotError:
        check.ok("空 SOUL 的人格拒绝应用", True)

    lib.delete("vesper")
    check.ok("删除后从列表消失", "vesper" not in [p.slug for p in lib.list()])


class _EmptyPreset:
    slug = "empty"
    name = "空卡"
    title = ""
    source = "tavern"
    first_mes = ""
    config: dict = {}
    greetings: list[str] = []
    warnings: list[str] = []

    def soul_text(self) -> str:
        return "   "



async def main() -> int:  # noqa: C901 - 顺序执行的一组独立场景
    server, base_url = serve()
    root = tempfile.mkdtemp(prefix="mysoulbot-smoke-")
    shutil.copytree(PROJECT_ROOT / "storage" / "templates", Path(root) / "templates")
    shutil.copytree(PROJECT_ROOT / "storage" / "presets", Path(root) / "presets")
    os.environ.update(
        {
            "BASE_URL": base_url,
            "API_KEY": "fake-key",
            "MODEL": "fake-chat",
            "EXTRACTOR_MODEL": "fake-lite",
            "STORAGE_DIR": root,
            "EXTRACTOR_MAX_FACTS": "5",
            "LOG_LEVEL": "WARNING",
        }
    )

    import config as config_module
    from core.bot import BotError, MySoulBot
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import PathSafetyError, StorageManager

    check = Checker()
    settings = config_module.get_settings()
    storage = StorageManager(settings)
    extractor = MemoryExtractor(settings, storage)
    bot = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor)
    extractor.start()
    user = "alice"

    await bot.open_session(user)
    for doc in ("SOUL", "USER", "MEMORY"):
        path = settings.users_dir / user / f"{doc}.md"
        check.ok(f"模板初始化为 {doc}.md", path.is_file() and path.stat().st_size > 100)
    check.ok("logs 目录已建", (settings.users_dir / user / "logs").is_dir())

    # 任何对话之前：两次组装必须逐字节一致
    prompt, layers = await bot.preview_prompt(user)
    check.ok("两次组装完全一致（确定性）", prompt == (await bot.preview_prompt(user))[0])
    for token, label in (
        ("LAYER 1 · 人格内核", "人格层"),
        ("LAYER 2 · 用户画像", "画像层"),
        ("LAYER 3 · 长期记忆", "记忆层"),
        ("LAYER 4 · 互动准则", "准则层"),
        ("LAYER 5 · 当下语境", "语境层"),
    ):
        check.ok(f"Prompt 含 {label}", token in prompt)
    check.ok("Prompt 含禁止代用户发言", "绝不代替用户发言" in prompt)
    check.ok("Prompt 含人格连贯约束", "保持人格" in prompt)
    check.ok("Prompt 含硬约束原文", "你必须始终遵守以下硬约束" in prompt)
    check.ok("Prompt 含人格内核正文", "夜汐" in prompt)
    check.ok("记忆为空时显式声明", "尚无确认的关键事实" in prompt)
    check.ok("分层报告可读", "system prompt 合计" in layers.render_report(), layers.render_report())

    reply = await drain(bot.stream_reply(user, "这么晚还在，我明天要出差。"))
    check.ok("对话接口被调用一次", STATE["chat_calls"] == 1, f"calls={STATE['chat_calls']}")
    check.ok("流式分片完整拼接", reply.endswith("这么晚还没睡。"), repr(reply))

    left = await extractor.wait_idle(20.0)
    check.ok("后台抽取已排空", left == 0, f"backlog={left}")
    memory = (settings.users_dir / user / "MEMORY.md").read_text(encoding="utf-8")
    facts = re.findall(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$", memory, re.M)
    check.ok("MEMORY.md 落入 2 条事实", len(facts) == 2, f"{facts}")
    check.ok("日期由引擎统一盖章", all(day == TODAY for day, _ in facts), f"{facts}")
    check.ok("格式外内容被丢弃", "格式不合规范" not in memory and "NONE" not in memory, memory)
    check.ok("空占位行被清理", "还没有记录" not in memory)
    check.ok("文件结构完好", memory.startswith("# MEMORY") and "## 事实" in memory)
    check.ok("抽取统计正确", extractor.stats["written"] == 2, json.dumps(extractor.stats))
    check.ok("抽取接口被调用", STATE["extract_calls"] == 1, f"calls={STATE['extract_calls']}")

    prompt_after, _ = await bot.preview_prompt(user)
    check.ok("新记忆进入下一轮 Prompt", "用户习惯在深夜十一点之后聊天" in prompt_after)
    check.ok("记忆小节计数正确", "共 2 条" in prompt_after)

    await drain(bot.stream_reply(user, "我习惯十一点后聊，别老追问我。"))
    left = await extractor.wait_idle(20.0)
    after = (settings.users_dir / user / "MEMORY.md").read_text(encoding="utf-8")
    check.ok("重复事实不再二次写入", len(re.findall(r"^- \[", after, re.M)) == 2, after)
    check.ok("队列仍为空", left == 0)

    logs = sorted((settings.users_dir / user / "logs").glob("*.jsonl"))
    check.ok("对话日志按天落盘", bool(logs), str(logs))
    records = await storage.read_recent_transcript(user, 12)
    check.ok("日志可回读为上下文", len(records) == 4, f"{len(records)}")
    restored = await MySoulBot(
        settings, storage, PromptBuilder(settings, storage), extractor
    ).open_session(user)
    check.ok("重开会话恢复上下文", len(restored.history) == 4, f"{len(restored.history)}")

    written = await storage.append_facts(user, ["用户喝咖啡会失眠"])
    check.ok("手工追加事实", written == ["用户喝咖啡会失眠"], str(written))
    again = await storage.append_facts(user, ["用户 喝咖啡 会失眠。"])
    check.ok("归一化后重复被拒", again == [], str(again))

    for bad in ("../evil", "a/b", "..", "x" * 65, "ali ice", "%2e%2e%2fetc"):
        try:
            storage.user_dir(bad)
            check.ok(f"拒绝非法 user_id {bad!r}", False, "竟然通过了校验")
        except PathSafetyError:
            check.ok(f"拒绝非法 user_id {bad!r}", True)

    config_module.get_settings.cache_clear()
    os.environ["BASE_URL"] = "http://127.0.0.1:1/v1"
    dead_settings = config_module.get_settings()
    dead_storage = StorageManager(dead_settings)
    dead_bot = MySoulBot(
        dead_settings, dead_storage, PromptBuilder(dead_settings, dead_storage), extractor
    )
    await dead_bot.open_session("dave")
    try:
        await drain(dead_bot.stream_reply("dave", "在吗"))
        check.ok("接口不可达时抛 BotError", False, "没有报错")
    except BotError as exc:
        check.ok("接口不可达时抛 BotError", "无法连接" in exc.message, exc.message)
        check.ok("错误附带可操作提示", bool(exc.hint), exc.hint)

    config_module.get_settings.cache_clear()
    os.environ["BASE_URL"] = base_url
    os.environ["EXTRACTOR_ENABLED"] = "false"
    off_settings = config_module.get_settings()
    off_storage = StorageManager(off_settings)
    off_extractor = MemoryExtractor(off_settings, off_storage)
    off_bot = MySoulBot(
        off_settings, off_storage, PromptBuilder(off_settings, off_storage), off_extractor
    )
    off_extractor.start()
    text = await drain(off_bot.stream_reply("carol", "我一般十一点后才睡"))
    await asyncio.sleep(0.5)
    carol_memory = (off_settings.users_dir / "carol" / "MEMORY.md").read_text(encoding="utf-8")
    check.ok(
        "抽取关闭后不写记忆",
        not re.search(r"^- \[\d{4}-\d{2}-\d{2}\]", carol_memory, re.M),
        carol_memory,
    )
    check.ok("抽取关闭后对话照常", "台灯" in text)

    concurrent_stream = bot.stream_reply("erin", "第一句")
    first_chunk = await concurrent_stream.__anext__()
    blocked = False
    try:
        await drain(bot.stream_reply("erin", "第二句"))
    except BotError as exc:
        blocked = "生成中" in exc.message
    check.ok("同一用户并发回复被互斥拒绝", blocked and bool(first_chunk), f"{blocked} {first_chunk!r}")
    await concurrent_stream.aclose()
    resumed = await drain(bot.stream_reply("erin", "第三句"))
    check.ok("中断后 busy 标记已释放", "台灯" in resumed, repr(resumed))

    try:
        await drain(bot.stream_reply("erin", "触发空正文"))
        check.ok("模型返回空正文时抛 BotError", False, "没有报错")
    except BotError as exc:
        check.ok("模型返回空正文时抛 BotError", "可见内容" in exc.message, exc.message)
        check.ok("空正文错误给出可操作提示", "EXTRACTOR_MAX_TOKENS" in exc.hint or "MAX_TOKENS" in exc.hint, exc.hint)

    # --- 在途抽取竞态：worker 取走任务后 qsize 归零，积压与等待都必须看到在途请求 ---
    race = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor)
    await race.open_session("racer")
    await drain(race.stream_reply("racer", "慢抽取 我养了一只叫花卷的猫"))
    await asyncio.sleep(0.05)
    check.ok("在途抽取被计入 backlog", extractor.backlog >= 1, f"backlog={extractor.backlog}")
    left = await extractor.wait_idle(5.0)
    racer_memory = (settings.users_dir / "racer" / "MEMORY.md").read_text(encoding="utf-8")
    check.ok("wait_idle 等到真正落盘才返回", left == 0 and FACT_LINE_RE.search(racer_memory) is not None,
             f"left={left} memory={racer_memory[-120:]!r}")
    check.ok(
        "残缺括号 `-2026-10-03] 事实` 被修复落盘",
        "- [2026-10-03] 用户养了一只叫花卷的猫" in racer_memory,
        racer_memory[-160:],
    )
    check.ok("残缺行未被计入丢弃导致事实丢失", "叫花卷的猫" in racer_memory, racer_memory[-160:])

    # --- 抽取正文为空时自动重试一次（真实推理模型上偶发） ---
    calls_before = STATE["extract_calls"]
    retry_bot = MySoulBot(settings, storage, PromptBuilder(settings, storage), extractor)
    await retry_bot.open_session("retrier")
    await drain(retry_bot.stream_reply("retrier", "触发抽取空正文 我妹妹叫小花"))
    await extractor.wait_idle(20.0)
    check.ok(
        "抽取空正文后自动重试一次",
        STATE["extract_calls"] - calls_before == 2,
        f"新增调用 {STATE['extract_calls'] - calls_before}",
    )
    retrier_memory = (settings.users_dir / "retrier" / "MEMORY.md").read_text(encoding="utf-8")
    check.ok("重试后事实正常落盘", FACT_LINE_RE.search(retrier_memory) is not None, retrier_memory[-140:])

    await _persona_checks(settings, storage, extractor, check)

    await extractor.aclose(timeout=10.0)
    await bot.aclose()
    server.shutdown()
    shutil.rmtree(root, ignore_errors=True)

    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
