"""阶段三测试：视觉摄取、多模态进模型、只读沉浸面板、守护排空。

全部离线：假 OpenAI 端点 + 真 socket 服务 + 临时 storage 目录，不碰真实模型与真实记忆。

覆盖五块：

1. **视觉摄取**：本地路径 / base64 / 链接三条来路，魔数与体积闸门，中文句子里摘图不误伤。
2. **多模态进模型**：图片以原生分段真的送进去；语境层禁止报菜名；关掉视觉或接口不认时
   诚实退化——角色承认看不了，绝不编造画面。
3. **工具层**：`see_image` 下单后图既落盘又进眼睛；`image_gen` 附带本地产物的静态查看链接。
4. **Web 面板**：/panel 静态页与 /api/state|timeline|docs 只读 JSON；熟络度在页面上
   是一根不能拖的条——所有写方法一律 405，且温度块逐字节不动。
5. **守护**：优雅停机先停接单、等在途回合说完、再等后台记忆落盘。

运行：
    .venv/bin/python tests/panel_web_test.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

REPLY_RE = "（盯着看）"
STAMP = dt.datetime.now().astimezone().replace(hour=22, minute=30, second=0, microsecond=0)
NOW = dt.datetime.now().astimezone()

# 假端点的可观察状态
SEEN: list[dict[str, Any]] = []
MODE: dict[str, Any] = {"reject_vision": False, "directive": "", "extract_sleep": 0.0, "extract_lines": ["NONE"]}


def png_bytes(seed: str = "cat", width: int = 24, height: int = 16) -> bytes:
    from core.tools.media import _placeholder_png

    return _placeholder_png(seed, width, height)


class FakeHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容假端点：能流式说话、能装得下多模态、能拒收图、能慢抽记忆。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        SEEN.append(payload)
        dumped = json.dumps(payload, ensure_ascii=False)
        if "记忆抽取器" in dumped:
            if MODE["extract_sleep"]:
                time.sleep(MODE["extract_sleep"])
            lines = "\n".join(MODE["extract_lines"])
            self._json({"choices": [{"message": {"role": "assistant", "content": lines}}]})
            return
        has_image = any(isinstance(m.get("content"), list) for m in payload.get("messages") or [])
        if has_image and MODE["reject_vision"]:
            body = b'{"error":{"message":"image not supported"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        pieces = [REPLY_RE, "这猫真丑。"]
        if MODE["directive"] and not any("内部结果" in str(m.get("content", "")) for m in payload.get("messages") or []):
            pieces = [f"（伸手）\n{MODE['directive']}\n"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for piece in pieces:
                frame = b"data: " + json.dumps(
                    {"choices": [{"delta": {"content": piece}, "index": 0}]}
                ).encode() + b"\n\n"
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
        "model": "fake-vision",
        "extractor_model": "fake-lite",
        "storage_dir": root,
        "log_level": "WARNING",
        "tools_enabled": True,
        "tool_native_calling": False,
        "extractor_enabled": False,
        "image_provider": "stub",
        "user_timezone": "",
        "default_user_id": "guest",
        "panel_enabled": True,
    }
    values.update(overrides)
    # _env_file=None：测试只认自己写死的那套值。开发机的 .env 一开 ONEBOT_ENABLED，
    # 继承下来的话这里会去抢 11556，测试就变成在测「这台机器现在怎么配的」。
    return Settings(_env_file=None, **values)


async def build_bot(settings: Any) -> Any:
    from core.bot import MySoulBot
    from core.clawd_soul import ClawdSoul
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    await clawd.ensure()
    return MySoulBot(settings, storage, PromptBuilder(settings, storage, clawd),
                     MemoryExtractor(settings, storage), clawd=clawd)


def http_get(url: str) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), dict(exc.headers or {})


def http_send(url: str, method: str, payload: dict[str, Any] | None = None) -> tuple[int, str]:
    body = json.dumps(payload or {}).encode() if payload is not None else b"{}"
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


# ================================================================ 1. 视觉摄取
def ingest_checks(check: Checker, settings: Any) -> None:
    import core.vision as vision
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    root = settings.storage_dir
    user = "ingest_user"

    real = root / "cat.png"
    real.write_bytes(png_bytes("cat"))
    jpeg = root / "photo.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)

    check.ok("png 认得出", vision.sniff(real.read_bytes()) == "image/png")
    check.ok("jpeg 认得出", vision.sniff(jpeg.read_bytes()) == "image/jpeg")
    check.ok("伪装成 png 的文本认不出", vision.sniff(b"<html>not an image</html>") == "")
    check.ok("gif 认得出", vision.sniff(b"GIF89a....") == "image/gif")
    check.ok("bmp 不在清单里", vision.sniff(b"BM\x00\x00") == "")

    ref = vision.ingest(str(real), settings, storage, user)
    check.ok("本地路径收成 ImageRef", ref.media_type == "image/png" and ref.path.is_file(), str(ref))
    check.ok("收下的图落进 artifacts", ref.path.parent.name == "artifacts", str(ref.path.parent))
    check.ok("来源被记下来", ref.origin == "文件", ref.origin)
    check.ok("展示名仍是他的文件名", ref.name == "cat.png", ref.name)

    import base64

    blob = "data:image/png;base64," + base64.b64encode(png_bytes("pasted")).decode()
    pasted = vision.ingest(blob, settings, storage, user)
    check.ok("base64 粘贴能收", pasted.path.is_file() and pasted.origin == "粘贴", pasted.origin)
    check.ok("粘贴的图也有扩展名", pasted.path.suffix == ".png", pasted.path.name)

    broken = root / "fake.png"
    broken.write_bytes(b"just some text pretending")
    try:
        vision.ingest(str(broken), settings, storage, user)
        check.ok("后缀骗不过魔数校验", False, "竟然收下了")
    except vision.VisionError as exc:
        check.ok("后缀骗不过魔数校验", "不是我能看的图" in str(exc), str(exc))
    try:
        vision.ingest(str(root / "nope.png"), settings, storage, user)
        check.ok("不存在的路径被拒", False, "竟然收下了")
    except vision.VisionError as exc:
        check.ok("不存在的路径被拒", "没找到" in str(exc), str(exc))
    try:
        vision.ingest("data:image/png;base64:@@@not-base64@@@", settings, storage, user)
        check.ok("坏 base64 被拒", False, "竟然收下了")
    except vision.VisionError as exc:
        check.ok("坏 base64 被拒", "base64" in str(exc), str(exc))

    settings.vision_max_bytes = 64
    try:
        vision.ingest(str(real), settings, storage, user)
        check.ok("超过体积闸门就拒收", False, "竟然收下了")
    except vision.VisionError as exc:
        check.ok("超过体积闸门就拒收", "太大" in str(exc), str(exc))
    settings.vision_max_bytes = 4_000_000

    # 从中文句子里摘图
    plain, found = vision.find_sources(f"看看这只猫 {real}")
    check.ok("句中路径被摘出来", found == [str(real)] and plain == "看看这只猫", f"{plain} {found}")
    plain, found = vision.find_sources("你看它昨天是不是很开心")
    check.ok("没有图就不硬摘", plain == "你看它昨天是不是很开心" and not found, str(found))
    plain, found = vision.find_sources("我在想 photo.png 这名字土不土")
    check.ok("不存在的裸文件名不当图", not found, str(found))
    plain, found = vision.find_sources(f"先看 {blob} 再看别的")
    check.ok("base64 串被摘走", len(found) == 1 and found[0].startswith("data:image/png"), str(found)[:60])
    check.ok("摘完剩下的还是他说的话", plain == "先看 再看别的", plain)
    plain, found = vision.find_sources("https://cdn.example.com/a/b.jpeg?x=1 这张看看")
    check.ok("图片链接被摘走", found == ["https://cdn.example.com/a/b.jpeg?x=1"], str(found))

    many = [vision.ingest(str(jpeg), settings, storage, user) for _ in range(3)]
    kept, dropped = vision.trim(many, 2)
    check.ok("张数闸门裁得掉多余的", len(kept) == 2 and dropped == 1, f"{len(kept)}/{dropped}")

    parts = vision.content_parts([ref])
    check.ok("分段是 image_url 形状", parts[0]["type"] == "image_url"
             and parts[0]["image_url"]["url"].startswith("data:image/png;base64,"), str(parts)[:80])
    seen_line = vision.note([ref], seen=True)
    blind_line = vision.note([ref], seen=False)
    check.ok("看得见时禁止报菜名", "严禁报菜名式描述" in seen_line, seen_line[:80])
    check.ok("看得见时先要感觉", "先说它落在你心里是什么感觉" in seen_line)
    check.ok("看不见时把话说死", "你这边现在看不了图" in blind_line, blind_line[:80])
    check.ok("看不见时禁止编", "别猜、别编" in blind_line)
    check.ok("没图就没有这一行", vision.note([], seen=True) == "")

    link = vision.view_url(settings, user, ref.path)
    check.ok("查看链接指到 /media", f"/media/{user}/" in link and link.startswith("http://127.0.0.1:"), link)
    settings.server_host = "0.0.0.0"
    check.ok("0.0.0.0 不进链接", vision.view_url(settings, user, ref.path).startswith("http://127.0.0.1:"),
             vision.view_url(settings, user, ref.path))
    settings.server_host = "127.0.0.1"


# ================================================================ 2. 多模态进模型
async def multimodal_checks(check: Checker, settings: Any) -> None:
    from core.storage_manager import StorageManager
    from core.vision import find_sources

    storage = StorageManager(settings)
    root = settings.storage_dir
    picture = root / "kitten.png"
    picture.write_bytes(png_bytes("kitten"))
    user = "mm_user"
    bot = await build_bot(settings)
    await bot.open_session(user)

    async def say(target: str, text: str, uid: str = user) -> str:
        """照 CLI 的真实流程走：边界先把图摘出来，引擎只收显式的 images。"""
        words, shots = find_sources(text)
        stream = bot.stream_reply(target, words, today=STAMP.date(), images=shots)
        pieces = [piece async for piece in stream]
        await stream.aclose()
        return "".join(pieces)

    SEEN.clear()
    reply = await say(user, f"看看这只 {picture}")
    sent = SEEN[-1]
    last = sent["messages"][-1]
    check.ok("回复仍是角色的话", reply.startswith(REPLY_RE), repr(reply))
    check.ok("最后一条是 user 分段", last["role"] == "user" and isinstance(last["content"], list), str(last)[:80])
    kinds = [part["type"] for part in last["content"]]
    check.ok("图片分段真的送出去了", kinds == ["image_url", "text"], str(kinds))
    check.ok("送出去的是 data url", last["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
    check.ok("他说的话还在", last["content"][1]["text"] == "看看这只", str(last["content"][1])[:60])
    check.ok("路径不留在话里", "kitten.png" not in last["content"][1]["text"], str(last["content"][1])[:80])
    system = next(m["content"] for m in sent["messages"] if m["role"] == "system")
    check.ok("语境层禁止报菜名", "严禁报菜名式描述" in system, system[-260:])
    check.ok("语境层说了图从哪来", "从本地递过来的" in system, system[-260:])
    check.ok("看得见时不说看不了", "你这边现在看不了图" not in system)

    lines = sorted((storage.logs_dir(user)).glob("*.jsonl"))
    body = lines[-1].read_text(encoding="utf-8")
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    marks = [r.get("attachments") for r in records if r.get("attachments")]
    check.ok("日志记下附件名", marks and marks[0] == ["kitten.png"], str(marks))
    check.ok("日志里没有 base64", "base64" not in body and "image_url" not in body)

    # 张数闸门
    second = root / "kitten2.png"
    second.write_bytes(png_bytes("kitten2"))
    third = root / "kitten3.png"
    third.write_bytes(png_bytes("kitten3"))
    SEEN.clear()
    await say(user, f"{picture} {second} {third}")
    last = SEEN[-1]["messages"][-1]
    images = [p for p in last["content"] if p["type"] == "image_url"]
    check.ok("一轮最多两张", len(images) == settings.vision_max_images, f"{len(images)} vs {settings.vision_max_images}")
    system = next(m["content"] for m in SEEN[-1]["messages"] if m["role"] == "system")
    check.ok("超出的那张被明说", "多出来的 1 张我没接" in system, system[-300:])

    # 收不下的图不静默吞掉
    bogus = root / "bogus.png"
    bogus.write_bytes(b"pretending hard")
    SEEN.clear()
    await say(user, f"看这张 {bogus}")
    system = next(m["content"] for m in SEEN[-1]["messages"] if m["role"] == "system")
    check.ok("收不下会明说", "收不下" in system or "不是我能看的图" in system, system[-300:])
    check.ok("坏图不挂分段", isinstance(SEEN[-1]["messages"][-1]["content"], str),
             str(SEEN[-1]["messages"][-1])[:60])

    # 关掉视觉：不挂分段，但必须承认收到过图
    settings.vision_enabled = False
    SEEN.clear()
    await say(user, f"再看一下这只 {picture}")
    sent = SEEN[-1]
    check.ok("关掉视觉后只有字", isinstance(sent["messages"][-1]["content"], str), str(sent["messages"][-1])[:70])
    system = next(m["content"] for m in sent["messages"] if m["role"] == "system")
    check.ok("关掉视觉后承认看不了", "你这边现在看不了图" in system, system[-300:])
    check.ok("关掉视觉仍知道有图", "给你看了 1 张图" in system)
    settings.vision_enabled = True

    # 接口不认多模态：退回而不是崩
    bot2 = await build_bot(settings)
    await bot2.open_session("mm_degrade")
    MODE["reject_vision"] = True
    SEEN.clear()
    stream = bot2.stream_reply("mm_degrade", "看这只", today=STAMP.date(), images=[str(picture)])
    degraded = "".join([piece async for piece in stream])
    await stream.aclose()
    MODE["reject_vision"] = False
    check.ok("接口拒图不甩错给界面", degraded.startswith(REPLY_RE), repr(degraded))
    check.ok("退回后重试过一次", len(SEEN) == 2, str(len(SEEN)))
    check.ok("第二次请求不带图", all(isinstance(m.get("content"), str) for m in SEEN[-1]["messages"]))
    system = next(m["content"] for m in SEEN[-1]["messages"] if m["role"] == "system")
    check.ok("退回后说的是看不了", "你这边现在看不了图" in system, system[-300:])
    check.ok("该会话记住接口看不了图", bot2.session("mm_degrade").vision_mode == "off")
    MODE["reject_vision"] = True
    SEEN.clear()
    stream = bot2.stream_reply("mm_degrade", "又看一次", today=STAMP.date(), images=[str(picture)])
    again = "".join([piece async for piece in stream])
    await stream.aclose()
    MODE["reject_vision"] = False
    check.ok("第二次直接不带图，不再试", len(SEEN) == 1, str(len(SEEN)))
    check.ok("仍然正常说话", again.startswith(REPLY_RE), repr(again))

    # 预览不改变任何东西
    before = len(SEEN)
    await bot.preview_prompt(user)
    check.ok("预览不发请求", len(SEEN) == before, f"{before} → {len(SEEN)}")
    await bot.aclose()
    await bot2.aclose()


# ================================================================ 3. 工具层
async def tool_checks(check: Checker, settings: Any) -> None:
    from core.storage_manager import StorageManager
    from core.tools.base import ToolContext
    from core.clawd_soul import ClawdSoul
    from core.tools.registry import ToolRegistry

    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    await clawd.ensure()
    user = "tool_vision"
    await storage.ensure_user(user)
    ctx = ToolContext(settings=settings, storage=storage, user_id=user, clawd=clawd)
    registry = ToolRegistry(ctx)
    check.ok("清单里有看图", "see_image" in registry.names, str(registry.names))
    check.ok("看图的 hint 是人话", any(t.hint and "图" in t.hint for t in registry.tools), str(registry.summary()))
    check.ok("别名 look→see_image", registry.resolve("look").name == "see_image")  # type: ignore[union-attr]
    check.ok("别名 看图→see_image", registry.resolve("看图").name == "see_image")  # type: ignore[union-attr]

    picture = settings.storage_dir / "given.png"
    picture.write_bytes(png_bytes("given"))
    result = await registry.call("see_image", {"source": str(picture)})
    check.ok("看图工具跑成了", result.ok, result.error)
    check.ok("看图产出 ImageRef", [type(r).__name__ for r in result.meta.get("images", [])] == ["ImageRef"],
             str(result.meta)[:120])
    check.ok("看图不报菜名", "严禁报菜名" in result.content, result.content[:80])
    check.ok("产物进了 artifacts", result.artifacts and result.artifacts[0].is_file(), str(result.artifacts))

    bad = await registry.call("see_image", {"source": str(settings.storage_dir / "ghost.png")})
    check.ok("图不存在时失败也是一句人话", not bad.ok and "没找到" in bad.content + bad.error, bad.digest(80))
    empty = await registry.call("see_image", {})
    check.ok("没给来路不硬跑", not empty.ok, empty.digest(60))

    drawn = await registry.call("image_gen", {"prompt": "一只在雨里的猫"})
    check.ok("生图跑成", drawn.ok, drawn.error)
    check.ok("生图给了本地产物", drawn.artifacts and drawn.artifacts[0].is_file(), str(drawn.artifacts))
    check.ok("生图附带可点开的地址", "/media/tool_vision/" in drawn.content, drawn.digest(160))
    check.ok("生图诚实说明占位", "占位" in drawn.content, drawn.digest(160))
    check.ok("生图把图也交给眼睛", bool(drawn.meta.get("images")), str(drawn.meta)[:120])
    link = drawn.meta.get("link", "")
    check.ok("link 与正文一致", link in drawn.content, f"{link} vs {drawn.digest(160)}")

    # 关掉视觉，就不该有这个能力
    settings.vision_enabled = False
    off = ToolRegistry(ToolContext(settings=settings, storage=storage, user_id=user, clawd=clawd))
    check.ok("关掉视觉后不挂看图", "see_image" not in off.names, str(off.names))
    settings.vision_enabled = True

    # 端到端：模型下单看图 → 图片本体真的回流进下一次请求
    bot = await build_bot(settings)
    await bot.open_session("tool_roundtrip")
    SEEN.clear()
    MODE["directive"] = f'⟦tool:see_image source="{picture}"⟧'
    stream = bot.stream_reply("tool_roundtrip", "帮我看看这张图", today=STAMP.date())
    spoken = "".join([piece async for piece in stream])
    await stream.aclose()
    MODE["directive"] = ""
    check.ok("下单那行不外泄", "⟦" not in spoken and "see_image" not in spoken, repr(spoken))
    check.ok("工具往返跑了两次请求", len(SEEN) == 2, str(len(SEEN)))
    second = SEEN[-1]["messages"]
    with_image = [m for m in second if isinstance(m.get("content"), list)]
    check.ok("模型第二轮真的拿到图", bool(with_image)
             and any(p["type"] == "image_url" for m in with_image for p in m["content"]), str(second)[-200:])
    check.ok("图前有一句这是什么的说明", any(p["type"] == "text" and "他" in p["text"]
                                        for m in with_image for p in m["content"]))
    await bot.aclose()


# ================================================================ 4. Web 面板
async def panel_http_checks(check: Checker, settings: Any) -> None:
    from core.rapport import RapportEngine
    from core.server import SoulServer
    from core.storage_manager import StorageManager

    storage = StorageManager(settings)
    user = "panel_user"
    await storage.ensure_user(user)
    await storage.append_facts(user, ["用户最近一直失眠", "用户在写一本小说"], on_date=dt.date(2026, 10, 1))
    await storage.append_dynamics(user, ["他累了就嫌话多，宜短不宜长"], on_date=dt.date(2026, 10, 2))
    await storage.write_state(
        user,
        {
            "mood": {"valence": -0.6, "cause": "他说我在讲道理",
                     "at": (NOW - dt.timedelta(minutes=12)).isoformat(timespec="seconds")},
            "patience": {"left": 0.3, "turns_today": 9, "day": NOW.date().isoformat(),
                         "touched_at": NOW.isoformat(timespec="seconds")},
            "last_seen": (NOW - dt.timedelta(days=4)).isoformat(timespec="seconds"),
            "rapport": {"score": 62.5, "peak": 70.0, "turns": 88,
                        "days": ["2026-09-01", "2026-10-03"], "late_nights": 12, "repairs": 3},
        },
    )
    artifact = storage.artifacts_dir(user)
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "drawn.png").write_bytes(png_bytes("drawn"))
    (artifact / "notes.md").write_text("不该被这个路由端出去的东西", encoding="utf-8")
    (artifact / "sub").mkdir(exist_ok=True)
    (artifact / "sub" / "deep.png").write_bytes(png_bytes("deep"))

    server = SoulServer(settings)
    host, port = await server.start("127.0.0.1", 11777)
    base = f"http://{host}:{port}"
    try:
        status, html, head = await asyncio.to_thread(http_get, f"{base}/panel")
        check.ok("/panel 出静态页", status == 200 and "<!DOCTYPE html>" in html, f"{status}")
        check.ok("面板是 utf-8 HTML", "text/html" in head.get("Content-Type", "") and "charset=utf-8" in head.get("Content-Type", ""), head.get("Content-Type", ""))
        check.ok("页面无外链资源", "http://" not in html.replace("http://127.0.0.1", "") and "cdn." not in html)
        check.ok("页面引用同目录资源", 'href="app.css"' in html and 'src="app.js"' in html)
        check.ok("页面把只读写在脸上", "只读 · 不可调节" in html, html[:120])
        check.ok("页面没有可调温度的控件", 'type="range"' not in html and 'type="number"' not in html and "step=" not in html)
        heat = html.split('<section class="card heat"')[1].split("</section>")[0] if '<section class="card heat"' in html else ""
        check.ok("温度卡里连一个可操作的控件都不长", bool(heat) and not any(
            tag in heat for tag in ("<button", "<input", "<select", "onclick", "onchange", "contenteditable")), heat[:80])
        check.ok("根路径也进面板", (await asyncio.to_thread(http_get, base + "/"))[0] == 200)

        for asset in ("app.css", "app.js"):
            code, text, hd = await asyncio.to_thread(http_get, f"{base}/panel/{asset}")
            check.ok(f"/panel/{asset} 可取", code == 200 and len(text) > 200, f"{code} {len(text)}")
            check.ok(f"{asset} 内容类型正确", hd.get("Content-Type", "").startswith("text/"), hd.get("Content-Type", ""))
        check.ok("面板资源不接野路径", (await asyncio.to_thread(http_get, base + "/panel/../app.js"))[0] in (400, 404))

        status, raw, _ = await asyncio.to_thread(http_get, f"{base}/api/state?user={user}")
        state = json.loads(raw)
        check.ok("/api/state 可用", status == 200 and state["user_id"] == user, raw[:120])
        check.ok("状态带当前人格", "persona" in state and "name" in state["persona"], str(state["persona"]))
        check.ok("状态带熟络度阶段", state["rapport"]["label"] in ("陌生期", "初熟", "熟络期", "深层默契"), str(state["rapport"]))
        check.ok("熟络度显式声明不可编辑", state["rapport"]["editable"] is False)
        check.ok("温度值来自 RELATIONS 的温度块", state["rapport"]["score"] == 0, f"{state['rapport']['score']}")
        check.ok("状态带当下心情", state["mood"]["word"] in ("松", "平", "堵"), str(state["mood"]))
        check.ok("情绪余温带出百分比", 0 <= state["mood"]["residual"] <= 100 and state["mood"]["residual"] >= 60, str(state["mood"]))
        check.ok("心情起因可以说出口", state["mood"]["cause"] == "他说我在讲道理", str(state["mood"]))
        check.ok("状态带精力", state["energy"]["patience"] == 30 and state["energy"]["turns_today"] == 9, str(state["energy"]))
        check.ok("状态带生理节律", state["rhythm"]["slot"] and state["rhythm"]["body"], str(state["rhythm"])[:120])
        check.ok("节律带分寸", "不许" in state["rhythm"]["conduct"] or state["rhythm"]["conduct"], str(state["rhythm"]["conduct"]))
        check.ok("久别重逢看得见", state["rhythm"]["gap"] == "4 天没说话", state["rhythm"]["gap"])
        check.ok("相处账本在", state["memory"]["days_together"] == 2 and state["memory"]["turns"] == 88, str(state["memory"]))
        check.ok("面板不泄露目录", "storage" not in raw and "state.json" not in raw and ".md" not in raw, raw[:200])
        check.ok("面板不泄露凭据", "sk-" not in raw and "api_key" not in raw and "base_url" not in raw)
        check.ok("面板不泄露模型参数", "temperature" not in raw and "max_tokens" not in raw)

        status, raw, _ = await asyncio.to_thread(http_get, f"{base}/api/timeline?user={user}")
        line = json.loads(raw)
        check.ok("/api/timeline 可用", status == 200, raw[:120])
        check.ok("时光机有她眼中的我", "失眠" in line["profile"] or len(line["profile"]) > 10, line["profile"][:60])
        check.ok("事实条目带日期", [f["text"] for f in line["facts"]] == ["用户最近一直失眠", "用户在写一本小说"], str(line["facts"]))
        check.ok("事实日期是 ISO", line["facts"][0]["day"] == "2026-10-01", line["facts"][0]["day"])
        check.ok("相处动态单独一轨", [d["text"] for d in line["dynamics"]] == ["他累了就嫌话多，宜短不宜长"], str(line["dynamics"]))
        check.ok("归档条数被算进来", "archived" in line and line["archived"]["facts"] == 0, str(line["archived"])[:120])
        check.ok("相处活动带每天轮数", isinstance(line["activity"], list), str(line["activity"])[:80])

        status, raw, _ = await asyncio.to_thread(http_get, f"{base}/api/docs/USER?user={user}")
        check.ok("/api/docs/USER 可读", status == 200 and json.loads(raw)["doc"] == "USER", raw[:100])
        for doc in ("MEMORY", "RELATIONS", "SOUL"):
            code, text, _ = await asyncio.to_thread(http_get, f"{base}/api/docs/{doc}?user={user}")
            check.ok(f"/api/docs/{doc} 可读", code == 200 and text.startswith("{"), f"{code}")
        code, text, _ = await asyncio.to_thread(http_get, f"{base}/api/docs/state?user={user}")
        check.ok("运行时状态不给面板读", code == 404, f"{code} {text[:60]}")
        code, _, _ = await asyncio.to_thread(http_get, f"{base}/api/docs/SOUL?user=../escape")
        check.ok("面板越界 user 被顶回", code == 400, str(code))
        code, text, _ = await asyncio.to_thread(http_get, f"{base}/api/state?user={user}"
                                                 + "&evil=1")
        check.ok("多余参数不影响读数", code == 200, str(code))

        code, _, hd = await asyncio.to_thread(http_get, f"{base}/media/{user}/drawn.png")
        check.ok("/media 能把生成的图端出来", code == 200 and hd.get("Content-Type") == "image/png", f"{code} {hd}")
        for attempt in (f"/media/{user}/notes.md", f"/media/{user}/sub/deep.png",
                        f"/media/{user}/%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fconfig.py",
                        "/media/..%2fconfig.py", f"/media/bad..id/drawn.png"):
            got, _, _ = await asyncio.to_thread(http_get, base + attempt)
            check.ok(f"{attempt.split('/')[-1] or attempt} 取不到", got in (400, 404, 415), str(got))

        relations_before = await storage.read_doc(user, "RELATIONS")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            got, _ = await asyncio.to_thread(http_send, f"{base}/api/state?user={user}", method, {"熟络度": 100})
            check.ok(f"面板不接 {method}", got == 405, str(got))
        for target in ("/panel", "/api/timeline", "/api/docs/USER", f"/media/{user}/drawn.png", "/api/rapport"):
            got, _ = await asyncio.to_thread(http_send, base + target + f"?user={user}", "POST", {"score": 999})
            check.ok(f"{target} 没有写入口", got == 405, str(got))
        check.ok("反复锤温度后文档一字未动", await storage.read_doc(user, "RELATIONS") == relations_before)
        engine = RapportEngine(settings, storage)
        check.ok("温度仍是 0（没人真陪她聊过）", (await engine.read(user)).value == 0)

        state_after = json.loads((await asyncio.to_thread(http_get, f"{base}/api/state"))[1])
        check.ok("不带 user 就回落默认用户", state_after["user_id"] == settings.default_user_id, state_after["user_id"])

        # 面板与 CLI/引擎同源：真跑一轮，温度涨了，页面读数跟着涨
        picture = settings.storage_dir / "shared.png"
        picture.write_bytes(png_bytes("shared"))
        SEEN.clear()
        spoken = "".join([
            piece async for piece in server.bot.stream_reply(user, "我最近在写一本小说", today=STAMP.date(), now=STAMP)
        ])
        check.ok("服务侧真跑完了一轮", spoken.startswith(REPLY_RE), repr(spoken))
        check.ok("跑完不再占用回合", server.active == 0, str(server.active))
        fresh = json.loads((await asyncio.to_thread(http_get, f"{base}/api/state?user={user}"))[1])
        published = (await engine.read(user)).value
        check.ok("页面温度与文档温度一致", fresh["rapport"]["score"] == published, f"{fresh['rapport']['score']} vs {published}")
        check.ok("跑完一轮温度不再是 0", published >= 1, str(published))
        check.ok("页面把阶段一起改了", fresh["rapport"]["stage"] == (await engine.read(user)).stage)
        check.ok("页面精力被消耗过", fresh["energy"]["turns_today"] >= 1, str(fresh["energy"]))
        status, raw, _ = await asyncio.to_thread(http_get, f"{base}/healthz")
        health = json.loads(raw)
        check.ok("/healthz 报告面板与视觉", health["panel_enabled"] is True and health["vision_enabled"] is True, raw[:200])
        check.ok("/healthz 报积压", "memory_backlog" in health and "active_turns" in health, raw[:200])

        settings.panel_enabled = False
        code, text, _ = await asyncio.to_thread(http_get, f"{base}/panel")
        check.ok("PANEL_ENABLED=false 就关掉", code == 404, f"{code} {text[:60]}")
        code, _, _ = await asyncio.to_thread(http_get, f"{base}/api/state?user={user}")
        check.ok("关掉面板也关掉数据", code == 404, str(code))
        code, _, _ = await asyncio.to_thread(http_get, f"{base}/v1/models")
        check.ok("酒馆端点不受面板开关影响", code == 200, str(code))
        settings.panel_enabled = True
    finally:
        await server.stop()


# ================================================================ 5. 前端脚本与安全底线
def strip_js_comments(text: str) -> str:
    """把注释剥掉再谈「代码里有没有」——注释里写着「不显示 token」不该算成在显示 token。"""
    return re.sub(r"(?m)^\s*//.*$", " ", re.sub(r"/\*.*?\*/", " ", text, flags=re.S))


def frontend_checks(check: Checker) -> None:
    folder = PROJECT_ROOT / "web" / "panel"
    html = (folder / "index.html").read_text(encoding="utf-8")
    script = (folder / "app.js").read_text(encoding="utf-8")
    style = (folder / "app.css").read_text(encoding="utf-8")
    code = strip_js_comments(script)

    check.ok("脚本只读接口", "/api/state" in script and "/api/timeline" in script)
    methods = re.findall(r'method:\s*"([A-Za-z]+)"', code)
    check.ok("脚本只发 GET 与 POST", set(methods) <= {"POST"}, str(methods))
    posts = re.findall(r'\bpost\("(/[^"]+)"', code)
    check.ok("POST 只对着对话与语音两个口", set(posts) <= {"/v1/chat/completions", "/voice/say"}, str(posts))
    check.ok("语音与对话之外没有写入口", not re.search(r"\b(PUT|PATCH|DELETE)\b", code))
    check.ok("只读接口只有读一条路", not re.search(r'\bpost\("/api', code))
    check.ok("界面不显示 token 与采样参数", not re.search(r"\b(tokens?|usage|logprobs|temperature|max_tokens)\b", code))
    check.ok("记忆正文走 textContent", "textContent" in script)
    check.ok("脚本不拼 innerHTML", "innerHTML" not in script)
    check.ok("只用一个 fetch 封装", script.count("fetch(") == 1, str(script.count("fetch(")))
    check.ok("HTML 无脚本内联事件", "onclick=" not in html and "onerror=" not in html)
    check.ok("HTML 无第三方埋点", "googleapis" not in html and "analytics" not in html.lower())
    check.ok("CSS 不引外部字体", "@import" not in style and "url(http" not in style)
    check.ok("面板会提示掉线", "markOffline" in script or "连不上" in script)
    check.ok("面板会重连刷新", "setInterval" in script)
    check.ok("每 20 秒看一眼", "20000" in script)


# ================================================================ 6. 守护与优雅退出
async def daemon_checks(check: Checker, settings: Any) -> None:
    from core.memory_extractor import MemoryExtractor
    from core.server import SoulServer
    from core.storage_manager import StorageManager

    draining = make_settings(Path(tempfile.mkdtemp(prefix="mysoulbot-drain-")), settings.base_url)
    draining.extractor_enabled = True
    draining.tools_enabled = False
    storage = StorageManager(draining)
    user = "drain_user"
    await storage.ensure_user(user)

    server = SoulServer(draining)
    host, port = await server.start("127.0.0.1", 11888)
    base = f"http://{host}:{port}"
    try:
        check.ok("服务先接单", (await asyncio.to_thread(http_get, f"{base}/healthz"))[0] == 200)

        MODE["extract_sleep"] = 0.6
        MODE["extract_lines"] = ["- [2026-10-03] 用户最近一直失眠"]
        before = len(await storage.read_facts(user))
        server.extractor.start()
        sent = server.bot.extractor.submit(
            user, [{"role": "user", "content": "我最近一直失眠"}, {"role": "assistant", "content": "多久了"}],
            today=dt.date(2026, 10, 3),
        )
        check.ok("抽取任务进了内存队列", sent, str(sent))
        check.ok("停机前确实有欠账", server.bot.extractor.backlog >= 1, str(server.bot.extractor.backlog))

        settled = await server.drain(5.0)
        check.ok("排空后不再有欠账", settled["backlog"] == 0, str(settled))
        check.ok("在途回合归零", settled["turns_left"] == 0, str(settled))
        after = await storage.read_facts(user)
        check.ok("内存里的记忆真落盘了", len(after) == before + 1, str(after))
        check.ok("落的是那条事实", any("失眠" in text for _, text in after), str(after))

        # 欠账超过等待时间：如实报出来，不含糊
        MODE["extract_sleep"] = 3.0
        MODE["extract_lines"] = ["- [2026-10-03] 用户在写一本小说"]
        server.bot.extractor.submit(
            user, [{"role": "user", "content": "我在写一本小说"}, {"role": "assistant", "content": "写到哪了"}],
            today=dt.date(2026, 10, 3),
        )
        tight = await server.drain(0.4)
        check.ok("等不到就如实报欠账", tight["backlog"] >= 1, str(tight))
        MODE["extract_sleep"] = 0.0
        await asyncio.sleep(3.2)
        check.ok("模型慢不等于丢任务", (await storage.read_facts(user)) and any("小说" in text for _, text in await storage.read_facts(user)), str(await storage.read_facts(user)))

        # 先关接单，再谈排空
        await server.close_listener()
        try:
            code, _, _ = await asyncio.to_thread(http_get, f"{base}/healthz")
            reachable = code == 200
        except (urllib.error.URLError, ConnectionError, OSError):
            reachable = False
        check.ok("关监听后不再接新连接", not reachable)
        await server.close_listener()  # 幂等
        check.ok("重复关监听不炸", True)
    finally:
        await server.stop()

    # extractor 生命周期：aclose 会等队列，不丢
    extractor = MemoryExtractor(draining, storage)
    extractor.start()
    extractor.submit(storage.user_dir("drain_user").name,
                     [{"role": "user", "content": "我养了只猫"}, {"role": "assistant", "content": "叫什么"}],
                     today=dt.date(2026, 10, 3))
    check.ok("提交后队列里有活", extractor.backlog >= 1, str(extractor.backlog))
    left = await extractor.wait_idle(6.0)
    check.ok("wait_idle 等到排空", left == 0, str(left))
    await extractor.aclose(timeout=4.0)
    check.ok("关闭后 worker 不再持有任务", extractor._worker is None)  # noqa: SLF001

    # 停机脚本与 systemd 模板
    script = PROJECT_ROOT / "scripts" / "daemon.sh"
    check.ok("daemon.sh 存在且可执行", script.is_file() and script.stat().st_mode & 0o111 > 0, oct(script.stat().st_mode) if script.is_file() else "缺失")
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    check.ok("daemon.sh 语法过", syntax.returncode == 0, syntax.stderr[:160])
    body = script.read_text(encoding="utf-8")
    for verb in ("start", "stop", "restart", "status", "logs"):
        check.ok(f"daemon.sh 支持 {verb}", f"{verb})" in body, verb)
    executed = [line.strip() for line in body.splitlines() if not line.strip().startswith("#")]
    check.ok("stop 用 SIGTERM 不用 -9", "kill -TERM" in body and not any(line.startswith("kill -9") for line in executed))
    check.ok("端口取自配置不写死", "port_of" in body)
    check.ok("start 等健康检查", "/healthz" in body)
    unit = (PROJECT_ROOT / "scripts" / "mysoulbot.service").read_text(encoding="utf-8")
    check.ok("systemd 模板存在", "[Unit]" in unit and "ExecStart=" in unit)
    check.ok("systemd 用 SIGTERM 停机", "KillSignal=SIGTERM" in unit)
    check.ok("systemd 给足排空时间", "TimeoutStopSec=90" in unit)
    started = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    check.ok("systemd 默认不暴露到网段", started and "--public" not in started[0], str(started)[:120])


# ================================================================ 主流程
async def main() -> int:
    server, base_url = serve_fake()
    roots: list[Path] = []
    check = Checker()

    def fresh(prefix: str, **overrides: Any) -> Any:
        root = Path(tempfile.mkdtemp(prefix=prefix))
        roots.append(root)
        return make_settings(root, base_url, **overrides)

    try:
        ingest_checks(check, fresh("mysoulbot-ingest-"))
        await multimodal_checks(check, fresh("mysoulbot-mm-"))
        await tool_checks(check, fresh("mysoulbot-tool-"))
        await panel_http_checks(check, fresh("mysoulbot-panel-", default_user_id="guest"))
        frontend_checks(check)
        await daemon_checks(check, fresh("mysoulbot-daemon-"))
    finally:
        server.shutdown()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
