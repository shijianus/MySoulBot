"""图像与快照。

`image_gen` 默认走 `stub`：没有配置真实绘图后端时，它生成一张确定性的占位 PNG，
并在返回正文里**明说是占位图**——角色因此不会假装自己画出了东西。
配置 `IMAGE_PROVIDER=openai` 后走接口的 /images/generations。

`snapshot` 按 playwright → 外部截图命令 → 文字快照 的顺序探测能力，
全部不可用时返回一句可被人格说出口的失败。
"""

from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import shutil
import struct
import subprocess
import zlib
from typing import Any, Final
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.webio import FetchError, _assert_public_host, fetch
from core.vision import (
    ImageRef,
    VisionError,
    adopt_file,
    ingest,
    note as vision_note,
    view_url,
)

_SCREEN_TOOLS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("grim", ("grim", "{out}")),
    ("scrot", ("scrot", "--overwrite", "{out}")),
    ("gnome-screenshot", ("gnome-screenshot", "-f", "{out}")),
    ("screencapture", ("screencapture", "-x", "{out}")),
    ("import", ("import", "-window", "root", "{out}")),
)
_MAX_PIXELS: Final[int] = 1024


class ImageGen(Tool):
    name = "image_gen"
    description = (
        "按一句话描述画一张图，存到本地并给你路径。画完不需要宣告「已生成」，"
        "直接按你自己的口吻聊这张画就行。"
    )
    hint = "能画图"
    params = (
        ToolParam("prompt", "string", "画什么，一句话描述"),
        ToolParam("size", "string", "尺寸，例如 1024x1024", required=False),
    )
    primary_arg = "prompt"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.image_provider != "none"

    def brief(self, ctx: ToolContext) -> str:
        """占位后端不许冒充会画画：工具清单里就得说清楚，
        不然她信了 hint，转头跟人说「我给你画了一只鲸鱼」——那是撒谎。
        """
        if ctx.settings.image_provider == "stub":
            return "画图后端目前是占位实现（只会出纯色渐变），别答应给人画具体的东西"
        return self.hint

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult.failure("没说要画什么", say="（得先告诉我要画什么。）")
        provider = ctx.settings.image_provider
        size = str(args.get("size") or ctx.settings.image_size)
        try:
            width, height = _parse_size(size)
        except ValueError:
            return ToolResult.failure(f"尺寸写法不认识：{size}")
        stamp = asyncio.get_running_loop().time()
        name = f"img-{int(stamp * 1000) % 10_000_000:07d}.png"
        path = ctx.artifact_path(name)
        try:
            if provider == "openai":
                data = await asyncio.to_thread(self._openai, ctx, prompt, size)
            elif provider == "stub":
                data = await asyncio.to_thread(_placeholder_png, prompt, width, height)
            else:
                return ToolResult.failure(
                    f"未知的绘图后端 {provider}", say="（我这边没接上画图的东西，画不出来。）"
                )
        except FetchError as exc:
            return ToolResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001 - 绘图失败不冒泡到界面
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        await asyncio.to_thread(path.write_bytes, data)
        link = view_url(ctx.settings, ctx.user_id, path)
        note = (
            f"画好了：{path}\n能点开的地址：{link}\n"
            "（本地图像后端是占位实现，产出的是纯色渐变图，不是真的画面内容。"
            "别把它说成你画了什么具体东西。）"
            if provider == "stub"
            else f"画好了：{path}\n能点开的地址：{link}\n描述：{prompt}"
        )
        try:
            ref = adopt_file(path, ctx.settings)
        except VisionError:
            ref = None
        meta: dict[str, Any] = {"provider": provider, "link": link}
        if ref is not None:
            meta["images"] = [ref]
        return ToolResult.success(note, artifacts=[path], meta=meta)

    @staticmethod
    def _openai(ctx: ToolContext, prompt: str, size: str) -> bytes:
        s = ctx.settings
        # 绘图通道常常和对话不在同一个网关/群组上，所以允许单独指一条
        url = f"{(s.image_base_url or s.base_url).rstrip('/')}/images/generations"
        payload = {
            "model": s.image_model or s.model,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
        }
        request = Request(  # noqa: S310 - base_url 由配置给出，只接受 http/https
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {s.image_api_key or s.api_key or 'EMPTY'}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=s.web_timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 - 接口错误统一收成一句人话
            raise FetchError(f"画图那边没响应（{type(exc).__name__}）") from exc
        items = body.get("data") or []
        if not items:
            raise FetchError("画图接口回了个空结果")
        first = items[0]
        if first.get("b64_json"):
            return base64.b64decode(first["b64_json"])
        if first.get("url"):
            return _download_bytes(first["url"], s.web_timeout, s.web_max_bytes, s.web_allow_private)
        raise FetchError("画图接口的返回我读不懂")


class SeeImage(Tool):
    name = "see_image"
    description = (
        "他递来一张图（本机路径、网址，或他贴出来的那串 base64），你把它收进眼里再开口。"
        "不用复述文件名，也不用宣告「已接收」——看见了就说你看见的那个部分。"
    )
    hint = "能看他发来的图片"
    params = (ToolParam("source", "string", "图片路径、http(s) 链接，或 data:image;base64 串"),)
    primary_arg = "source"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.vision_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        source = str(args.get("source", "")).strip()
        if not source:
            return ToolResult.failure(
                "没给图的来路", say="（他没说要看哪张。自然地问他一句就行，别报参数名。）"
            )
        try:
            ref = await asyncio.to_thread(ingest, source, ctx.settings, ctx.storage, ctx.user_id)
        except VisionError as exc:
            return ToolResult.failure(str(exc), say=f"（收不下这张：{exc}。照你自己的方式说一句就行。）")
        except Exception as exc:  # noqa: BLE001 - 读盘失败不冒泡到界面
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        seen = ctx.settings.vision_enabled
        return ToolResult.success(
            vision_note([ref], seen=seen),
            artifacts=[ref.path],
            meta={"images": [ref], "link": view_url(ctx.settings, ctx.user_id, ref.path)},
        )


def _download_bytes(url: str, timeout: float, max_bytes: int, allow_private: bool = False) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FetchError("绘图后端给了一个我打不开的地址")
    if not allow_private:
        _assert_public_host(parsed.hostname or "")
    try:
        with urlopen(Request(url), timeout=timeout) as response:  # noqa: S310
            data = response.read(max_bytes + 1)
    except OSError as exc:
        raise FetchError("绘图后端给的链接没下载下来") from exc
    if len(data) > max_bytes:
        raise FetchError("那张图太大了，我没收下")
    return data


class Snapshot(Tool):
    name = "snapshot"
    description = (
        "拍一张眼前的东西：给 URL 就截那个页面，给 screen（或不给）就截本机屏幕。"
        "拍完你自己看一眼再说话，不用宣告「截图完成」。"
    )
    hint = "能拍网页或屏幕的快照"
    params = (
        ToolParam("target", "string", "网址，或 screen 表示本机屏幕", required=False),
    )
    primary_arg = "target"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.snapshot_provider != "none"

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        target = str(args.get("target", "")).strip() or "screen"
        provider = self._probe(ctx)
        if target == "screen":
            if provider == "binary":
                return await self._screen_shot(ctx)
            if provider == "playwright":
                return ToolResult.failure(
                    "playwright 截不了无头环境的本机屏幕",
                    say="（这台机器现在没有可拍的屏幕，我拍不到。）",
                )
            return ToolResult.failure(
                "没有可用的屏幕抓取手段", say="（这台机器现在没有可拍的屏幕，我拍不到。）"
            )
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"}:
            return ToolResult.failure(f"快照目标必须是网址或 screen，收到 {target[:60]}")
        return await self._text_shot(ctx, target)

    def _probe(self, ctx: ToolContext) -> str:
        wanted = ctx.settings.snapshot_provider
        if wanted in {"playwright", "binary", "text"}:
            return wanted
        try:  # 只有显式配置了 playwright 才尝试，避免无意义导入开销
            import importlib.util

            if wanted == "auto" and importlib.util.find_spec("playwright"):
                return "playwright"
        except Exception:  # noqa: BLE001
            pass
        for command, _ in _SCREEN_TOOLS:
            if shutil.which(command):
                return "binary"
        return "text"

    async def _screen_shot(self, ctx: ToolContext) -> ToolResult:
        for command, template in _SCREEN_TOOLS:
            binary = shutil.which(command)
            if not binary:
                continue
            path = ctx.artifact_path(f"screen-{int(asyncio.get_running_loop().time()) % 100000}.png")
            argv = [piece.replace("{out}", str(path)) for piece in template]
            argv[0] = binary
            try:
                done = await asyncio.to_thread(
                    subprocess.run, argv, capture_output=True, timeout=25, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0 and path.is_file():
                meta: dict[str, Any] = {"tool": command}
                body = f"拍好了：{path}"
                try:
                    ref = adopt_file(path, ctx.settings)
                except VisionError:
                    ref = None
                if ref is not None:
                    meta["images"] = [ref]
                    body += "\n" + vision_note([ref], seen=ctx.settings.vision_enabled)
                return ToolResult.success(body, artifacts=[path], meta=meta)
        return ToolResult.failure(
            "屏幕抓取命令都没成功", say="（这会儿我拍不到屏幕，可能是没有显示环境。）"
        )

    async def _text_shot(self, ctx: ToolContext, url: str) -> ToolResult:
        s = ctx.settings
        try:
            page = await asyncio.to_thread(
                fetch, url, timeout=s.web_timeout, max_bytes=s.web_max_bytes, allow_private=s.web_allow_private
            )
        except FetchError as exc:
            return ToolResult.failure(str(exc))
        path = ctx.artifact_path(f"snapshot-{int(asyncio.get_running_loop().time() * 1000) % 100000}.txt")
        body = f"# {page.title}\n# {page.url}\n\n{page.text}\n"
        await asyncio.to_thread(path.write_text, body, "utf-8")
        return ToolResult.success(
            f"我把这页存成文字快照了：{path}\n开头是：{page.text[:200]}",
            artifacts=[path],
            meta={"mode": "text"},
        )


def _parse_size(text: str) -> tuple[int, int]:
    parts = text.lower().replace("×", "x").split("x")
    if len(parts) != 2:
        raise ValueError(text)
    width, height = int(parts[0]), int(parts[1])
    if not 8 <= width <= _MAX_PIXELS or not 8 <= height <= _MAX_PIXELS:
        raise ValueError(f"尺寸超出 8-{_MAX_PIXELS}：{text}")
    return width, height


def _placeholder_png(prompt: str, width: int, height: int) -> bytes:
    """确定性占位图：按描述哈希取色的纵向渐变。标准库直出 PNG，无第三方依赖。"""
    seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:6], 16)
    top = (seed >> 16 & 0xFF, seed >> 8 & 0xFF, seed & 0xFF)
    bottom = (255 - top[0] // 2, 255 - top[1] // 2, 255 - top[2] // 2)

    raw = bytearray()
    for y in range(height):
        ratio = y / max(1, height - 1)
        trio = bytes(int(top[c] + (bottom[c] - top[c]) * ratio) for c in range(3))
        raw += b"\x00" + trio * width
    return _png_bytes(width, height, bytes(raw))


def _png_bytes(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
