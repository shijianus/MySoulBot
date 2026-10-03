#!/usr/bin/env bash
# MySoulBot 本地酒馆服务：常驻、优雅停止、平滑重启。
#
#   bash scripts/daemon.sh start        # 后台起来（默认 127.0.0.1:11555）
#   bash scripts/daemon.sh status       # 在不在、端口、积压多少条没落盘的记忆
#   bash scripts/daemon.sh logs         # 追服务日志
#   bash scripts/daemon.sh restart      # 平滑重启：等旧进程排空再起新的
#   bash scripts/daemon.sh stop         # 优雅停止（SIGTERM，最多等 GRACE 秒）
#
# 为什么不是 `kill -9`：记忆抽取是后台 fire-and-forget 的，硬杀会丢掉刚攒下的
# 那几事实。server 收到 SIGTERM 会先停止接单、等在途回合说完、再等队列排空——
# 这里只负责把信号发出去并等它自己走完，等不到才升级。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PYTHON_BIN=".venv/bin/python"; else PYTHON_BIN="$(command -v python3)"; fi
fi
[[ -x "$PYTHON_BIN" ]] || { echo "✗ 找不到可用的 python（设 PYTHON=/path/to/python）" >&2; exit 2; }

RUN_DIR="storage/run"
LOG_DIR="storage/logs"
PID_FILE="$RUN_DIR/server.pid"
OUT_FILE="$LOG_DIR/server.out"
GRACE="${GRACE:-60}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

alive() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; }

probe() {
  # 用引擎自己的解释器问 /healthz：不依赖机器上有没有 curl
  "$PYTHON_BIN" - "$@" <<'PY'
import json, sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=4) as response:
        print(json.dumps(json.loads(response.read()), ensure_ascii=False))
except Exception as exc:
    print(f"unreachable: {type(exc).__name__}")
    sys.exit(1)
PY
}

port_of() { "$PYTHON_BIN" -c "from config import get_settings; print(get_settings().server_port)" 2>/dev/null || echo 11555; }

cmd_start() {
  if alive; then echo "· 已经在跑了（pid $(cat "$PID_FILE")）"; return 0; fi
  local port; port="$(port_of)"
  setsid nohup "$PYTHON_BIN" server.py >>"$OUT_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  for _ in $(seq 1 40); do
    probe "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 && {
      echo "✓ 起来了（pid ${pid}） http://127.0.0.1:${port}/v1"; return 0; }
    alive || { echo "✗ 进程退了，最后几行：" >&2; tail -20 "$OUT_FILE" >&2; return 1; }
    sleep 0.25
  done
  echo "✗ ${port} 端口上没等到健康检查，看看 $OUT_FILE" >&2
  return 1
}

cmd_stop() {
  if ! alive; then echo "· 没在运行"; rm -f "$PID_FILE"; return 0; fi
  local pid; pid="$(cat "$PID_FILE")"
  kill -TERM "$pid"
  printf '· 已发 SIGTERM，等它把在途记忆落盘（最多 %ss）' "$GRACE"
  for _ in $(seq 1 "$((GRACE * 2))"); do
    kill -0 "$pid" 2>/dev/null || { echo "  ✓ 干净退出"; rm -f "$PID_FILE"; return 0; }
    sleep 0.5
  done
  echo ""
  echo "✗ ${GRACE}s 还没走完（可能有回合卡在模型上）。要真强行终止：kill -9 ${pid}" >&2
  echo "  现在强杀会丢掉还没落盘的记忆，所以这里不替你决定。" >&2
  return 1
}

cmd_status() {
  local port; port="$(port_of)"
  if ! alive; then echo "· 未运行"; return 1; fi
  echo "· pid $(cat "$PID_FILE") · 日志 $OUT_FILE"
  probe "http://127.0.0.1:${port}/healthz" || echo "· 进程在，但 /healthz 没答话"
}

cmd_logs() { tail -n "${LINES:-80}" -f "$OUT_FILE"; }

cmd_restart() {
  cmd_stop || true
  rm -f "$PID_FILE"
  cmd_start
}

case "${1:-status}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *) echo "用法：bash scripts/daemon.sh {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
