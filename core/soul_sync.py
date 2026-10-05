"""灵魂资产独立分支同步：把「她是谁」单独版本化到一个私有仓库，与主工程隔离。

与 `core.sync`（备份到主仓库）的分工：

- 主仓库 `shijianus/MySoulBot`：代码 + 可公开的记忆资产。
- 灵魂仓库 `shijianus/ClawdSoul`：`CLAWD.md`、心境记事 `MOOD.md`、人格与用户灵魂档案——
  推到**独立分支**（默认 `soul`），带私密运行记事，绝不与 main 混在一起。

三条纪律：

1. **不强推**。只用普通 push；上游有分叉就停下来报告，绝不 `--force`/`--force-with-lease`。
2. **凭据扫描照跑**。复用主同步那套扫描器，扫出密钥形状的内容就中止——灵魂仓库也是仓库。
3. **默认不推**。不带 `--push` 时只准备与体检，不动远端。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Final

from config import PROJECT_ROOT, Settings, get_settings
from core.storage_manager import StorageManager
from core.sync import GitRun, SyncReport, _scan_files, git

DEFAULT_REMOTE: Final[str] = "https://github.com/shijianus/ClawdSoul.git"
DEFAULT_BRANCH: Final[str] = "soul"

# 进灵魂仓库的东西：灵魂与人格档案，不含逐轮日志与运行态
_SOUL_FILES: Final[tuple[str, ...]] = ("CLAWD.md", "MOOD.md")
_USER_DOCS: Final[tuple[str, ...]] = ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "persona.json")
_USER_DIRS: Final[tuple[str, ...]] = ("presets",)

_README: Final[str] = """# ClawdSoul

MySoulBot 的灵魂资产独立仓库：深层灵魂宪法、当下心境、人格内核与每个对话空间的档案。

- 这一支 (`soul`) 由 `scripts/sync_clawdsoul.sh` 从主工程隔离推送，**不是代码**。
- 逐轮对话日志、`state.json`、语音与图片产物一律不进这里（那是运行垃圾，也是别人的隐私）。
- 主工程：`shijianus/MySoulBot`。
"""


def _staging_dir(settings: Settings) -> Path:
    return settings.storage_dir / "run" / "clawdsoul"


def _collect(settings: Settings, staging: Path) -> list[str]:
    """把灵魂资产抄进暂存区。逐轮日志与运行态一个字节都不带。"""
    copied: list[str] = []
    soul = settings.soul_dir
    for name in _SOUL_FILES:
        src = soul / name
        if src.is_file():
            dst = staging / "soul" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(f"soul/{name}")
    templates = settings.template_dir
    if templates.is_dir():
        for src in sorted(templates.glob("*.md")):
            dst = staging / "templates" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(f"templates/{src.name}")
    users = settings.users_dir
    if users.is_dir():
        for user in sorted(path for path in users.iterdir() if path.is_dir()):
            for name in _USER_DOCS:
                src = user / name
                if src.is_file():
                    dst = staging / "users" / user.name / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied.append(f"users/{user.name}/{name}")
            for folder in _USER_DIRS:
                src = user / folder
                if src.is_dir():
                    dst = staging / "users" / user.name / folder
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    copied.append(f"users/{user.name}/{folder}/")
    # 管理者那棵树单独收：它是另一棵，不是 users 下的一个子目录，
    # 漏了它就会出现「交互者的灵魂资产天天同步、管理者自己的反而不备」这种荒唐结果
    owner_root = settings.owner_dir
    if owner_root.is_dir():
        for user in sorted(path for path in owner_root.iterdir() if path.is_dir()):
            for name in _USER_DOCS:
                src = user / name
                if src.is_file():
                    dst = staging / "owner" / user.name / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied.append(f"owner/{user.name}/{name}")
    presets = settings.presets_dir
    if presets.is_dir():
        dst = staging / "presets"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(presets, dst)
        copied.append("presets/")
    return copied


def _ignore_file() -> str:
    return (
        "# 运行态与逐轮日志绝不进灵魂仓库\n"
        "logs/\nstate.json\nartifacts/\nbackups/\n*.bak-*\n*.lock\n.env\n*.key\n"
    )


async def run_soul_sync(
    settings: Settings,
    *,
    push: bool = False,
    remote_url: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    message: str = "",
) -> SyncReport:
    """抄灵魂资产 → 本地提交 → （要推才）推到独立分支。全程不强推。"""
    report = SyncReport()
    staging = _staging_dir(settings)
    staging.mkdir(parents=True, exist_ok=True)

    copied = _collect(settings, staging)
    (staging / ".gitignore").write_text(_ignore_file(), encoding="utf-8")
    if not (staging / "README.md").is_file():
        (staging / "README.md").write_text(_README, encoding="utf-8")
    report.steps.append(f"抄了 {len(copied)} 份灵魂资产")
    if not copied:
        report.aborted = "灵魂目录是空的，没什么可同步"
        report.ok = False
        return report

    if not (staging / ".git").is_dir():
        init = await git(staging, "init", "-q", ".")
        if not init.ok:
            report.aborted = f"暂存区 git init 失败：{init.err[:120]}"
            report.ok = False
            return report
        await git(staging, "branch", "-M", branch)
        report.steps.append(f"已建暂存仓库并命名为 {branch}")
    else:
        current = await git(staging, "rev-parse", "--abbrev-ref", "HEAD")
        if current.ok and current.out.strip() != branch:
            await git(staging, "checkout", "-B", branch)

    if not (staging / ".git").is_dir():
        report.aborted = "暂存仓库没起来，不动远端"
        report.ok = False
        return report

    existing = await git(staging, "remote", "get-url", "soul")
    if not existing.ok:
        await git(staging, "remote", "add", "soul", remote_url)
        report.steps.append(f"远端 soul = {remote_url}")
    else:
        await git(staging, "remote", "set-url", "soul", remote_url)

    if not (await git(staging, "config", "user.email")).out.strip():
        await git(staging, "config", "user.name", "mysoulbot-soul")
        await git(staging, "config", "user.email", "mysoulbot-soul@local")

    await git(staging, "add", "-A")
    staged = [line for line in (await git(staging, "diff", "--cached", "--name-only")).out.splitlines() if line]
    if staged:
        leaked = _scan_files(staging, settings, staged)
        if leaked:
            await git(staging, "reset", "-q")
            report.aborted = f"这些文件里有凭据形状的内容，我不提交：{leaked}"
            report.ok = False
            return report
        note = message or f"灵魂更新 {dt.date.today().isoformat()}（{len(staged)} 项）"
        committed = await git(staging, "commit", "-q", "-m", note)
        if not committed.ok:
            report.aborted = f"提交失败：{committed.err[:160]}"
            report.ok = False
            return report
        report.committed = True
        report.steps.append(f"已提交 {len(staged)} 项")
    else:
        report.steps.append("灵魂资产没有新变化")

    if not push:
        # 上一次只提交没推送时，本地可能已经领先远端——这里如实说一句，别让人以为万事大吉
        ahead_count = await git(staging, "rev-list", "--count", f"soul/{branch}..HEAD")
        waiting = ahead_count.out.strip() if ahead_count.ok else ""
        extra = f"，有 {waiting} 个提交还没推上去" if waiting and waiting != "0" else ""
        report.steps.append(f"未推送{extra}（要推：bash scripts/sync_clawdsoul.sh --push）")
        report.ok = True
        return report

    # 提交与推送解耦：没新内容也要把先前压着的提交推上去，否则 cron 会永远漏掉上一轮
    ahead = await git(staging, "push", "-u", "soul", branch)
    if not ahead.ok:
        # 上游分叉或没权限：停下来交给人，绝不强推
        report.aborted = f"推送没成（不强推，不动别人的提交）：{ahead.err[:200]}"
        report.ok = False
        return report
    report.pushed = True
    report.steps.append(f"已推送到 soul/{branch}")
    report.ok = True
    return report


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把灵魂资产隔离同步到 ClawdSoul 独立分支")
    parser.add_argument("--push", action="store_true", help="真推到远端（默认只本地提交）")
    parser.add_argument("--dry-run", action="store_true", help="只抄一遍并体检，不提交不推送")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="灵魂仓库地址")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="独立分支名")
    parser.add_argument("--message", default="", help="提交说明")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.dry_run:
        staging = Path(PROJECT_ROOT) / ".soul-dry"
        staging.mkdir(exist_ok=True)
        copied = _collect(settings, staging)
        print(f"dry-run：会抄 {len(copied)} 份灵魂资产（不写 git、不动远端）")
        for name in copied[:12]:
            print("  ·", name)
        shutil.rmtree(staging, ignore_errors=True)
        return 0

    report = await run_soul_sync(
        settings,
        push=args.push,
        remote_url=args.remote,
        branch=args.branch,
        message=args.message,
    )
    print(("✓ " if report.ok else "✗ ") + report.human())
    for step in report.steps:
        print("  ·", step)
    if not report.ok:
        print("  中止原因：" + report.aborted)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
