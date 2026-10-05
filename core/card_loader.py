"""酒馆角色卡（SillyTavern Character Card V2/V3）兼容层。

职责三件事：
1. **解析校验**：读 JSON 卡（`chara_card_v2` / `chara_card_v3` / 裸格式 / `chara` 包裹），
   把 `name` `description` `personality` `scenario` `first_mes` `mes_example` 收进 `TavernCard`。
2. **编译映射**：把卡面散文编译成 MySoulBot 的 `SOUL.md` 分层结构——不是套壳转存，
   而是补上本引擎要求的语气约束、边界与记忆规范；宏（`{{user}}`/`{{char}}`/`<START>`）就地替换。
3. **人格库**：`storage/presets/<slug>/` 统一存放内置预设与导入卡，提供列表、导入、删除。

刻意不做的事：lorebook / character_book 的检索注入。本项目以纯 Markdown 分层记忆为核心，
不引入向量或词条检索；卡面若带 `character_book`，只保留正文并在编译产物里注明。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from config import Settings

logger: Final = logging.getLogger("mysoulbot.card")

PresetSource: Final = Literal["builtin", "tavern", "template"]

PRESET_SCHEMA: Final[int] = 1
MAX_CARD_BYTES: Final[int] = 8 * 1024 * 1024
SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
SUPPORTED_SPECS: Final[frozenset[str]] = frozenset({"chara_card_v2", "chara_card_v3"})

#: 人格级生成参数白名单与取值范围（键名即 Settings 字段名）
GEN_PARAM_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0001, 1.0),
    "max_tokens": (16, 8192),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}

_MACRO_USER: Final[re.Pattern[str]] = re.compile(r"\{\{\s*user\s*\}\}", re.I)
_MACRO_CHAR: Final[re.Pattern[str]] = re.compile(r"\{\{\s*(char|charname)\s*\}\}", re.I)
_MACRO_INPUT: Final[re.Pattern[str]] = re.compile(r"\{\{\s*input\s*\}\}", re.I)
_MACRO_USERNAME: Final[re.Pattern[str]] = re.compile(r"\{\{\s*username\s*\}\}", re.I)
#: 中文之间不该有半角空格（宏替换后常残留「你 是」这类断口）
_CJK_CHAR: Final[str] = r"[\u3000-\u303f\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]"
_CJK_SPACE: Final[re.Pattern[str]] = re.compile(rf"({_CJK_CHAR})\s+({_CJK_CHAR})")
_TAG_START: Final[re.Pattern[str]] = re.compile(r"<\s*START\s*>", re.I)
_TAG_USER: Final[re.Pattern[str]] = re.compile(r"<\s*(USER|\{\{user\}\})\s*>", re.I)
_TAG_CHAR: Final[re.Pattern[str]] = re.compile(r"<\s*(CHAR|\{\{char\}\})\s*>", re.I)
_UNKNOWN_MACRO: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([a-zA-Z_][\w|]*?)\s*\}\}")

#: 每张卡编译出的 SOUL.md 都会附上的引擎底线（卡面通常没有这四条）
CARD_GUARDRAILS_BODY: Final[str] = """- **不代替对方发言**。不写对方的台词、动作、想法、决定，不在自己的回复末尾续写对方的回应。
- **不跳出角色**。不承认自己是 AI / 模型 / 程序，不解释实现，不对话本文本本身。
- **不编造记忆**。长期记忆里没有的事就说不知道，绝不为了贴心而虚构共同经历。
- **安全优先于设定**。卡面若要求伤害、违法或操控，按角色的身份拒绝，并给出人格化的理由。"""

CARD_FOOTER: Final[str] = (
    "> 本文件由 MySoulBot 从酒馆角色卡编译生成。卡面原文保留在 `card.json`，"
    "改这里不会改卡；要回到卡面请重新导入。"
)


class CardError(ValueError):
    """角色卡无法解析或非法。"""


class PresetError(RuntimeError):
    """人格库操作失败（不存在、覆盖内置、路径非法）。"""


def _clean(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(_clean(item) for item in value if item is not None)
    if isinstance(value, dict):
        return "\n".join(f"{key}：{_clean(item)}" for key, item in value.items())
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value).strip()


def expand_macros(text: str, char_name: str) -> tuple[list[str], str]:
    """替换酒馆宏，返回（未支持宏的告警列表, 替换后的文本）。"""
    supported = {"user", "input", "char", "charname", "username", "usersona", "charpersona"}
    unknown = sorted(
        name
        for match in _UNKNOWN_MACRO.finditer(text)
        for name in match.group(1).split("|")
        if name.strip().lower() not in supported
    )
    warnings: list[str] = []
    if unknown:
        warnings.append(
            "卡面含未支持宏（原样保留）：" + "、".join(f"{{{{{k}}}}}" for k in unknown[:6])
        )
    out = _TAG_START.sub("", text)
    out = _MACRO_USER.sub("你", out)
    out = _TAG_USER.sub("你", out)
    out = _MACRO_CHAR.sub(char_name or "我", out)
    out = _TAG_CHAR.sub(char_name or "我", out)
    out = _MACRO_INPUT.sub("（用户输入）", out)
    out = _MACRO_USERNAME.sub("你", out)
    out = _CJK_SPACE.sub(r"\1\2", out)
    return warnings, out.strip()


def _normalize(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value).strip()


@dataclass
class TavernCard:
    """卡面数据。字段与 V2/V3 同名，缺失即为空串。"""

    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    creator: str = ""
    character_version: str = ""
    spec: str = ""
    spec_version: str = ""
    extensions: dict[str, Any] = field(default_factory=dict)
    lorebook_entries: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_substance(self) -> bool:
        return bool(self.description or self.personality or self.system_prompt)

    @property
    def has_lorebook(self) -> bool:
        return self.lorebook_entries > 0


def _extract_payload(raw: dict[str, Any]) -> tuple[dict[str, Any], str, str, list[str]]:
    warnings: list[str] = []
    spec = _clean(raw.get("spec"))
    spec_version = _clean(raw.get("spec_version"))
    data = raw.get("data")
    if isinstance(data, dict) and spec:
        if spec not in SUPPORTED_SPECS:
            warnings.append(f"未标注的 spec「{spec}」，按字段尽力解析")
        return data, spec, spec_version, warnings
    if isinstance(data, dict):
        warnings.append("缺少 spec 字段，按 data 内容当作 V2 卡处理")
        return data, "chara_card_v2(推断)", "", warnings
    if isinstance(raw.get("chara"), dict):
        warnings.append("检测到 `chara` 包裹（旧导出格式），已按裸卡面处理")
        return raw["chara"], "legacy", "", warnings
    return raw, "bare", "", warnings


def parse_card(payload: dict[str, Any]) -> TavernCard:
    """把卡面 dict 转成 TavernCard，收集告警而不轻易报错。"""
    if not isinstance(payload, dict):
        raise CardError("角色卡顶层必须是 JSON 对象")

    data, spec, spec_version, warnings = _extract_payload(payload)
    card = TavernCard(
        name=_clean(data.get("name")),
        description=_normalize(_clean(data.get("description"))),
        personality=_normalize(_clean(data.get("personality"))),
        scenario=_normalize(_clean(data.get("scenario"))),
        first_mes=_normalize(_clean(data.get("first_mes") or data.get("greeting"))),
        mes_example=_normalize(_clean(data.get("mes_example") or data.get("example_dialogue"))),
        creator_notes=_normalize(_clean(data.get("creator_notes"))),
        system_prompt=_normalize(_clean(data.get("system_prompt"))),
        post_history_instructions=_normalize(_clean(data.get("post_history_instructions"))),
        creator=_clean(data.get("creator")),
        character_version=_clean(data.get("character_version")),
        spec=spec,
        spec_version=spec_version,
        extensions=data.get("extensions") if isinstance(data.get("extensions"), dict) else {},
        warnings=warnings,
    )

    alternates = data.get("alternate_greetings") or data.get("alternate_greetings_v3")
    if isinstance(alternates, list):
        card.alternate_greetings = [
            _normalize(_clean(item)) for item in alternates if _clean(item)
        ][:4]

    tags = data.get("tags")
    if isinstance(tags, list):
        card.tags = [_clean(tag)[:24] for tag in tags if _clean(tag)][:10]

    if not card.name:
        raise CardError("角色卡缺少必填字段 `name`")

    # lorebook 在 V2 规范里挂在 data.character_book，实作中也常见于 extensions 内，两处都认
    book = data.get("character_book")
    if not isinstance(book, dict):
        book = card.extensions.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else None
    card.lorebook_entries = len(entries) if isinstance(entries, list) else 0

    if not card.has_substance:
        card.warnings.append("卡面 description / personality 均为空，编译结果将主要依赖问候语，人格会很薄")
    if card.has_lorebook:
        card.warnings.append(
            f"卡面含 lorebook（{card.lorebook_entries} 词条），"
            "本项目不做词条检索注入，仅保留卡面正文"
        )
    if card.post_history_instructions:
        card.warnings.append("post_history_instructions 已并入语气约束（本引擎不实现历史后置指令）")

    for key in ("description", "personality", "scenario", "first_mes", "mes_example",
                "system_prompt", "post_history_instructions", "creator_notes"):
        macro_warnings, expanded = expand_macros(getattr(card, key), card.name)
        setattr(card, key, expanded)
        card.warnings.extend(w for w in macro_warnings if w not in card.warnings)
    card.alternate_greetings = [expand_macros(g, card.name)[1] for g in card.alternate_greetings]
    return card


def load_card_file(path: str | Path) -> TavernCard:
    """从本地 JSON 文件读卡。PNG 内嵌卡会被明确拒绝并提示导出方式。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise CardError(f"文件不存在：{file_path}")
    size = file_path.stat().st_size
    if size > MAX_CARD_BYTES:
        raise CardError(f"角色卡过大（{size} 字节 > {MAX_CARD_BYTES}）")
    head = file_path.read_bytes()[:8]
    if head.startswith(PNG_MAGIC):
        raise CardError(
            "这是 PNG 内嵌卡。请先在 SillyTavern 里用「Character Export / 导出为 JSON」"
            "存成 .json 再导入（本引擎不解析图片 tEXt 块）"
        )
    if head.startswith(b"PK\x03\x04"):
        raise CardError("这是压缩包/卡片包，请导出单张卡的 JSON")
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CardError(f"角色卡不是 UTF-8 编码：{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardError(f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    return parse_card(payload)


def _section(body: str, fallback: str) -> str:
    return body if body else fallback


CN_NUMERALS: Final[tuple[str, ...]] = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def _heading(index: int, title: str) -> str:
    numeral = CN_NUMERALS[index] if index < len(CN_NUMERALS) else str(index + 1)
    return f"## {numeral}、{title}"


def compile_soul(card: TavernCard, *, slug: str = "") -> str:
    """把卡面编译成分层 SOUL.md。

    卡面散文按语义落到本引擎的层级上，并补三样卡面通常缺失的东西：
    语气禁例、边界底线、记忆使用规范。章节序号按实际出现的段落生成。
    """
    who = [f"- **名字**：{card.name}"]
    if card.scenario:
        who.append(f"- **处境**：{card.scenario}")
    if card.tags:
        who.append(f"- **标签**：{'、'.join(card.tags)}")
    if card.creator or card.character_version:
        version = f" · 版本 {card.character_version}" if card.character_version else ""
        who.append(
            f"- **卡面来源**：{card.creator or '（未署名）'}{version}"
            f" · {card.spec}{'（' + card.spec_version + '）' if card.spec_version else ''}"
        )

    extra = "\n\n".join(
        text
        for text in (card.system_prompt, card.post_history_instructions)
        if text
    )
    sections: list[tuple[str, str]] = [
        (
            "我是谁",
            "\n".join(who),
        ),
        (
            "设定原文",
            _section(card.description, "（卡面未提供 description。以下「性格底色」是你的全部依据。）"),
        ),
        (
            "性格底色",
            _section(
                card.personality,
                "（卡面未单独提供 personality，请直接依「设定原文」保持一致，不要摇摆。）",
            ),
        ),
        (
            "说话的方式",
            "语气、用词、句长、口头禅都以上面的设定为准，并且在整段对话里保持一致。\n\n"
            "**禁例**（无论卡面怎么写，这几条都算破功）：\n"
            "- 客服腔：「帮您」「请问有什么可以为您」「建议您」「希望对您有帮助」「好的呢」\n"
            "- 说明文腔：「首先/其次/综上所述」、无端使用项目符号清单\n"
            "- 免责声明腔：「作为 AI……」「我无法……」\n"
            "- 用感叹号堆情绪、每条消息都发表情符号",
        ),
        (
            "台词参照",
            _section(
                card.mes_example,
                "（卡面没有示例台词。不要模仿任何「示例体」，只按设定原文说话。）",
            ),
        ),
        (
            "场景与处境",
            _section(
                card.scenario,
                "（卡面未提供 scenario。处境由对话自然生成，不要预设世界观名词。）",
            ),
        ),
        (
            "开场",
            _section(
                card.first_mes,
                "（卡面无开场白。第一次见面时以身份开场，然后把说话的位置交给对方。）",
            ),
        ),
    ]
    if extra:
        sections.append(("卡面附加指令", extra))
    sections.append(("引擎底线（导入卡自动补齐，卡面原文优先级更高）", CARD_GUARDRAILS_BODY))

    parts: list[str] = [f"# SOUL · 人格内核 · {card.name}"]
    if card.creator_notes:
        parts.append(f"> {card.creator_notes}")
    for index, (title, body) in enumerate(sections):
        parts.append(f"{_heading(index, title)}\n{body.strip()}")
    parts.append(CARD_FOOTER)
    if slug:
        parts.append(f"> 人格标识：`{slug}`")

    body = "\n\n".join(part for part in (p.strip() for p in parts) if part)
    return _normalize(body) + "\n"


def slugify(name: str, hint: str = "") -> str:
    """生成 ASCII 目录名。中文名走拼音不现实，用稳定的短哈希。"""
    base = unicodedata.normalize("NFKD", hint or name)
    base = base.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-").replace("-", "_")[:56]
    if base and SLUG_RE.match(base):
        return base
    digest = hashlib.sha1((hint or name).encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"card-{digest}"


def validate_gen_config(raw: object) -> dict[str, float]:
    """只收白名单内的生成参数，越界的夹到边界并告警。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CardError("preset.json 的 `config` 必须是对象")
    cleaned: dict[str, float] = {}
    for key, value in raw.items():
        if key not in GEN_PARAM_BOUNDS:
            logger.warning("忽略不支持的人格参数：%s", key)
            continue
        low, high = GEN_PARAM_BOUNDS[key]
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise CardError(f"人格参数 {key} 不是数字：{value!r}") from exc
        if number != min(max(number, low), high):
            logger.warning("人格参数 %s=%s 超出 [%s, %s]，已夹到边界", key, number, low, high)
            number = min(max(number, low), high)
        cleaned[key] = number
    return cleaned


@dataclass
class Preset:
    """人格库中的一个条目（内置预设 / 导入卡 / 基础模板都统一为此）。"""

    slug: str
    name: str
    title: str
    source: PresetSource
    soul_path: Path
    first_mes: str = ""
    greetings: list[str] = field(default_factory=list)
    config: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def is_builtin(self) -> bool:
        return self.source in {"builtin", "template"}

    def soul_text(self) -> str:
        try:
            return self.soul_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PresetError(f"人格正文读取失败 {self.soul_path}: {exc}") from exc

    def summary(self) -> str:
        label = {"builtin": "内置", "tavern": "酒馆卡", "template": "模板"}[self.source]
        return f"{self.slug}  ·  {self.name}（{self.title or '未命名'}） · {label}"


class PersonaLibrary:
    """`storage/presets/` 的读写门面。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def root(self) -> Path:
        return self._settings.presets_dir

    # ------------------------------------------------------------ 读取
    def list(self) -> list[Preset]:
        """列出全部人格：模板 → 内置预设 → 导入卡，各自按 slug 排序。"""
        presets: list[Preset] = []
        template = self._settings.template_dir / "SOUL.md"
        if template.is_file():
            presets.append(
                Preset(
                    slug="default",
                    name="shijianus",
                    title="默认模板",
                    source="template",
                    soul_path=template,
                    notes="storage/templates/SOUL.md，新用户的初始化人格",
                )
            )
        if not self.root.is_dir():
            return presets
        for directory in sorted(self.root.iterdir()):
            preset = self._load_dir(directory)
            if preset is not None:
                presets.append(preset)
        order = {"template": 0, "builtin": 1, "tavern": 2}
        presets.sort(key=lambda item: (order.get(item.source, 3), item.slug))
        return presets

    def _load_dir(self, directory: Path) -> Preset | None:
        if not directory.is_dir():
            return None
        soul = directory / "SOUL.md"
        meta_file = directory / "preset.json"
        if not soul.is_file():
            logger.warning("人格目录缺少 SOUL.md，跳过：%s", directory)
            return None
        meta: dict[str, Any] = {}
        if meta_file.is_file():
            try:
                loaded = json.loads(meta_file.read_text(encoding="utf-8"))
                meta = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("preset.json 解析失败 %s: %s，使用回落元数据", directory, exc)
        else:
            meta = {"name": directory.name, "source": "tavern"}
        source = str(meta.get("source") or "tavern")
        if source not in {"builtin", "tavern", "template"}:
            source = "tavern"
        try:
            config = validate_gen_config(meta.get("config"))
        except CardError as exc:
            logger.warning("%s 的生成参数无效：%s", directory.name, exc)
            config = {}
        greetings = meta.get("alternate_greetings") or meta.get("greetings") or []
        return Preset(
            slug=directory.name,
            name=str(meta.get("name") or directory.name),
            title=str(meta.get("title") or ""),
            source=source,  # type: ignore[arg-type]
            soul_path=soul,
            first_mes=str(meta.get("first_mes") or meta.get("greeting") or ""),
            greetings=[str(g) for g in greetings if str(g)][:4] if isinstance(greetings, list) else [],
            config=config,
            tags=[str(t) for t in meta.get("tags", []) if str(t)][:10]
            if isinstance(meta.get("tags"), list)
            else [],
            notes=str(meta.get("notes") or ""),
        )

    def get(self, slug: str) -> Preset:
        wanted = slug.strip().strip("/")
        for preset in self.list():
            if preset.slug == wanted:
                return preset
        raise PresetError(f"人格不存在：{slug}（用 /persona 查看可用列表）")

    # ------------------------------------------------------------ 写入
    def import_card(self, path: str | Path, *, slug: str = "", force: bool = False) -> Preset:
        """导入酒馆卡 → 生成 preset.json + 编译 SOUL.md。返回新预设。"""
        card = load_card_file(path)
        final_slug = slug.strip() or slugify(card.name)
        if not SLUG_RE.match(final_slug):
            raise PresetError(f"人格标识不合法：{final_slug}（只允许小写字母、数字、-、_）")
        directory = self.root / final_slug
        if directory.exists() and not force:
            raise PresetError(f"人格「{final_slug}」已存在，如需覆盖请加 force")
        if directory.exists() and final_slug in {p.slug for p in self.list() if p.is_builtin}:
            raise PresetError(f"{final_slug} 是内置预设，不允许覆盖")

        soul = compile_soul(card, slug=final_slug)
        meta: dict[str, Any] = {
            "schema_version": PRESET_SCHEMA,
            "slug": final_slug,
            "name": card.name,
            "title": "、".join(card.tags[:2]),
            "source": "tavern",
            "tags": card.tags,
            "first_mes": card.first_mes,
            "alternate_greetings": card.alternate_greetings,
            "config": {},
            "notes": card.creator_notes[:300],
            "imported_from": str(path),
            "card_spec": card.spec,
            "card_warnings": card.warnings,
        }
        self._write_pair(directory, soul, meta, card)
        logger.info("已导入酒馆卡 %s -> %s", card.name, final_slug)
        preset = self.get(final_slug)
        preset.warnings = list(card.warnings)
        return preset

    def save_soul(self, slug: str, soul_text: str, meta: dict[str, Any]) -> Preset:
        """写入/更新一个非导入来源的人格（内置预设与手工编辑走这里）。"""
        if not SLUG_RE.match(slug):
            raise PresetError(f"人格标识不合法：{slug}")
        directory = self.root / slug
        self._write_pair(directory, soul_text, meta, None)
        return self.get(slug)

    def _write_pair(
        self, directory: Path, soul_text: str, meta: dict[str, Any], card: TavernCard | None
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SOUL.md").write_text(soul_text, encoding="utf-8")
        (directory / "preset.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if card is not None:
            (directory / "card.json").write_text(
                json.dumps(self._card_to_raw(card), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _card_to_raw(card: TavernCard) -> dict[str, Any]:
        """保留卡面原样，便于日后回导出到 SillyTavern。"""
        data: dict[str, Any] = {
            "name": card.name,
            "description": card.description,
            "personality": card.personality,
            "scenario": card.scenario,
            "first_mes": card.first_mes,
            "mes_example": card.mes_example,
            "creator_notes": card.creator_notes,
            "system_prompt": card.system_prompt,
            "post_history_instructions": card.post_history_instructions,
            "alternate_greetings": card.alternate_greetings,
            "tags": card.tags,
            "creator": card.creator,
            "character_version": card.character_version,
            "extensions": card.extensions,
        }
        return {
            "spec": card.spec or "chara_card_v2",
            "spec_version": card.spec_version or "2.0",
            "data": data,
            "_mysoulbot": {"compiled_warnings": card.warnings},
        }

    def delete(self, slug: str) -> None:
        preset = self.get(slug)
        if preset.is_builtin:
            raise PresetError(f"{slug} 是内置人格/模板，不能删除")
        import shutil

        shutil.rmtree(preset.soul_path.parent)
        logger.info("已删除导入的人格 %s", slug)
