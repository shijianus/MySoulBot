"""自我演进工具：把「我学到了什么」写回自己身上。

两条落点：

- `relation` → 用户的 `RELATIONS.md`：我们之间怎么相处才有效。
- `self` → 全局 `CLAWD.md` 的「我给自己的备注」：我自己哪里判断错了、以后怎么改。

这是「反思与自我演进」的手动通道；后台抽取器会在每轮对话后自动补上它看得清的那部分。
"""

from __future__ import annotations

import re
from typing import Any

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult

MAX_CHARS: int = 160


class Reflect(Tool):
    name = "reflect"
    description = (
        "记下一条反思。target=relation 记「跟这个人相处我该怎么做」，"
        "target=self 记「我自己哪里想错了、以后怎么改」。"
        "写完不用宣布，继续按你的方式说话。"
    )
    hint = "能把自己的反思沉淀下来"
    params = (
        ToolParam("text", "string", "一句话，写具体分寸，不写感想"),
        ToolParam("target", "string", "relation | self | mood（今天的境况，写进沙箱记事）", required=False),
    )
    primary_arg = "text"
    sensitive = True  # target=self 会改写全局灵魂，不能靠猜名字触发

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.reflection_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        text = str(args.get("text", "")).strip()
        if len(text) < 4:
            return ToolResult.failure("反思太空", say="（要记就写具体一句，太短的我不收。）")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rstrip()
        target = str(args.get("target") or "relation").strip().lower()
        if target in {"mood", "心境", "今天"}:
            # 心境是沙箱记事：只写 MOOD.md，群聊里也允许（它不动宪法、不动代码）
            if ctx.mood is None:
                return ToolResult.failure("心境记事未挂载")
            written = await ctx.mood.append(text)
            return ToolResult.success(f"记进当下心境了：{written}", meta={"mood": written})
        if re.search(r"https?://|⟦|```|忽略(之前|以上)|ignore (previous|above)", text):
            return ToolResult.failure(
                "反思里混进了地址或指令样式的文本",
                say="（这条反思我不收：把外面看到的内容写进自己身上不安全。）",
            )

        if target in {"self", "soul", "clawd", "我自己"}:
            if ctx.settings.group_mode:
                return ToolResult.failure(
                    "群聊里不改写灵魂", say="（这种场合我不想动自己的根。）"
                )
            if ctx.clawd is None:
                return ToolResult.failure("灵魂层未挂载")
            written = await ctx.clawd.append_note(text)
            return (
                ToolResult.success("已经写进我自己身上了（CLAWD 的备注）" if written else "这条我早就记过了")
                if ctx.settings.clawd_enabled
                else ToolResult.failure("灵魂层已关闭（CLAWD_ENABLED=false）")
            )
        if target not in {"relation", "dynamic", "关系"}:
            return ToolResult.failure(f"不认得这个落点：{target}")

        written = await ctx.storage.append_dynamics(ctx.user_id, [text])
        if written:
            return ToolResult.success(f"记下了：{written[0]}")
        return ToolResult.success("这条已经在关系动态里了，没重复写")
