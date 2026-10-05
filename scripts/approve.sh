#!/usr/bin/env bash
# 人类审批台：列出待确认工单、批准或驳回。
#
#   bash scripts/approve.sh                    # 列出所有工单
#   bash scripts/approve.sh AP-20261003-...    # 看一张的详情
#   bash scripts/approve.sh approve <id>       # 点头（只改台账，不代表引擎会自动去执行）
#   bash scripts/approve.sh deny <id> "理由"   # 驳回
#
# 为什么不在面板上点：面板按设计只读（有测试锁着 /api 的写方法一律 405），
# 给它开一个能批准的口子，等于把自己工程的执行权限挂在一个只读界面上，不值。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PYTHON_BIN=".venv/bin/python"; else PYTHON_BIN="$(command -v python3)"; fi
fi

"$PYTHON_BIN" - "$@" <<'PY'
import sys

from config import get_settings
from core.sandbox import ApprovalDesk

desk = ApprovalDesk(get_settings())
args = sys.argv[1:]

if not args:
    items = desk.list()
    pending = [item for item in items if item.state == "pending"]
    print(f"待确认 {len(pending)} 张 / 全部 {len(items)} 张")
    for item in pending:
        print("  " + item.human())
    if not pending:
        print("  （没有要人点头的事）")
    sys.exit(0)

if args[0] in {"approve", "deny"}:
    if len(args) < 2:
        sys.exit(f"用法：scripts/approve.sh {args[0]} <工单号> [理由]")
    decided = desk.decide(args[1], approve=args[0] == "approve", by="operator", note=" ".join(args[2:]))
    if decided is None:
        sys.exit(f"没有这张工单：{args[1]}")
    print(f"{decided.id} → {decided.state}（{decided.decided_by} · {decided.decided_at}）")
    print("  注意：台账只记「人点过头」。真正要做的那件事仍要由人执行，引擎不会替你删文件。")
    sys.exit(0)

found = [item for item in desk.list() if item.id == args[0]]
if not found:
    sys.exit(f"没有这张工单：{args[0]}")
item = found[0]
print(item.human())
print(f"  详情：{item.detail}")
sys.exit(0)
PY
