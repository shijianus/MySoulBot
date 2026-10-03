"""工具生态：AI 能使的劲，以及「用了之后仍然像人」的规矩。

`voice` 是唯一一个**不**注册成 `Tool` 的能力：声音属于界面，不属于推理——
它一旦被播报给酒馆与 CLI，每一轮都要多跑一趟模型往返。
"""

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.git import GitSync
from core.tools.media import ImageGen, SeeImage, Snapshot
from core.tools.protocol import Directive, StreamGuard, parse_directive, strip_markers
from core.tools.registry import DEFAULT_TOOLS, ToolRegistry
from core.tools.soul import Reflect
from core.tools.webio import FetchError, fetch, html_to_text
from core.tools.web import WebBrowse, WebSearch
from core.tools.voice import (
    VoiceClip,
    VoiceError,
    VoiceProsody,
    audio_sniff,
    audio_url,
    prosody_for,
    provider_of,
    spoken_text,
    synthesize,
)

__all__ = [
    "DEFAULT_TOOLS",
    "Directive",
    "FetchError",
    "GitSync",
    "ImageGen",
    "Reflect",
    "SeeImage",
    "Snapshot",
    "StreamGuard",
    "Tool",
    "ToolContext",
    "ToolParam",
    "ToolRegistry",
    "ToolResult",
    "VoiceClip",
    "VoiceError",
    "VoiceProsody",
    "WebBrowse",
    "WebSearch",
    "audio_sniff",
    "audio_url",
    "fetch",
    "html_to_text",
    "parse_directive",
    "prosody_for",
    "provider_of",
    "spoken_text",
    "strip_markers",
    "synthesize",
]
