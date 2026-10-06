"""线路测速的核心：同一句话同时发给每条形线，分别量「多久落正文」。

配对口令、招呼语这类短产出吃的是延迟，不是模型多聪明。而 `.env` 里那行 ROUTES
看不出一条线是 2 秒落字、还是 40 秒只思考不落正文——只有真打一遍才知道。
这里量的就是这几件事，每次请求都不过几十个体。

三个概念别混：
  · **first**：正文第一个字出现的秒数（流式）。只思考不落正文的线路这里永远空。
  · **total**：整句说完的秒数（非流式，也就是 `ask_once` 实际用的那种问法）。
  · **thinking_only**：一路只有 reasoning 在动、正文一个字没来——
    这种线路能聊天（引擎那边有看门狗兜着），但绝不能拿去做短产出。

密钥不出这台机器：`Channel` 只接已经在 `.env`/`data:<路径>` 里的凭据，
输出里也绝不打印 key。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

from openai import AsyncOpenAI

__all__ = ["Channel", "probe_all", "render"]

# 「按指令给短句」的判据：超 15 字或夹带解释/候选，就不适合做口令
MAX_OK_CHARS: Final[int] = 15
_LEAK_MARKS: Final[tuple[str, ...]] = ("好的", "当然", "以下是", "比如", "：", "1.", "1、", "\n")


@dataclass(frozen=True)
class Channel:
    """一条要量的形线。`base` 可以带 /v1 也可以不带。"""

    name: str
    model: str
    base: str
    key: str
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # frozen dataclass 不能赋值，用 object.__setattr__ 收尾：
        # 配置文件里 key 常常带个换行，带着它去打就是 401，看着像线路坏了
        object.__setattr__(self, "key", (self.key or "").strip())
        object.__setattr__(self, "base", (self.base or "").strip().rstrip("/"))

    @property
    def v1(self) -> str:
        return self.base if self.base.endswith("/v1") else f"{self.base}/v1"

    @property
    def label(self) -> str:
        return f"{self.name}/{self.model}"

    def client(self, timeout: float) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=self.key or "EMPTY", base_url=self.v1,
                           timeout=timeout, max_retries=0)


async def _probe_once(channel: Channel, prompt: str, deadline: float) -> dict[str, Any]:
    """一条线跑一趟：先非流式量「整句多久」，再流式量「正文多久露头」。"""
    out: dict[str, Any] = {"channel": channel.label, "base": channel.v1}
    client = channel.client(deadline)

    started = time.perf_counter()
    try:
        done = await asyncio.wait_for(client.chat.completions.create(
            model=channel.model, messages=[{"role": "user", "content": prompt}],
            temperature=1.0, max_tokens=48, stream=False,
            extra_body=channel.extra or None), timeout=deadline)
        message = done.choices[0].message
        body = str(message.content or "").strip()
        out["total"] = round(time.perf_counter() - started, 2)
        out["body"] = body[:40]
        out["thinking"] = len(str(getattr(message, "reasoning_content", "") or ""))
        out["empty"] = not body
    except Exception as exc:  # noqa: BLE001 - 探路工具：什么错都照实记下来
        out["total"] = round(time.perf_counter() - started, 2)
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}".replace("\n", " ")

    started = time.perf_counter()
    first: float | None = None
    thought = 0
    streamed = ""
    try:
        stream = await client.chat.completions.create(
            model=channel.model, messages=[{"role": "user", "content": prompt}],
            temperature=1.0, max_tokens=48, stream=True, extra_body=channel.extra or None)
        async def walk() -> None:
            nonlocal first, thought, streamed
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue
                piece = str(getattr(delta, "content", "") or "")
                idea = str(getattr(delta, "reasoning_content", "") or "")
                if idea:
                    thought += len(idea)
                if piece:
                    streamed += piece
                    if first is None:
                        first = time.perf_counter() - started
        await asyncio.wait_for(walk(), timeout=deadline)
        out["first"] = round(first, 2) if first is not None else None
        out["streamed"] = round(time.perf_counter() - started, 2)
        out["thought"] = thought
        out["thinking_only"] = first is None and thought > 0
        if streamed:
            out.setdefault("body", streamed.strip()[:40])
    except Exception as exc:  # noqa: BLE001 - 流式不通也不影响这一行有读数
        out["stream_error"] = f"{type(exc).__name__}: {str(exc)[:90]}".replace("\n", " ")
        if first is not None:
            out["first"] = round(first, 2)
        out["thought"] = thought
    return out


async def probe_all(channels: list[Channel], prompt: str, *,
                    deadline: float = 12.0) -> list[dict[str, Any]]:
    """所有线同时问——错开问的话，慢的那条会把后面那条的读数一起拖脏。"""
    return await asyncio.gather(*[_probe_once(ch, prompt, deadline) for ch in channels])


def usable(row: dict[str, Any]) -> bool:
    """这条能不能拿来生成口令：非流式拿得到正文、且那句真的短。"""
    if row.get("error") or row.get("empty") or row.get("thinking_only"):
        return False
    body = str(row.get("body") or "")
    return bool(body) and len(body) <= MAX_OK_CHARS and not any(m in body for m in _LEAK_MARKS)


def render(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'线路':<26}{'正文露头':>10}{'整句':>9}{'思考':>7}  判定 / 那一趟说了什么"]
    for row in sorted(rows, key=lambda r: (r.get("first") is None, r.get("first") or 9e9)):
        first = f"{row['first']:.1f}s" if row.get("first") is not None else "—"
        total = f"{row['total']:.1f}s" if row.get("total") is not None else "—"
        thought = row.get("thought") or row.get("thinking") or 0
        if row.get("error"):
            verdict = f"✗ {row['error'][:70]}"
        elif row.get("thinking_only"):
            verdict = "✗ 只思考不落正文（不能做短产出）"
        elif row.get("empty"):
            verdict = "✗ 回了空"
        elif not usable(row):
            verdict = "△ 有正文但不合短句指令"
        else:
            verdict = "✓ 能做短产出"
        lines.append(f"{row['channel']:<26}{first:>10}{total:>9}{thought:>7}  {verdict}"
                     f"  {str(row.get('body') or '')[:24]!r}")
    return "\n".join(lines)
