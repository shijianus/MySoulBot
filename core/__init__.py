"""MySoulBot 核心层。"""

from core.bot import BotError, MySoulBot, PersonaApplied, Session
from core.card_loader import (
    CardError,
    PersonaLibrary,
    Preset,
    PresetError,
    TavernCard,
    compile_soul,
    load_card_file,
    parse_card,
    slugify,
)
from core.clawd_soul import ClawdSoul
from core.memory_extractor import ExtractionOutcome, MemoryExtractor
from core.prompt_builder import PromptBuilder, PromptLayers
from core.storage_manager import (
    PathSafetyError,
    StorageError,
    StorageManager,
    normalize_fact,
    parse_facts,
    scan_size_gate,
)
from core.tools import ToolRegistry, ToolResult
from core.tools.protocol import StreamGuard, parse_directive

__all__ = [
    "BotError",
    "CardError",
    "ClawdSoul",
    "ExtractionOutcome",
    "MemoryExtractor",
    "MySoulBot",
    "PersonaApplied",
    "PersonaLibrary",
    "PathSafetyError",
    "Preset",
    "PresetError",
    "PromptBuilder",
    "PromptLayers",
    "Session",
    "StorageError",
    "StorageManager",
    "StreamGuard",
    "TavernCard",
    "ToolRegistry",
    "ToolResult",
    "compile_soul",
    "load_card_file",
    "normalize_fact",
    "parse_card",
    "parse_directive",
    "parse_facts",
    "scan_size_gate",
    "slugify",
]
