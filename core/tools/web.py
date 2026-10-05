"""网页能力：看网页、搜资料。

对模型的呈现方式刻意的「读过了」而不是「返回 200」：结果正文直接进入对话语境，
失败也只是一句人话。
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.query import WebSearchCN
from core.tools.webio import FetchError, fetch


class WebBrowse(Tool):
    name = "web_browse"
    description = (
        "打开一个网址，把正文读给你。用于核实对方提到的链接、查最新说法、看具体页面。"
        "拿到的是页面原文，你自己决定引用哪句、怎么开口。"
    )
    hint = "能把网页打开看一眼"
    params = (ToolParam("url", "string", "完整地址，http/https"),)
    primary_arg = "url"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult.failure("没给地址", say="（要说看什么，得先给我地址。）")
        s = ctx.settings
        try:
            page = await asyncio.to_thread(
                fetch,
                url,
                timeout=s.web_timeout,
                max_bytes=s.web_max_bytes,
                allow_private=s.web_allow_private,
            )
        except FetchError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            "【原文材料，消化之后用自己的话说给他，别整段搬过去】\n" + page.render(s.web_max_chars),
            meta={"status": page.status, "bytes": page.bytes_read},
        )


class WebSearch(WebSearchCN):
    """搜索的正名。

    原来这条走 `html.duckduckgo.com`，那个地址在本机根本连不出去
    （`Network is unreachable`），于是「能查资料」是个空头支票。
    实现直接继承中文多源那条（Bing 中文 + www.bing 兜底），只换个名字——
    两套并存的搜索结果只会让模型挑花眼。
    """

    name = "web_search"
