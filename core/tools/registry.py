"""轻量工具调度器。

一条硬规矩：**工具从不对用户说话**。它的返回值只回到模型，由模型决定怎么说、说不说。
所以这里没有「进度条」「spinner」「已调用 3 个工具」这类东西——那是界面层的事，
而界面层拿到的只有角色说出来的话。

- `native_specs()` → 接口的 function calling 声明（模型直接下单）。
- `instructions()` → 行内暗号协议说明（接口不支持原生下单时注入 prompt）。
- `call()` → 静默执行：参数整理、超时、失败收敛成人话、审计落盘。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final

from core.storage_manager import atomic_write
from core.sandbox import ApprovalDesk
from core.tools.base import Tool, ToolContext, ToolResult
from core.tools.git import GitSync
from core.tools.host import HostStats, ScratchList, ScratchRead, ScratchWrite
from core.tools.media import ImageGen, SeeImage, Snapshot
from core.tools.persona import PersonaAdopt, PersonaRewrite
from core.tools.protocol import Directive
from core.tools.qq_account import ACCOUNT_TOOLS
from core.tools.query import QUERY_TOOLS
from core.tools.soul import Reflect
from core.tools.web import WebBrowse, WebSearch

logger: Final = logging.getLogger("mysoulbot.tools")

DEFAULT_TOOLS: Final[tuple[type[Tool], ...]] = (
    WebBrowse,
    WebSearch,
    *QUERY_TOOLS,
    ImageGen,
    SeeImage,
    Snapshot,
    GitSync,
    Reflect,
    # 自我塑造两只：改人格、换人格。都是 sensitive——改的是她下一轮被喂进去的东西，
    # 靠别名或模糊匹配触发不能接受（`resolve()` 对 sensitive 只认实名）
    PersonaRewrite,
    PersonaAdopt,
    HostStats,
    ScratchWrite,
    ScratchRead,
    ScratchList,
    *ACCOUNT_TOOLS,
)
ALIASES: Final[dict[str, str]] = {
    "browse": "web_browse",
    "web": "web_browse",
    "fetch": "web_browse",
    "open_url": "web_browse",
    "read_url": "web_browse",
    "search": "web_search",
    "google": "web_search",
    "搜": "web_search_cn",
    "查一下": "web_search_cn",
    "天气": "weather_now",
    "气温": "weather_now",
    "下雨": "weather_now",
    "股价": "stock_quote",
    "股票": "stock_quote",
    "A股": "stock_quote",
    "汇率": "exchange_rate",
    "换汇": "exchange_rate",
    "车票": "train_query",
    "高铁": "train_query",
    "火车": "train_query",
    "热榜": "hot_list",
    "热搜": "hot_list",
    "负荷": "host_stats",
    "负载": "host_stats",
    "机器": "host_stats",
    "内存": "host_stats",
    "磁盘": "host_stats",
    "卡不卡": "host_stats",
    "草稿": "scratch_write",
    "通讯录": "qq_roster",
    "我的群": "qq_roster",
    "翻记录": "qq_read_history",
    "点赞": "qq_like",
    "动态": "qq_publish_qzone",
    "发动态": "qq_publish_qzone",
    "申请": "qq_requests",
    "入群申请": "qq_requests",
    "记一下": "scratch_write",
    "翻草稿": "scratch_read",
    "draw": "image_gen",
    "generate_image": "image_gen",
    "paint": "image_gen",
    "image": "image_gen",
    "see": "see_image",
    "look": "see_image",
    "view_image": "see_image",
    "photo": "see_image",
    "看图": "see_image",
    "screenshot": "snapshot",
    "screen": "snapshot",
    "capture": "snapshot",
    "git": "git_sync",
    "sync": "git_sync",
    "push": "git_sync",
    "backup": "git_sync",
    "remember": "reflect",
    "note": "reflect",
    "反思": "reflect",
}
AUDIT_NAME: Final[str] = "tools.jsonl"
AUDIT_MAX_BYTES: Final[int] = 1_000_000
AUDIT_TAIL_LINES: Final[int] = 400


# 危险动作的形状：这些一律不自动执行，只开工单
_DANGER: Final[tuple[tuple[str, ...], str]] = (
    (("delete", "remove", "unlink", "rmdir", "rm", "drop", "purge", "清空", "删除", "删掉"), "删除文件"),
    (("shell", "exec", "system", "bash", "sh", "subprocess", "run_cmd", "执行命令", "跑命令"), "执行命令"),
    (("chmod", "chown", "mkfs", "reboot", "kill", "重启", "提权"), "改系统权限或状态"),
    (("edit_config", "write_config", "patch", "rewrite_code", "改代码", "改配置"), "改底层配置或代码"),
)


def classify_danger(name: str) -> str:
    """把「delete_files」「跑命令」这类请求认出来，返回要审批的动作名；不是危险动作就返回空串。"""
    key = (name or "").strip().lower()
    if not key:
        return ""
    for tokens, action in _DANGER:
        if any(token in key for token in tokens):
            return action
    return ""


class ToolRegistry:
    """当前用户可用的工具集合与执行入口。"""

    def __init__(self, ctx: ToolContext, tools: Iterable[Tool] | None = None) -> None:
        self.ctx = ctx
        self.approvals = ApprovalDesk(ctx.settings)
        instances: Sequence[Tool] = list(tools) if tools is not None else [T() for T in DEFAULT_TOOLS]
        self._tools = [tool for tool in instances if tool.available(ctx)]
        self._by_name = {tool.name: tool for tool in self._tools}

    # ------------------------------------------------------------ 声明
    def __bool__(self) -> bool:
        return bool(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    def summary(self) -> str:
        """说给她听的手段清单。走 `brief` 而不是 `hint`：后端只是占位时，这里必须改口，
        不然她信了「能画图」就答应给人画具体的东西。"""
        return "、".join(tool.brief(self.ctx) for tool in self._tools) if self._tools else "暂时没有能使的劲"

    def native_specs(self) -> list[dict[str, Any]]:
        return [tool.native_spec() for tool in self._tools]

    def usage_lines(self) -> list[str]:
        return [tool.usage_line() for tool in self._tools]

    def for_user(self, user_id: str) -> "ToolRegistry":
        return ToolRegistry(
            ToolContext(
                settings=self.ctx.settings,
                storage=self.ctx.storage,
                user_id=user_id,
                clawd=self.ctx.clawd,
            ),
            self._tools,
        )

    # ------------------------------------------------------------ 行内暗号协议
    def instructions(self) -> str:
        lines = "\n".join(f"  ⟦tool:{usage}⟧" for usage in self.usage_lines())
        return (
            "【工具暗号】这个接口没给你挂原生工具调用，改用暗号下单。\n"
            "- 需要工具时，**单独占一行**写：⟦tool:工具名 参数=值⟧；"
            "带空格或网址的值用引号包住；只给一个裸值时它填进第一个参数。\n"
            "  例：⟦tool:web_browse url=\"https://example.com\"⟧\n"
            "      ⟦tool:web_search \"凌晨三点睡不着怎么办\"⟧\n"
            f"  你可用的：\n{lines}\n"
            "- 这一行是写给我的，对方看不见，也不许向对方提起「暗号」「工具」「参数」这些词。\n"
            "- 一行只下一个单。不要在同一行里既说话又下单。\n"
            "- 拿到结果后直接说结论，就像你本人刚去看了一样；做不到就一句带过，不解释机制。"
        )

    # ------------------------------------------------------------ 执行
    def resolve(self, name: str) -> Tool | None:
        key = (name or "").strip().lower()
        if not key:
            return None
        if key in self._by_name:
            return self._by_name[key]
        aliased = ALIASES.get(key)
        if aliased in self._by_name and not self._by_name[aliased].sensitive:
            return self._by_name[aliased]
        # 会对外动手/改写灵魂的工具不接受「猜」：push、sync 之类不能误触发 git_sync
        candidates = [tool.name for tool in self._tools if not tool.sensitive]
        close = difflib.get_close_matches(key, candidates, n=1, cutoff=0.62)
        return self._by_name[close[0]] if close else None

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        tool = self.resolve(name)
        if tool is None:
            danger = classify_danger(name)
            if danger:
                # 删文件、跑命令、改底层配置这类事：不开口子、也不悄悄吞掉，
                # 落一张「待人类确认」的工单，让人看得见这件事发生过
                ticket = self.approvals.request(danger, f"{name} {args or {}}", requested_by=self.ctx.user_id)
                logger.warning("危险动作已登记工单 %s：%s（未执行）", ticket.id, danger)
                return ToolResult.failure(
                    f"这一步要人点头：已登记工单 {ticket.id}，引擎没有替你执行",
                    say="（这种事不归我决定，我已经挂上待办了，等人点。）",
                )
            logger.info("收到不认得的工具名：%s", name)
            return ToolResult.failure(
                f"没有这个能力：{name}", say="（这件事我做不了，也别硬编。自然地岔开或直说。）"
            )
        clean = tool.coerce(args or {})
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                tool.run(self.ctx, clean), timeout=self.ctx.settings.tool_timeout
            )
        except TimeoutError:
            result = ToolResult.failure(
                f"{tool.name} 跑了超过 {int(self.ctx.settings.tool_timeout)} 秒",
                say="（这事卡住了，没结果。就说没看成，别解释为什么。）",
            )
        except Exception as exc:  # noqa: BLE001 - 工具异常绝不允许冒泡到界面
            logger.warning("工具 %s 异常: %s", tool.name, exc, exc_info=True)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
        self._audit(tool.name, clean, result, int((time.perf_counter() - started) * 1000))
        return result

    async def call_directive(self, directive: Directive) -> ToolResult:
        tool = self.resolve(directive.name)
        if tool is None:
            return await self.call(directive.name, {})
        args = directive.args or tool.from_bare(directive.raw_args)
        return await self.call(tool.name, args)

    async def call_many(
        self, calls: Iterable[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, ToolResult]]:
        """一趟里的多个工具并行跑完，结果按下单顺序回填。"""
        pairs = list(calls)
        results = await asyncio.gather(*(self.call(name, args) for name, args in pairs))
        return [(name, result) for (name, _), result in zip(pairs, results, strict=True)]

    # ------------------------------------------------------------ 审计
    def _audit(self, name: str, args: dict[str, Any], result: ToolResult, ms: int) -> None:
        """记一行业务日志：只存参数键名与长度，不存抓回来的正文，避免把别人的 token 抄进日志。"""
        if not self.ctx.settings.tool_audit:
            return
        path: Path = self.ctx.settings.audit_dir / AUDIT_NAME
        record = {
            "tool": name,
            "user": self.ctx.user_id,
            "ok": result.ok,
            "ms": ms,
            "args": sorted(args),
            "chars": len(result.content),
            # 失败原因来自引擎自己（不抓外部正文），成功时只记长度
            "error": result.error[:120] if not result.ok else "",
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > AUDIT_MAX_BYTES:
                self._trim_audit(path)
        except OSError as exc:
            logger.debug("工具审计落盘失败: %s", exc)
        logger.info("工具 %s · %s · %dms", name, "ok" if result.ok else "fail", ms)

    @staticmethod
    def _trim_audit(path: Path) -> None:
        kept = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        atomic_write(path, "\n".join(kept[-AUDIT_TAIL_LINES:]) + "\n")
