"""仓库同步工具：把 `core.sync` 包成一个模型能使的劲。"""

from __future__ import annotations

from typing import Any

from config import PROJECT_ROOT
from core.tools.base import Tool, ToolContext, ToolParam, ToolResult


class GitSync(Tool):
    name = "git_sync"
    description = (
        "把灵魂与记忆文件同步到远端仓库。action=status 看一眼、backup=只提交不推送、"
        "push=提交并推送。不用向对方播报每一步，办完自然地在话里提一句就行。"
    )
    hint = "能把记忆推到远端备份"
    params = (
        ToolParam("action", "string", "status | backup | push", required=False),
        ToolParam("message", "string", "提交说明，可留空", required=False),
    )
    primary_arg = "action"
    sensitive = True

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "status").strip().lower()
        message = str(args.get("message") or "").strip()
        if action in {"status", "现在什么样", "查"}:
            return await self._status()
        if action not in {"backup", "push", "commit", "备份", "推"}:
            return ToolResult.failure(
                f"不认得这个动作：{action}", say="（同步只有 status / backup / push 三种，别的我不做。）"
            )
        push = action in {"push", "推"}
        from core.sync import run_sync  # 延迟导入：让 `python -m core.sync` 不带包级副作用

        try:
            report = await run_sync(
                ctx.settings,
                push=push,
                storage=ctx.storage,
                user_id=ctx.user_id,
                message=message,
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败也只是一句话
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if not report.ok:
            return ToolResult.failure(report.aborted)
        return ToolResult.success(
            f"同步完了。{report.human()}\n明细：" + "；".join(report.steps),
            meta={"committed": report.committed, "pushed": report.pushed},
        )

    async def _status(self) -> ToolResult:
        from core.sync import git  # 同上，工具层不在包初始化链路里

        dirty = await git(PROJECT_ROOT, "status", "--porcelain")
        if not dirty.ok:
            return ToolResult.failure(
                "这里还不是 git 仓库", say="（这个目录还没建仓库，先 init 才谈得上同步。）"
            )
        branch = await git(PROJECT_ROOT, "rev-parse", "--abbrev-ref", "HEAD")
        remote = await git(PROJECT_ROOT, "remote", "get-url", "origin")
        changed = [line for line in dirty.out.splitlines() if line.strip()]
        return ToolResult.success(
            f"分支 {branch.out.strip() or '（未知）'}；远端 {remote.out.strip() or '（还没登记）'}；"
            f"有改动的文件 {len(changed)} 个。"
        )
