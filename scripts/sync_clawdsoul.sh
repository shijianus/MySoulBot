#!/usr/bin/env bash
# 把灵魂资产隔离同步到私有仓库 shijianus/ClawdSoul 的独立分支（默认 soul）。
#
#   bash scripts/sync_clawdsoul.sh --dry-run     # 只看会抄哪些，不写 git 不动远端
#   bash scripts/sync_clawdsoul.sh               # 抄 + 本地提交（不推送）
#   bash scripts/sync_clawdsoul.sh --push        # 推送（不强推；上游分叉就停下来报告）
#   bash scripts/sync_clawdsoul.sh --branch yexi --push
#
# 定时跑（裸机 cron，每天 04:10）：
#   10 4 * * * cd /home/developer/project/MySoulBot && bash scripts/sync_clawdsoul.sh --push >> storage/logs/soul-sync.cron.log 2>&1
#
# 逻辑在 core/soul_sync.py：逐轮日志、state.json、语音图片产物一律不带。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"; else PY="$(command -v python3)"; fi
fi
[[ -x "$PY" ]] || { echo "✗ 找不到可用的 python（设 PYTHON=/path/to/python）" >&2; exit 2; }

exec "$PY" -m core.soul_sync "$@"
