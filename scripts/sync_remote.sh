#!/usr/bin/env bash
# 一键把灵魂与记忆资产备份到 shijianus/MySoulBot。
#
#   bash scripts/sync_remote.sh              # 体检 + 本地提交（不推）
#   bash scripts/sync_remote.sh --push       # 体检 + 提交 + 推送
#   bash scripts/sync_remote.sh --dry-run    # 只体检，不动任何东西
#   bash scripts/sync_remote.sh --user alice --push
#
# 真正的逻辑在 core/sync.py（凭据扫描、体积闸门、rebase 无冲突推送），
# 这个脚本只负责找到解释器并把参数原样转过去——两条路径同一套规则，不会走偏。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi

if [[ ! -x "$PY" ]]; then
  echo "✗ 找不到可用的 python（设 PYTHON=/path/to/python 再试）" >&2
  exit 2
fi

exec "$PY" -m core.sync "$@"
