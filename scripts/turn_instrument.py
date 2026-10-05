"""回合解剖：把「一句话进来 → 字出去」这一段拆到每一次上游调用、每一个阶段。

不是猜上游慢还是本地慢——直接记：
  · 这一回合到底打了几次上游（工具往返会把一趟变成两趟）
  · 每次请求的体积、是否挂 tools、max_tokens 是多少
  · 上游从发出到首个 delta / 首个**可见** delta / 收尾分别多久
  · 本地阶段各花多久：建立会话、取料、体温、装配提示词

    .venv/bin/python scripts/turn_instrument.py ["问句"] [回合数]
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from core.bot import MySoulBot  # noqa: E402
from core.card_loader import PersonaLibrary  # noqa: E402
from core.clawd_soul import ClawdSoul  # noqa: E402
from core.memory_extractor import MemoryExtractor  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

USER = "qq_group_950689514"


def size_of(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(part.get("text", ""))) for part in content)
        total += len(str(message.get("tool_calls") or ""))
    return total


class Spy:
    """替上游调用记账的客户端壳。行为完全透传，只在旁边掐表。"""

    def __init__(self, inner: Any, clock: list[float]) -> None:  # noqa: ANN401
        self._inner = inner
        self.calls: list[dict[str, Any]] = []
        self.packed_user = ""   # 最近一次真正递给模型的那句话（连发时该是好几句）
        self._clock = clock

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - 透传
        return getattr(self._inner, name)

    @property
    def chat(self) -> Any:  # noqa: ANN401
        outer = self

        class _Chat:
            @property
            def completions(self) -> Any:  # noqa: ANN401
                class _Completions:
                    async def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
                        t0 = time.perf_counter() - outer._clock[0]
                        stream = await outer._inner.chat.completions.create(**kwargs)
                        texts = [str(m.get("content")) for m in (kwargs.get("messages") or [])
                                 if m.get("role") == "user" and isinstance(m.get("content"), str)]
                        if texts:
                            outer.packed_user = texts[-1]
                        row = {
                            "at_s": round(t0, 2),
                            "model": kwargs.get("model"),
                            "max_tokens": kwargs.get("max_tokens"),
                            "tools": len(kwargs.get("tools") or []),
                            "messages": len(kwargs.get("messages") or []),
                            "prompt_chars": size_of(kwargs.get("messages") or []),
                            "first_delta_s": None,
                            "first_visible_s": None,
                            "end_s": None,
                            "think_chars": 0,
                            "visible_chars": 0,
                        }
                        outer.calls.append(row)

                        async def wrap(stream_inner: Any = stream) -> Any:  # noqa: ANN401
                            async for chunk in stream_inner:
                                now = time.perf_counter() - outer._clock[0]
                                choices = getattr(chunk, "choices", None) or []
                                if not choices:
                                    continue
                                delta = choices[0].delta
                                thought = getattr(delta, "reasoning_content", None) or ""
                                body = getattr(delta, "content", None) or ""
                                if thought:
                                    row["think_chars"] += len(thought)
                                    if row["first_delta_s"] is None:
                                        row["first_delta_s"] = round(now - t0, 2)
                                if body:
                                    row["visible_chars"] += len(body)
                                    if row["first_visible_s"] is None:
                                        row["first_visible_s"] = round(now - t0, 2)
                                yield chunk
                            row["end_s"] = round(time.perf_counter() - outer._clock[0] - t0, 2)

                        return wrap()

                return _Completions()

        return _Chat()

    async def close(self) -> None:
        await self._inner.close()


class Spies:
    """多线路之后的记账合集：每条线各一个壳，读的时候按时间轴并起来。"""

    def __init__(self, items: list[Spy]) -> None:
        self.items = items

    @property
    def calls(self) -> list[dict[str, Any]]:
        return sorted((call for spy in self.items for call in spy.calls),
                      key=lambda row: row.get("at_s") or 0.0)


def attach(bot: Any, clock: list[float]) -> Spies:  # noqa: ANN401
    """给引擎的**每条线路**各挂一个记账壳。

    不能再「一个壳管全部」：那等于把所有模型名都发往同一个域名，
    现场日志里就会看到 sol61 打到 GLM 那台，回退全变 503。
    """
    by_key: dict[str, Spy] = {}
    order: list[Spy] = []

    def hook(route: Any, real_client: Any) -> Any:  # noqa: ANN401
        key = route.client_id()
        spy = by_key.get(key)
        if spy is None:
            spy = Spy(real_client(route), clock)
            by_key[key] = spy
            order.append(spy)
        return spy

    bot._client_hook = hook  # noqa: SLF001 - 解剖自己家引擎的接管位
    return Spies(order)


async def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "@溟汐 今天累不累"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    root = Path(tempfile.mkdtemp(prefix="turn-anatomy-"))
    # 模板与预设得先搬进去，否则 ensure_user 第一步就没模板可读
    shutil.copytree(ROOT / "storage" / "templates", root / "templates")
    if (ROOT / "storage" / "presets").is_dir():
        shutil.copytree(ROOT / "storage" / "presets", root / "presets")
    # BG=on 让抽取与慢环照常打同一个网关；BG=off 只量「回合本身」——用来分清是不是自己人抢额度
    bg_on = os.environ.get("INSTRUMENT_BG", "off").lower() == "on"
    no_tools = os.environ.get("INSTRUMENT_TOOLS", "on").lower() == "off"
    settings = Settings(storage_dir=root, extractor_enabled=bg_on, cognition_enabled=bg_on,
                        tools_enabled=not no_tools, tts_provider="stub", image_provider="none")
    print(f"工具挂载：{'关' if no_tools else '开'}", flush=True)
    print(f"后台环：{'开' if bg_on else '关'}", flush=True)
    try:
        storage = StorageManager(settings)
        clawd = ClawdSoul(settings)
        prompts = PromptBuilder(settings, storage, clawd)
        extractor = MemoryExtractor(settings, storage)
        if extractor.enabled:
            extractor.start()
        bot = MySoulBot(settings, storage, prompts, extractor, PersonaLibrary(settings), clawd)

        clock = [time.perf_counter()]
        await bot.open_session(USER, restore=False)
        session = bot.session(USER)

        for turn in range(rounds):
            spy = attach(bot, clock)  # 每条线路各挂一个记账壳
            stage = {"open": 0.0, "ingest": 0.0, "pulse": 0.0, "build": 0.0}
            t0 = time.perf_counter()
            out = []
            async for delta in bot.stream_reply(USER, text, group_mode=True, external_origin=True):
                if not out:
                    stage["first_delta"] = round(time.perf_counter() - t0, 2)
                out.append(delta)
            stage["total"] = round(time.perf_counter() - t0, 2)
            print(f"\n=== 回合 {turn + 1} · 问题「{text}」")
            print(f"  上游调用次数 {len(spy.calls)}  · 本地首个可见 delta {stage.get('first_delta')}s · "
                  f"回合整体 {stage['total']}s · 正文 {sum(len(x) for x in out)} 字")
            for index, call in enumerate(spy.calls, 1):
                print(f"  [{index}] 发出 {call['at_s']}s · 请求 {call['prompt_chars']} 字 / "
                      f"{call['messages']} 条 / tools={call['tools']} · max_tokens={call['max_tokens']}")
                print(f"      首 delta {call['first_delta_s']}s · 首个可见字 {call['first_visible_s']}s · "
                      f"收尾 {call['end_s']}s · 思考 {call['think_chars']} 字 · 正文 {call['visible_chars']} 字")
            await asyncio.sleep(0.4)
        print("\n会话历史条数", len(session.history), "| 引擎读数", getattr(bot, "stats", None))
        await bot.aclose()
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
