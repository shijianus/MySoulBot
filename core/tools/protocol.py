"""流层守卫：把「机器的声音」挡在气泡之外。

三件事：

1. **行内暗号**：接口不支持原生 function calling 时，模型用 `⟦tool:name k=v⟧` 单独一行下单。
   这一行**永远不会进入显示流**——它被就地剥掉，交给调度器静默执行。
2. **状态泄漏**：模型偶尔仍会写出「正在调用工具…」「HTTP 404」「{"status": 200, ...}」这类话。
   整行是机器声就丢掉，并且**这一行余下的部分直到换行之前都继续丢**——
   超长 JSON 转储不能从观望上限那里漏出去。
3. **跨行残骸**：模型真的会把暗号写成两行。从 `⟦` 到 `⟧` 之间的所有文字一律吞掉，
   标记本身绝不外泄。

观望只发生在一行的**开头**：残留片段仍可能是暗号或机器声时先不发，等它露出破绽；
行中正文照旧逐字放行，所以打字机手感不受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

MARK_OPEN: Final[str] = "⟦"
MARK_CLOSE: Final[str] = "⟧"
_DIRECTIVE: Final[re.Pattern[str]] = re.compile(
    r"^⟦\s*(?:tool|工具)\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*?)\s*⟧\s*$", re.S
)
_SPAN: Final[re.Pattern[str]] = re.compile(
    r"⟦\s*(?:tool|工具)\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*?)⟧", re.S
)
# 整行匹配上就判定为机器声：这些不是角色扮演会说的话
_LEAK: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*```"),
    re.compile(r'^\s*[\{\[]\s*"'),
    re.compile(r'^\s*"?(status|status_code|http|error|traceback|tool_calls?|function)"?\s*[:=]', re.I),
    re.compile(r"^\s*(正在|即将|现在)?\s*(调用|使用|执行|运行)\s*(该|某个)?\s*(工具|函数|接口|api)", re.I),
    re.compile(r"^\s*(工具|接口|系统)\s*(返回|响应|输出)\s*[:：]?", re.I),
    re.compile(r"^\s*(loading|fetching|requesting|waiting for)\b", re.I),
    re.compile(r"^\s*(请求|检索|抓取|读取|生成)\s*(中|ing)", re.I),
    re.compile(r"^\s*HTTP\s*\d{3}\b", re.I),
)
# 行首片段与这些前缀互为前缀时，说明还不能断定是不是机器声，再多等一个字
_HOLD_PREFIX: Final[tuple[str, ...]] = (
    "⟦",
    "```",
    '{"',
    '["',
    "http",
    "status:",
    "status =",
    "status_code",
    "tool_call",
    "tool:",
    "function",
    "error:",
    "traceback",
    "正在调用",
    "正在使用",
    "即将调用",
    "执行工具",
    "调用工具",
    "使用工具",
    "接口返回",
    "工具返回",
    "系统返回",
    "请求中",
    "检索中",
    "抓取中",
    "loading",
    "fetching",
)
# 观望上限：超过它就必须做判定（放行或整行丢弃），不能无限期把正文扣着
HOLD_MAX: Final[int] = 400


@dataclass
class Directive:
    """一次行内下单。"""

    name: str
    raw_args: str
    line: str
    args: dict[str, Any] = field(default_factory=dict)


def parse_directive(line: str) -> Directive | None:
    """整行就是一个下单指令。"""
    stripped = line.strip()
    if not stripped.startswith(MARK_OPEN):
        return None
    match = _DIRECTIVE.match(stripped)
    return Directive(match.group(1), match.group(2).strip(), stripped) if match else None


def extract_directives(line: str) -> list[Directive]:
    """一行里夹着下单（模型把正文和暗号写在同一行）时也能摘出来。"""
    return [
        Directive(match.group(1), match.group(2).strip(), match.group(0))
        for match in _SPAN.finditer(line)
    ]


def is_mechanical_line(line: str) -> bool:
    """整行是不是机器声（状态码、JSON 转储、调用播报）。"""
    if not line.strip():
        return False
    return any(pattern.match(line) for pattern in _LEAK)


def _could_be_mechanical(fragment: str) -> bool:
    head = fragment.lstrip().lower()
    if not head:
        return False
    return any(head.startswith(p.lower()) or p.lower().startswith(head) for p in _HOLD_PREFIX)


def _opens_span(text: str) -> bool:
    return text.count(MARK_OPEN) > text.count(MARK_CLOSE)


def strip_markers(text: str, swallowed: list[str] | None = None) -> str:
    """剥掉 `⟦…⟧`；没有闭合标记的尾巴直接截断——标记本身绝不外泄。"""
    if MARK_OPEN not in text:
        return text
    sink = swallowed if swallowed is not None else []
    out: list[str] = []
    rest = text
    while MARK_OPEN in rest:
        before, _, rest = rest.partition(MARK_OPEN)
        out.append(before)
        if MARK_CLOSE in rest:
            span, _, rest = rest.partition(MARK_CLOSE)
            sink.append(f"{MARK_OPEN}{span}{MARK_CLOSE}")
        else:
            sink.append(MARK_OPEN + rest[:80])
            rest = ""
    out.append(rest)
    return "".join(out).rstrip()


class StreamGuard:
    """把增量文本切成「可显示」与「下单」两路。"""

    def __init__(self, *, hold: bool = True) -> None:
        self._pending = ""
        self._line_start = True
        self._hold = hold
        self._dropping = False  # 本行已判定为机器声，到换行之前继续丢
        self._in_span = False  # ⟦ 开了没关，跨行残骸继续吞
        self.swallowed: list[str] = []

    # ------------------------------------------------------------ 主入口
    def feed(self, delta: str) -> tuple[str, list[Directive]]:
        if not delta:
            return "", []

        if self._dropping:
            cut = delta.find("\n")
            if cut < 0:
                return "", []
            self._dropping = False
            self._line_start = True
            return self.feed(delta[cut + 1 :])

        if self._in_span:
            cut = delta.find(MARK_CLOSE)
            if cut < 0:
                return "", []
            self._in_span = False
            self._line_start = False
            return self.feed(delta[cut + 1 :])

        buffer = self._pending + delta
        out: list[str] = []
        directives: list[Directive] = []

        while True:
            index = buffer.find("\n")
            if index < 0:
                break
            line, buffer = buffer[:index], buffer[index + 1 :]
            shown, found = self._finish_line(line)
            out.append(shown)
            directives.extend(found)
            out.append("\n")
            self._line_start = True

        self._pending = ""

        unclosed = MARK_OPEN in buffer and MARK_CLOSE not in buffer
        if unclosed and len(buffer) < HOLD_MAX:
            self._pending = buffer  # 暗号写了一半，整段等住
            return "".join(out), directives
        if unclosed:
            # 长到不像话：⟦ 之前的照播，之后进入吞掉模式
            head, _, _ = buffer.partition(MARK_OPEN)
            self._in_span = True
            if head.strip():
                out.append(head.rstrip())
            return "".join(out), directives

        waiting = bool(buffer) and self._line_start and self._hold and _could_be_mechanical(buffer)
        if waiting and len(buffer) < HOLD_MAX:
            self._pending = buffer  # 行首可疑，再等一个字
            return "".join(out), directives
        if waiting and is_mechanical_line(buffer):
            # 观望到上限还判不出：像机器声就整行丢掉，余下的一起丢
            self.swallowed.append(buffer.strip()[:80])
            self._dropping = True
            return "".join(out), directives

        if buffer:
            mixed = extract_directives(buffer)
            if mixed:
                self.swallowed.extend(item.line for item in mixed)
                directives.extend(mixed)
            out.append(strip_markers(buffer, self.swallowed))
            self._line_start = False
        return "".join(out), directives

    def flush(self) -> tuple[str, list[Directive]]:
        """流结束：结算最后一段（可能没有换行）。"""
        if self._dropping or self._in_span:
            self._pending = ""
            return "", []
        buffer, self._pending = self._pending, ""
        if not buffer:
            return "", []
        if self._line_start or MARK_OPEN in buffer:
            return self._finish_line(buffer)
        return strip_markers(buffer, self.swallowed), []

    # ------------------------------------------------------------ 行处理
    def _finish_line(self, line: str) -> tuple[str, list[Directive]]:
        directive = parse_directive(line)
        if directive is not None:
            self.swallowed.append(directive.line)
            return "", [directive]
        mixed = extract_directives(line)
        if mixed:
            self.swallowed.extend(item.line for item in mixed)
            return strip_markers(line, []), mixed
        if is_mechanical_line(line):
            self.swallowed.append(line.strip())
            return "", []
        shown = strip_markers(line, self.swallowed)
        if _opens_span(line):
            self._in_span = True
        return shown, []
