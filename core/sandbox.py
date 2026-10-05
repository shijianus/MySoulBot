"""沙箱护栏：模型能往哪里落笔、哪些动作必须等人点头，全在这一个文件里说清楚。

四条规矩：

1. **系统本体不可自改**。代码、配置、脚本、提示词模板不是她的记事本；写入路径一旦越界就抛
   `SandboxError`，不解释、不通融。人格可以傲娇，护栏不能傲娇。
2. **危险动作只开工单，不动手**。删文件、执行命令、改底层配置这类事，引擎只登记一张
   「待人类确认」的工单，等人在命令行上 approve/deny。没有自动执行这条路径。
3. **沙箱内的记事可以自主**。`storage/soul/MOOD.md`（当下心境）与用户自己的灵魂资产是她能写的地方。
4. **另开一块草稿区给她干活**。`storage/sandbox/<user>/` 是她自己的临时工位：
   记事、算东西、放查来的材料都在这儿，进出都要过 `assert_scratch`，
   出不去这个目录，也就碰不到别人的目录和系统本体。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from config import PROJECT_ROOT, Settings

__all__ = [
    "SandboxError", "assert_writable", "assert_scratch", "scratch_dir",
    "Approval", "ApprovalDesk",
]

# 这些位置她一个字都不能改：程序、配置、脚本、提示词模板与宪法
_CORE_NAMES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?:^|/)CLAWD\.md$"),
    re.compile(r"^templates/"),
    re.compile(r"^run/approvals/"),
)
_CORE_GLOBAL: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^config\.py$"),
    re.compile(r"\.py$"),
    re.compile(r"\.sh$"),
    re.compile(r"^\.env(\.|$)"),
    re.compile(r"^scripts/"),
    re.compile(r"^tests/"),
    re.compile(r"^web/"),
    re.compile(r"^core/"),
    re.compile(r"^requirements[^/]*\.txt$"),
    re.compile(r"^\.(gitignore|gitattributes)$"),
    re.compile(r"^emoji/"),
)
# 沙箱内允许她落笔的位置（相对存储根）
_ALLOWED: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^data/users/[^/]+/.+\.md$"),
    re.compile(r"^soul/MOOD\.md$"),
)


class SandboxError(RuntimeError):
    """越界了：这个写入不在沙箱里。"""


def _project_rel(target: Path) -> str:
    try:
        return target.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return ""


def assert_writable(path: Path, *, storage_dir: Path | str | None = None) -> Path:
    """允许写入就返回解析后的路径，越界就抛 `SandboxError`。

    判序是硬的：先按黑名单挡掉系统本体与模板（藏在存储目录里也挡），
    再看它是否落在**存储根之内**的白名单上。基准取存储目录而不是项目根——
    测试与自定义部署的 storage 常在别处，硬写项目相对路径会把它们全误判成越界。
    """
    target = Path(path).resolve()
    base = Path(storage_dir).resolve() if storage_dir else (PROJECT_ROOT / "storage")
    rel = _project_rel(target)
    for pattern in _CORE_GLOBAL:
        if rel and pattern.search(rel):
            raise SandboxError(f"这一处不是她能改的：{rel}")
    try:
        inside = target.relative_to(base).as_posix()
    except ValueError:
        raise SandboxError(f"沙箱只圈在存储目录里：{target}") from None
    for pattern in _CORE_NAMES:
        if pattern.search(inside):
            raise SandboxError(f"这一处是护栏，不是记事本：{inside}")
    if not any(pattern.match(inside) for pattern in _ALLOWED):
        raise SandboxError(f"这块记事本没开放给她：{inside}")
    return target


# ---------------------------------------------------------------- 草稿工位
# 只圈死一个目录：她的草稿只能落在 storage/sandbox/<这个用户>/ 里。
# 名字里带 .. 或者绝对路径一律挡在 resolve 之前——先解析再比，符号链接也跑不掉。
_SCRATCH_NAME = re.compile(r"[^\w.\-一-鿿]")


def scratch_dir(settings: Settings, user_id: str) -> Path:
    """这个用户的草稿工位。目录名收过一遍，不让 `..` 或 `/` 混进来。"""
    safe = _SCRATCH_NAME.sub("_", (user_id or "guest").strip()) or "guest"
    return settings.storage_dir / "sandbox" / safe[:64]


def assert_scratch(path: Path, *, settings: Settings, user_id: str) -> Path:
    """草稿区内的路径就返回解析结果，越界就抛 `SandboxError`。

    工位是**独立**的一道闸，不走 `assert_writable` 那套灵魂资产白名单：
    她能在这儿放查来的材料、算一半的数，但这不代表她能写自己的 SOUL.md。
    """
    root = scratch_dir(settings, user_id).resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise SandboxError(f"草稿只能放在自己的工位里：{target}") from None
    if target == root:
        raise SandboxError("工位本身不是文件")
    return target


@dataclass
class Approval:
    """一张待人类确认的工单。引擎只写这张纸，不代做那件事。"""

    id: str
    action: str
    detail: str
    requested_by: str
    created_at: str
    state: str = "pending"
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""

    def human(self) -> str:
        who = f"（{self.requested_by} 申请）" if self.requested_by else ""
        return f"{self.id} · {self.action}{who} · {self.detail} · {self.state}"


@dataclass
class ApprovalDesk:
    """工单台账：`storage/run/approvals/<id>.json`，一次一张，人点了才算数。"""

    settings: Settings

    @property
    def directory(self) -> Path:
        return self.settings.storage_dir / "run" / "approvals"

    def request(self, action: str, detail: str, *, requested_by: str = "") -> Approval:
        """登记一张工单并返回它。**这里没有任何执行**——执行只发生在人类 approve 之后。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        approval = Approval(
            id=f"AP-{stamp}-{len(self.list()) + 1:03d}",
            action=action,
            detail=detail,
            requested_by=requested_by,
            created_at=stamp,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{approval.id}.json"
        path.write_text(json.dumps(asdict(approval), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return approval

    def _read(self) -> list[Approval]:
        if not self.directory.is_dir():
            return []
        items: list[Approval] = []
        for path in sorted(self.directory.glob("AP-*.json")):
            try:
                raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            known = {f for f in Approval.__dataclass_fields__}
            items.append(Approval(**{key: value for key, value in raw.items() if key in known}))
        return items

    def list(self, *, state: str | None = None) -> list[Approval]:
        items = self._read()
        return [item for item in items if state is None or item.state == state]

    def decide(self, approval_id: str, *, approve: bool, by: str = "", note: str = "") -> Approval | None:
        """人工裁决。引擎与工具一律不许调这条路径的 approve——那是人的手。"""
        path = self.directory / f"{approval_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        raw["state"] = "approved" if approve else "denied"
        raw["decided_by"] = by
        raw["decided_at"] = time.strftime("%Y%m%d-%H%M%S")
        raw["note"] = note
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        known = {f for f in Approval.__dataclass_fields__}
        return Approval(**{key: value for key, value in raw.items() if key in known})
