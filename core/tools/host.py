"""基础无害操作：看一眼机器负荷，以及在她自己的草稿工位里记点东西。

这一层刻意做得很薄——**只读聚合数字 + 只写自己的工位**，两件事：

- `host_stats` 不接任何参数，也就没有路径穿越的余地；报的是负荷、内存、磁盘这类
  「朋友问我我顺手看一眼」的量级，不列进程、不读环境变量、不回显任何绝对路径。
- 草稿工具全部走 `sandbox.assert_scratch`：她的记事只能落在 `storage/sandbox/<自己>/`，
  写不到别人的目录，也写不到灵魂资产和系统本体。工位和记事本是两道独立的闸。

危险动作（删、执行、改配置）不在这里，那套只开工单、绝不动手，见 `core/sandbox.py`。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any, Final

from core.sandbox import SandboxError, assert_scratch, scratch_dir
from core.tools.base import Tool, ToolContext, ToolParam, ToolResult


def _read_first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _loadavg() -> tuple[float, float, float] | None:
    with contextlib.suppress(OSError, AttributeError, ValueError):
        return os.getloadavg()
    parts = _read_first_line("/proc/loadavg").split()
    if len(parts) >= 3:
        with contextlib.suppress(ValueError):
            return float(parts[0]), float(parts[1]), float(parts[2])
    return None


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split()
                if value and key in ("MemTotal", "MemAvailable"):
                    out[key] = int(value[0])  # kB
    except OSError:
        pass
    return out


def _uptime() -> float:
    parts = _read_first_line("/proc/uptime").split()
    if not parts:
        return 0.0
    with contextlib.suppress(ValueError):
        return float(parts[0])
    return 0.0


def _human_gb(kb: float) -> str:
    return f"{kb / 1024 / 1024:.1f}G"


def _human_secs(secs: float) -> str:
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


class HostStats(Tool):
    name = "host_stats"
    description = (
        "看一眼这台机器的负荷：CPU 负载、内存用了多少、磁盘还剩多少、开了多久。"
        "对方问「你那边卡不卡」「机器还撑得住吗」就用它。只读，改不了任何东西。"
    )
    hint = "能看机器负荷"
    params: tuple[ToolParam, ...] = ()
    sensitive = False

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.host_stats_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002
        stats = await asyncio.to_thread(_collect, ctx)
        if not stats:
            return ToolResult.failure("这台机器的负荷读不出来",
                                      say="（这次没读到机器负荷，直接说看不到就行。）")
        return ToolResult.success("\n".join(stats), meta={"keys": [line.split("：")[0] for line in stats]})


def _collect(ctx: ToolContext) -> list[str]:
    lines: list[str] = []
    cores = os.cpu_count() or 0
    load = _loadavg()
    if load:
        pct = f"，约 {load[0] / cores * 100:.0f}% 满载" if cores else ""
        lines.append(f"CPU 负载：{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"
                     f"（{cores} 核，1 分钟均值{pct}）")
    mem = _meminfo()
    if mem.get("MemTotal"):
        used = mem["MemTotal"] - mem.get("MemAvailable", 0)
        pct = used / mem["MemTotal"] * 100
        lines.append(f"内存：{_human_gb(used)} / {_human_gb(mem['MemTotal'])}（已用 {pct:.0f}%）")
    try:
        total, used, free = shutil.disk_usage(ctx.settings.storage_dir)
        if total:
            lines.append(f"磁盘：剩余 {_human_gb(free / 1024)} / {_human_gb(total / 1024)}"
                         f"（已用 {used / total * 100:.0f}%）")
    except OSError:
        pass
    up = _uptime()
    if up:
        lines.append(f"已连续运行：{_human_secs(up)}")
    return lines


def _safe_name(name: str) -> str:
    """草稿文件名：只留一个干净的文件名，路径分隔符与 `..` 一律挤掉。"""
    stem = Path(str(name or "").strip().replace("\\", "/")).name
    stem = stem.strip(". ") or "note.md"
    return stem[:80]


class ScratchWrite(Tool):
    name = "scratch_write"
    description = (
        "把一段东西记进你自己的草稿里（记事、算一半的数、查来的材料）。"
        "存在你自己的工位上，别人看不到，也影响不到你的灵魂资产和系统。"
    )
    hint = "有自己的草稿工位"
    params = (
        ToolParam("name", "string", "文件名，比如 draft.md"),
        ToolParam("text", "string", "要记下的内容"),
    )
    primary_arg = "name"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.sandbox_scratch_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = _safe_name(str(args.get("name", "")))
        text = str(args.get("text", args.get("content", "")))
        if not text.strip():
            return ToolResult.failure("没给要记的内容", say="（要记什么总得给我点字。）")
        return await asyncio.to_thread(_write_scratch, ctx, name, text)


def _write_scratch(ctx: ToolContext, name: str, text: str) -> ToolResult:
    s = ctx.settings
    if len(text.encode("utf-8")) > s.scratch_max_bytes:
        return ToolResult.failure(
            f"这段太长，工位单张纸上限 {s.scratch_max_bytes} 字节",
            say=f"（这张纸写不下，最多 {s.scratch_max_bytes} 字节，得拆着记。）")
    try:
        path = assert_scratch(scratch_dir(s, ctx.user_id) / name, settings=s, user_id=ctx.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = [p for p in path.parent.iterdir() if p.is_file()] if path.parent.is_dir() else []
        if not path.exists() and len(existing) >= s.scratch_max_files:
            return ToolResult.failure(
                f"工位上已经有 {s.scratch_max_files} 张纸了",
                say="（我这块草稿地方摊满了，得先擦掉几张再记新的。）")
        path.write_text(text, encoding="utf-8")
    except (SandboxError, OSError) as exc:
        return ToolResult.failure(str(exc))
    return ToolResult.success(f"记下了：{name}（{len(text)} 字，在你自己的草稿工位里）",
                              meta={"name": name, "bytes": path.stat().st_size})


class ScratchRead(Tool):
    name = "scratch_read"
    description = "把之前记在草稿工位里的某张纸读回来。"
    hint = "能翻自己的草稿"
    params = (ToolParam("name", "string", "文件名"),)
    primary_arg = "name"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.sandbox_scratch_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        s = ctx.settings
        name = _safe_name(str(args.get("name", "")))
        try:
            path = assert_scratch(scratch_dir(s, ctx.user_id) / name, settings=s, user_id=ctx.user_id)
        except SandboxError as exc:
            return ToolResult.failure(str(exc))
        if not path.is_file():
            return ToolResult.failure(f"草稿里没有 {name}", say=f"（我这儿没记过叫 {name} 的。）")
        try:
            body = await asyncio.to_thread(path.read_text, "utf-8")
        except OSError as exc:
            return ToolResult.failure(str(exc))
        clip = body[: s.scratch_read_max_chars]
        tail = "" if len(body) <= len(clip) else f"\n……（后面还有 {len(body) - len(clip)} 字没显示）"
        return ToolResult.success(f"【草稿 {name}】\n{clip}{tail}")


class ScratchList(Tool):
    name = "scratch_list"
    description = "看看自己草稿工位上都摊了哪些纸。"
    hint = "能列自己的草稿"
    params: tuple[ToolParam, ...] = ()

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.sandbox_scratch_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002
        s = ctx.settings
        root = scratch_dir(s, ctx.user_id)
        files = await asyncio.to_thread(_list_scratch, root)
        if not files:
            return ToolResult.success("草稿工位是空的，一张纸都没有。")
        return ToolResult.success("草稿工位上有：\n" + "\n".join(files))


def _list_scratch(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return [f"{p.name}（{p.stat().st_size}B）"
            for p in sorted(root.iterdir()) if p.is_file()][:50]
