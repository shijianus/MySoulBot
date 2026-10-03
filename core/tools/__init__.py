"""工具生态：AI 能使的劲，以及「用了之后仍然像人」的规矩。"""

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.git import GitSync
from core.tools.media import ImageGen, Snapshot
from core.tools.protocol import Directive, StreamGuard, parse_directive, strip_markers
from core.tools.registry import DEFAULT_TOOLS, ToolRegistry
from core.tools.soul import Reflect
from core.tools.webio import FetchError, fetch, html_to_text
from core.tools.web import WebBrowse, WebSearch

__all__ = [
    "DEFAULT_TOOLS",
    "Directive",
    "FetchError",
    "GitSync",
    "ImageGen",
    "Reflect",
    "Snapshot",
    "StreamGuard",
    "Tool",
    "ToolContext",
    "ToolParam",
    "ToolRegistry",
    "ToolResult",
    "WebBrowse",
    "WebSearch",
    "fetch",
    "html_to_text",
    "parse_directive",
    "strip_markers",
]
