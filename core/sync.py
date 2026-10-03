"""记忆资产的一键远端同步（默认推到 shijianus/MySoulBot）。

为什么不用「git add . && git commit && git push」一把梭：

1. **密钥必须挡在提交之前**。`.env` 里有真实 API_KEY。这里显式检查它有没有被 ignore，
   并且扫描待提交内容里是否混进了配置的 key 字面量或 `sk-…` 形态的串；命中就整次中止。
2. **体积闸门**。GitHub 单文件 100MB 是硬墙，撞上去会在仓库历史里留下清不掉的对象。
   超过 `GIT_SAFE_FILE_BYTES`（默认 20MB）就中止本次同步并点名文件——
   不做「先把别的推上去」，那会让人误以为已经备份好了。
3. **不制造冲突，也不强推**。推送前 `pull --rebase --autostash`；一旦 rebase 冲突就
   `rebase --abort`，尽量把 autostash 弹回工作区，再如实报告。绝不 `--force`。
4. **origin 只认配置里那一个**。已存在的 origin 与配置不一致就停手：
   个人记忆不能被推到一个没核对过的地址。
5. **先瘦身再提交**。滚动归档日志、下沉超限的记忆条目到 `archive/`，让推上去的东西一直是小的。
6. **`--dry-run` 真的什么都不动**：不 init、不 add、不归档、不提交，只体检。

所有 git 调用都是 argv 列表（不走 shell，不存在拼接注入），`stdin=DEVNULL` 且
`GIT_TERMINAL_PROMPT=0`：不让 git 抢终端要口令，也不在无人值守时挂死。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from config import PROJECT_ROOT, Settings, get_settings
from core.storage_manager import StorageManager, scan_size_gate

logger: Final = logging.getLogger("mysoulbot.sync")

_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # 这三类本身就是密钥形状，不依赖上下文
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # 第四类要求「引号包住的字面量」：源码里 `api_key=self._settings.api_key` 是变量传递，
    # 不是泄漏；`api_key="sk-..."` 才是。少了这个约束，任何 Python 项目都同步不动。
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"),
)
# 演示串、占位值、以及从环境变量取值的写法都不算泄漏
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"(?i)^(empty|none|null|<[^>]*>|\S*x{4,}\S*|your[-_\w]*|changeme|placeholder|sk-x+)$"
)
_CODE_EXPRESSION: Final[re.Pattern[str]] = re.compile(
    r"(?i)(os\.|getenv|environ|self\.|settings\.|config\.|\$\{|\{|\.(get|strip|format)\()"
)
_CREDENTIAL_KEYS: Final[tuple[str, ...]] = ("api_key", "extractor_api_key")
_SAFE_IGNORES: Final[tuple[str, ...]] = (".env",)
_GIT_TIMEOUT: Final[float] = 60.0
_SCAN_MAX_BYTES: Final[int] = 2_000_000
_AUTOSTASH: Final[str] = "autostash"
FALLBACK_EMAIL: Final[str] = "soul@mysoulbot.local"
FALLBACK_NAME: Final[str] = "MySoulBot"


class SyncError(RuntimeError):
    """同步中止，原因已经说清楚。"""


@dataclass
class SyncReport:
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    committed: bool = False
    pushed: bool = False
    skipped: list[str] = field(default_factory=list)
    aborted: str = ""

    def human(self) -> str:
        if self.aborted:
            return f"没同步成：{self.aborted}"
        parts = [f"{len(self.steps)} 步", f"提交{'了' if self.committed else '无新增'}"]
        parts.append(f"推送{'了' if self.pushed else '没推'}")
        if self.skipped:
            parts.append("超限文件：" + "、".join(self.skipped))
        return " · ".join(parts)


@dataclass
class GitRun:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


async def git(root: Path, *args: str, timeout: float = _GIT_TIMEOUT) -> GitRun:
    """唯一的 git 出口：argv 列表、无 shell、不接 stdin、有超时。"""
    if shutil.which("git") is None:
        raise SyncError("这台机器上没有 git 命令")
    try:
        done = await asyncio.to_thread(_spawn, ["git", *args], root, timeout)
    except subprocess.TimeoutExpired:
        return GitRun(124, f"git {' '.join(args)} 超时（大概在建连接或等凭据）")
    return GitRun(done.returncode, (done.stdout or "") + (done.stderr or ""))


def _spawn(argv: list[str], cwd: Path, timeout: float) -> Any:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", LC_ALL="C.UTF-8")
    return subprocess.run(  # noqa: S603 - argv 固定、无 shell，参数来自配置或白名单
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        env=env,
    )


async def run_sync(
    settings: Settings,
    *,
    push: bool = False,
    dry_run: bool = False,
    message: str = "",
    storage: StorageManager | None = None,
    user_id: str = "",
    root: Path | None = None,
) -> SyncReport:
    """执行一次同步。`dry_run` 只检查不写；`push` 才真的往远端推。"""
    report = SyncReport()
    root = root or PROJECT_ROOT
    branch = settings.sync_remote_branch
    remote = settings.sync_remote_url

    if (await git(root, "rev-parse", "--is-inside-work-tree")).ok:
        report.steps.append("已定位本地仓库")
    else:
        if dry_run:
            report.ok = False
            report.aborted = "这里还不是 git 仓库（dry-run 不会替你 init）"
            return report
        await git(root, "init", "-q", ".")
        await git(root, "branch", "-M", branch)
        report.steps.append(f"已 git init 并把主分支命名为 {branch}")

    # 提交身份：容器里常常没有 global user.email，缺了就给仓库本地配一个，不碰全局
    if not dry_run:
        identity = await git(root, "config", "user.email")
        if not identity.out.strip():
            await git(root, "config", "user.email", FALLBACK_EMAIL)
            await git(root, "config", "user.name", FALLBACK_NAME)
            report.steps.append(f"提交身份回落为 {FALLBACK_NAME} <{FALLBACK_EMAIL}>")

    # 忽略规则必须在场，否则 .env 会被带进提交
    for name in _SAFE_IGNORES:
        if (await git(root, "check-ignore", "-q", "--", name)).ok:
            continue
        report.ok = False
        report.aborted = f"{name} 没有被 .gitignore 挡住，我先不提交（里面可能有密钥）"
        return report
    report.steps.append(".env 已被 ignore")

    if dry_run:
        return await _health_check(settings, root, branch, report)

    # 1) 先瘦身：滚动归档日志、把超限的记忆条目下沉（只碰已存在的用户目录）
    await _slim_memory(storage, user_id, report)

    status = await git(root, "status", "--porcelain")
    if status.out.strip():
        add = await git(root, "add", "-A")
        if not add.ok:
            report.ok = False
            report.aborted = f"git add 失败：{add.out.strip()[:200]}"
            return report
        report.steps.append("已暂存变更")
    else:
        report.steps.append("工作区干净，无需提交")

    staged = await _staged_paths(root)

    # 2) 体积闸门：命中就整次中止，不做「先把别的推上去」
    limit = settings.git_safe_file_bytes
    offenders = [(path, size) for path, size in _sizes(root, staged) if size > limit]
    if offenders:
        for path, _ in offenders:
            await git(root, "restore", "--staged", "--", str(path))
        report.skipped = [f"{path}({_mb(size)})" for path, size in offenders]
        report.ok = False
        report.aborted = (
            f"{len(offenders)} 个文件超过闸门 {_mb(limit)}，已退出暂存，本次不提交。"
            "要么 /panel archive 瘦身，要么把它写进 .gitignore"
        )
        return report

    # 3) 密钥扫描：命中就整次中止，不做「只提交一部分」的侥幸
    leak = await _scan_staged_secrets(root, settings)
    if leak:
        report.ok = False
        report.aborted = f"暂存内容里疑似有凭据（{leak}），我停在这里"
        return report
    report.steps.append("凭据扫描通过")

    # 4) 提交（无暂存内容时 git commit 会失败，先判空）
    if staged:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        text = message.strip() or f"soul: 灵魂与记忆同步 {stamp}"
        commit = await git(root, "commit", "-q", "-m", text)
        if not commit.ok:
            report.ok = False
            report.aborted = f"commit 失败：{commit.out.strip()[:220]}"
            return report
        report.committed = True
        report.steps.append("已提交")

    if not push:
        report.steps.append("未推送（要推：/sync remote）")
        return report

    # 5) 远端：没有就登记，有但不是配置里那一个就停手
    remote_probe = await git(root, "remote", "get-url", "origin")
    if not remote_probe.ok:
        await git(root, "remote", "add", "origin", remote)
        report.steps.append(f"已登记 origin = {remote}")
    elif not _same_remote(remote_probe.out, remote):
        report.ok = False
        report.aborted = (
            f"origin 已指向 {remote_probe.out.strip()}，与配置的 {remote} 不一致。"
            "个人记忆不往没核对过的地址推：确认后 git remote set-url origin ..."
        )
        return report

    return await _push_aligned(root, branch, report)


async def _push_aligned(root: Path, branch: str, report: SyncReport) -> SyncReport:
    """先与远端对齐（rebase），再推。接不上就退回原状并如实说。"""
    check = await git(root, "ls-remote", "--heads", "origin", branch)
    if check.ok and check.out.strip():
        rebase = await git(root, "pull", "--rebase", "--autostash", "origin", branch)
        if not rebase.ok:
            await git(root, "rebase", "--abort")
            restored = await _restore_autostash(root)
            report.ok = False
            report.aborted = (
                "远端和本地有分歧，rebase 没自动接上，我已退回原状（不做强制操作）。"
                + ("autostash 已弹回工作区。" if restored else "若有 autostash 留在 stash 里，请 git stash list 自行确认。")
                + f" 详情：{rebase.out.strip()[-180:]}"
            )
            return report
        report.steps.append("已与远端对齐")
    else:
        report.steps.append("远端还没有这个分支，直接推")

    pushed = await git(root, "push", "-u", "origin", branch)
    if not pushed.ok:
        report.ok = False
        report.aborted = f"push 失败：{pushed.out.strip()[-240:]}"
        return report
    report.pushed = True
    report.steps.append(f"已推送到 {branch}")
    return report


async def _restore_autostash(root: Path) -> bool:
    """`--autostash` 在 rebase 失败时会把改动留在 stash 里，这里尽量弹回工作区。"""
    listing = await git(root, "stash", "list")
    if not listing.ok or _AUTOSTASH not in listing.out:
        return False
    token = listing.out.splitlines()[0].split(":")[0]
    return (await git(root, "stash", "pop", token)).ok


async def _slim_memory(
    storage: StorageManager | None, user_id: str, report: SyncReport
) -> None:
    if storage is None or not user_id:
        return
    try:
        exists = storage.user_dir(user_id).is_dir()
    except Exception as exc:  # noqa: BLE001 - 非法 id 不该阻断同步
        logger.warning("跳过归档：用户目录不可用（%s）", exc)
        return
    if not exists:
        return
    rotation = await storage.rotate_transcripts(user_id)
    if rotation["archived"]:
        report.steps.append(f"归档 {len(rotation['archived'])} 份日志")
    for doc in ("MEMORY", "RELATIONS"):
        moved = await storage.compact_memory(user_id, doc=doc)
        if moved:
            report.steps.append(f"{doc}.md 下沉 {moved} 条到归档")


async def _health_check(
    settings: Settings, root: Path, branch: str, report: SyncReport
) -> SyncReport:
    """`--dry-run`：只看体积与凭据，不 add、不 commit、不归档。"""
    tracked = await git(root, "ls-files")
    dirty = await git(root, "status", "--porcelain", "-uall")
    candidates = sorted(
        {path for line in dirty.out.splitlines() if (path := _clean_path(line))}
        | {path for path in tracked.out.split() if path},
        key=str,
    )
    limit = settings.git_safe_file_bytes
    offenders = [(path, size) for path, size in _sizes(root, candidates) if size > limit]
    report.skipped = [f"{path}({_mb(size)})" for path, size in offenders]
    report.steps.append(f"待提交候选 {len(candidates)} 个（分支 {branch}）")
    if offenders:
        report.ok = False
        report.aborted = f"{len(offenders)} 个文件超过闸门 {_mb(limit)}：请瘦身或写进 .gitignore"
        return report
    leak = _scan_files(root, settings, candidates)
    if leak:
        report.ok = False
        report.aborted = f"工作区里疑似有凭据（{leak}）"
        return report
    report.steps.append("体积闸门与凭据扫描都干净（dry-run 未提交、未推送）")
    return report


def _clean_path(line: str) -> str:
    """`?? path` / ` M path` / `A  path` → path。"""
    return line[3:].strip() if len(line) > 3 else ""


async def _staged_paths(root: Path) -> list[str]:
    run = await git(root, "diff", "--cached", "--name-only")
    return [line for line in run.out.splitlines() if line.strip()]


def _sizes(root: Path, paths: list[str]) -> list[tuple[Path, int]]:
    found: list[tuple[Path, int]] = []
    for text in paths:
        path = root / text
        try:
            if path.is_file():
                found.append((path, path.stat().st_size))
        except OSError:
            continue
    return found


async def _scan_staged_secrets(root: Path, settings: Settings) -> str:
    if not await _staged_paths(root):
        return ""
    return _scan_text((await git(root, "diff", "--cached", "-U0")).out, settings)


def _scan_files(root: Path, settings: Settings, paths: Sequence[str]) -> str:
    """dry-run 用：直接读工作区里的文本候选，逐个扫凭据。"""
    for text in paths:
        path = root / text
        try:
            if path.stat().st_size > _SCAN_MAX_BYTES:
                continue
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hit = _scan_text(body, settings)
        if hit:
            return f"{text}：{hit}"
    return ""


def _scan_text(body: str, settings: Settings) -> str:
    """配置的 key 字面量 + 常见凭据形态。命中返回描述，没命中返回空串。"""
    if not body:
        return ""
    for label in _CREDENTIAL_KEYS:
        key = str(getattr(settings, label, "") or "").strip()
        if len(key) >= 8 and not _is_placeholder(key) and key in body:
            return f"命中配置里的 {label} 字面量"
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(body):
            value = match.group(2) if match.re.groups >= 2 else match.group(0)
            if _is_placeholder(value) or _CODE_EXPRESSION.search(match.group(0)):
                continue
            return f"疑似凭据片段 {value[:10]}…"
    return ""


def _is_placeholder(value: str) -> bool:
    """`EMPTY`、`sk-xxxx`、`<待填>`、短于 8 位的值不算泄漏。"""
    cleaned = (value or "").strip().strip("\"'")
    if len(cleaned) < 8:
        return True
    return bool(_PLACEHOLDER.match(cleaned))


def _same_remote(current: str, wanted: str) -> bool:
    def normalize(url: str) -> str:
        return url.strip().removesuffix(".git").removeprefix("https://").removeprefix("git@")

    return bool(wanted.strip()) and normalize(current) == normalize(wanted)


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


def oversized_report(settings: Settings, root: Path | None = None) -> list[str]:
    """本地视角的超限清单（同步之前先自查，也供 /panel status 用）。"""
    root = root or PROJECT_ROOT
    return [
        f"{path.relative_to(root)} {_size_of(size)}"
        for path, size in scan_size_gate(root, settings.git_safe_file_bytes)
    ]


def _size_of(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}GB"


# ---------------------------------------------------------------- 命令行入口
def _cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.sync",
        description="把灵魂与记忆资产同步到远端仓库（默认 shijianus/MySoulBot）",
    )
    parser.add_argument("--push", action="store_true", help="提交后推到远端")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="只体检：忽略规则、体积闸门、凭据扫描"
    )
    parser.add_argument("--user", default="", help="顺带为该用户滚动归档日志、下沉超限记忆")
    parser.add_argument("--message", default="", help="提交说明")
    parser.add_argument("--list-oversized", dest="list_oversized", action="store_true", help="只列超限文件")
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.apply_logging()
    if args.list_oversized:
        found = oversized_report(settings)
        for line in found:
            print(f"  {line}")
        print(f"超限文件 {len(found)} 个（闸门 {_mb(settings.git_safe_file_bytes)}）")
        return 1 if found else 0

    report = asyncio.run(
        run_sync(
            settings,
            push=args.push,
            dry_run=args.dry_run,
            message=args.message,
            storage=None if args.dry_run else StorageManager(settings),
            user_id=args.user,
        )
    )
    for step in report.steps:
        print(f"  · {step}")
    for skipped in report.skipped:
        print(f"  ! 超限：{skipped}")
    if not report.ok:
        print(f"✗ {report.aborted}", file=sys.stderr)
        return 1
    print(f"✓ {report.human()}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
