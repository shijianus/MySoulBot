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
from core.panel import build_doc, build_status, build_timeline
from core.presence import Mood, Patience, Presence, build_presence, slot_for
from core.prompt_builder import PromptBuilder, PromptLayers
from core.rapport import Rapport, RapportEngine, parse_temperature, stage_for
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
from core.vision import ImageRef, VisionError, find_sources, ingest

__all__ = [
    "BotError",
    "CardError",
    "ClawdSoul",
    "ExtractionOutcome",
    "ImageRef",
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
    "Mood",
    "Patience",
    "Presence",
    "Rapport",
    "RapportEngine",
    "StorageError",
    "StorageManager",
    "StreamGuard",
    "TavernCard",
    "ToolRegistry",
    "ToolResult",
    "VisionError",
    "build_doc",
    "build_presence",
    "build_status",
    "build_timeline",
    "compile_soul",
    "find_sources",
    "ingest",
    "load_card_file",
    "normalize_fact",
    "parse_card",
    "parse_directive",
    "parse_facts",
    "scan_size_gate",
    "slugify",
    "parse_temperature",
    "slot_for",
    "stage_for",
]
