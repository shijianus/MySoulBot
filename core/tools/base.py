"""工具层的数据契约。

设计约束（对应「拟人化交互规范」）：

- 工具返回值 `ToolResult.content` 是**给模型看的人话**，不是状态码、不是 JSON 转储。
  模型拿到的是「我看完了，正文大概讲了……」这种可直接衔接进对话的材料。
- 工具失败也必须返回一句可用的话（`Tool.failure` 生成），让角色能自然地承认做不到，
  而不是把异常抛到界面上变成「HTTP 500」。
- 工具永远不直接对用户说话——它的输出只回到模型，由模型决定怎么说。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from config import Settings
from core.clawd_soul import ClawdSoul
from core.storage_manager import StorageManager

ARG_KEY: Final[re.Pattern[str]] = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("(?:[^"]*)"|\S+)""")


@dataclass
class ToolResult:
    """一次工具执行的结果。"""

    ok: bool
    content: str
    error: str = ""
    artifacts: list[Path] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **kwargs: Any) -> "ToolResult":
        return cls(ok=True, content=content.strip(), **kwargs)

    @classmethod
    def failure(cls, reason: str, *, say: str = "") -> "ToolResult":
        """失败也是一句人话：`reason` 给日志，`say` 给模型。"""
        text = say or f"（没办成：{reason}。不用解释机制，直接按你自己的方式说做不到就行。）"
        return cls(ok=False, content=text, error=reason)

    def digest(self, limit: int = 200) -> str:
        return re.sub(r"\s+", " ", self.content)[:limit]


class ToolError(RuntimeError):
    """工具自身的配置/参数错误，收敛成 ToolResult.failure 后不外抛。"""


@dataclass(frozen=True)
class ToolParam:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True


class Tool(ABC):
    """一个能力。子类只需要声明元信息并实现 `run`。"""

    name: str = ""
    description: str = ""
    hint: str = ""  # 一句话说给语境层：「能看网页(web_browse)」
    params: tuple[ToolParam, ...] = ()
    primary_arg: str = ""  # 行内暗号只给一个裸值时填到这个参数
    # 会造成外部动作或改写灵魂的工具：只认精确名字，不给别名、不给模糊匹配
    sensitive: bool = False

    @abstractmethod
    async def run(self, ctx: "ToolContext", args: dict[str, Any]) -> ToolResult: ...

    def available(self, ctx: "ToolContext") -> bool:  # noqa: ARG002 - 子类按配置判断
        return True

    def brief(self, ctx: "ToolContext") -> str:  # noqa: ARG002 - 子类按配置改口
        """说给语境层的那一句。后端只是占位时，这句必须实话实说。"""
        return self.hint

    # ------------------------------------------------------------ 声明
    def schema(self) -> dict[str, Any]:
        required = [p.name for p in self.params if p.required]
        return {
            "type": "object",
            "properties": {
                p.name: {"type": p.type, "description": p.description} for p in self.params
            },
            **({"required": required} if required else {}),
        }

    def native_spec(self) -> dict[str, Any]:
        """OpenAI function calling 的 tools[] 条目。"""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.schema()},
        }

    def usage_line(self) -> str:
        arg_text = " ".join(f"{p.name}<{p.type}>" for p in self.params if p.required)
        return f"{self.name} {arg_text}".strip()

    # ------------------------------------------------------------ 参数
    def coerce(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把接口送来的 JSON 参数按声明整理一遍，丢弃未知键。"""
        known = {p.name: p for p in self.params}
        out: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            param = known.get(key)
            if param is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if param.type == "integer":
                out[key] = _to_int(value)
            elif param.type == "number":
                out[key] = _to_float(value)
            elif param.type == "boolean":
                out[key] = str(value).strip().lower() in {"1", "true", "yes", "是"}
            else:
                out[key] = str(value).strip()
        return out

    def from_bare(self, text: str) -> dict[str, Any]:
        """行内暗号的简写形式：`⟦tool:web_browse https://x⟧` → 主参数。"""
        pairs = {m.group(1): m.group(2).strip('"') for m in ARG_KEY.finditer(text)}
        leftovers = ARG_KEY.sub("", text).strip()
        args = dict(pairs)
        if leftovers and self.primary_arg and self.primary_arg not in args:
            args[self.primary_arg] = leftovers
        return self.coerce(args)


@dataclass
class ToolContext:
    """工具运行需要的东西：配置、存储、当前用户、灵魂层。"""

    settings: Settings
    storage: StorageManager
    user_id: str
    clawd: ClawdSoul | None = None
    mood: Any = None  # noqa: ANN401 - core.mood_soul.MoodSoul，工具可选挂载

    def artifact_path(self, name: str) -> Path:
        path = self.storage.artifacts_dir(self.user_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def _to_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _to_float(value: object) -> float:
    try:
        return float(str(value))
    except ValueError:
        return 0.0
