"""网页能力：看网页、搜资料。

对模型的呈现方式刻意的「读过了」而不是「返回 200」：结果正文直接进入对话语境，
失败也只是一句人话。
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from typing import Any, Final
from urllib.parse import parse_qs, quote, unquote, urlparse

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.webio import FetchError, fetch

_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_RESULT_LINK: Final[re.Pattern[str]] = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I
)
_RESULT_SNIPPET: Final[re.Pattern[str]] = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
SEARCH_ENDPOINT: Final[str] = "https://html.duckduckgo.com/html/?q="


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


class WebSearch(Tool):
    name = "web_search"
    description = "搜一个关键词，拿到前几条结果的标题、摘要和地址。不确定就从这里开始查。"
    hint = "能查资料"
    params = (
        ToolParam("query", "string", "搜索词，自然语言即可"),
        ToolParam("limit", "integer", "最多几条，默认 5", required=False),
    )
    primary_arg = "query"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.failure("没给搜索词", say="（要查什么，先说个词。）")
        limit = max(1, min(int(args.get("limit") or 5), 8))
        s = ctx.settings
        try:
            page = await asyncio.to_thread(
                fetch,
                SEARCH_ENDPOINT + quote(query, safe=""),
                timeout=s.web_timeout,
                max_bytes=s.web_max_bytes,
                allow_private=s.web_allow_private,
            )
        except FetchError as exc:
            return ToolResult.failure(str(exc))
        hits = _parse_results(page.html, limit)
        if not hits:
            return ToolResult.failure("搜索没有返回结果", say="（搜了一圈没搜到，我只能说不知道。）")
        body = "\n".join(
            f"{i}. {title}\n   {snippet}\n   {url}"
            for i, (title, snippet, url) in enumerate(hits, 1)
        )
        return ToolResult.success(f"搜「{query}」看到 {len(hits)} 条：\n{body}")


def _parse_results(markup: str, limit: int) -> list[tuple[str, str, str]]:
    links = _RESULT_LINK.findall(markup)
    snippets = _RESULT_SNIPPET.findall(markup)
    hits: list[tuple[str, str, str]] = []
    for index, (href, raw_title) in enumerate(links[:limit]):
        title = _clean(raw_title)
        snippet = _clean(snippets[index]) if index < len(snippets) else ""
        if title:
            hits.append((title, snippet, _real_href(href)))
    return hits


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", unescape(fragment))).strip()


def _real_href(href: str) -> str:
    """DuckDuckGo 用 /l/?uddg=<原地址> 做跳转，还原成真实链接。"""
    if "uddg=" not in href:
        return unescape(href)
    query = parse_qs(urlparse(unquote(href)).query)
    origin = query.get("uddg") or []
    return unquote(origin[0]) if origin else unescape(href)
