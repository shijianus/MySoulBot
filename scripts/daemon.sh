#!/usr/bin/env bash
# MySoulBot 本地酒馆服务：常驻、优雅停止、平滑重启。
#
#   bash scripts/daemon.sh start        # 后台起来（默认 127.0.0.1:11555）
#   bash scripts/daemon.sh status       # 在不在、端口、积压多少条没落盘的记忆、QQ 网桥的连接与心跳
#   bash scripts/daemon.sh logs         # 追服务日志
#   bash scripts/daemon.sh restart      # 平滑重启：等旧进程排空再起新的
#   bash scripts/daemon.sh stop         # 优雅停止（SIGTERM，最多等 GRACE 秒）
#
# QQ 那一侧的协议端（LLOneBot / NapCat）不是另一个进程管家：ONEBOT_ENABLED=true 时
# 网桥就长在这个服务里，反向 WS 监听 ONEBOT_PORT（默认 11556）。所以 status/healthz
# 一套读数就够，不再单独起一个 pidfile——两套进程互相不知道对方死活是最难查的故障。
# 裸机协议端自己的安装与扫码登录：bash scripts/qq/setup_onebot.sh。
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

onebot_line() {
  # 网桥的读数就藏在 /healthz 的 onebot 字段里：它不是独立进程，不该另开一个口子去问。
  # JSON 走参数不走管道——`python - <<PY` 已经把这个函数的 stdin 用作脚本本身了。
  "$PYTHON_BIN" - "$1" <<'PY'
import json, sys

try:
    health = json.loads(sys.argv[1] or "{}")
except Exception:
    print("· OneBot 网桥：/healthz 的读数读不出")
    sys.exit(0)
bridge = health.get("onebot") or {}
if not bridge.get("enabled"):
    print("· OneBot 网桥：没开（ONEBOT_ENABLED=false，QQ 进不来）")
    sys.exit(0)
bound = bridge.get("bound") or {}
counts = bridge.get("counts") or {}
age = bridge.get("heartbeat_seconds_ago")
heart = f"{age}s 前" if isinstance(age, (int, float)) else "还没收到心跳"
print(
    "· OneBot 网桥：ws://{}:{}  ·  协议端 {} 个  ·  心跳 {}  ·  对面在线 {}".format(
        bound.get("host", "?"), bound.get("port", "?"), bridge.get("connections", 0),
        heart, bridge.get("peer_online"),
    )
)
print(
    "  事件 {} · 回复 {} · 没点名不接 {} · 防刷屏挡下 {} · 重复 {} · 出错 {}".format(
        counts.get("events", 0), counts.get("replies", 0), counts.get("not_woken", 0),
        counts.get("flood", 0), counts.get("duplicate", 0), counts.get("errors", 0),
    )
)

def leg(view):
    # 只有样本才成句：刚起来的空服务不该报一排 None 装成读数
    if not isinstance(view, dict) or not view.get("n"):
        return ""
    return "均 {avg}ms / 最坏 {max}ms ×{n}".format(**view)

first = leg(bridge.get("turn_latency_ms", {}).get("first_bubble"))
whole = leg(bridge.get("turn_latency_ms", {}).get("turn_total"))
net = leg(bridge.get("qq_latency_ms", {}).get("send_private_msg")) or leg(
    bridge.get("qq_latency_ms", {}).get("send_group_msg"))
parts = [label + " " + text for label, text in (
    ("首字", first), ("说完", whole), ("QQ 来回", net)) if text]
if parts:
    print("  " + " · ".join(parts))

routes = (health.get("upstreams") or {}).get("routes") or []
if len(routes) > 1:
    print("  上游线路（谁快先打谁，坏了同回合换下一条）：")
    for row in routes:
        state = "冷却 {:.0f}s".format(row["cooling"]) if row.get("cooling") else "可打"
        speed = "{:.1f}s".format(row["first_visible_avg"]) if row.get("first_visible_avg") is not None else "没测过"
        print("    {:<8} {:<22} 优先级 {:>3} · 成 {} 败 {} 白等 {} · 首字 {} · {}".format(
            row["name"], row["model"], row["priority"], row["ok"], row["fail"],
            row["stalled"], speed, state))
PY
}

onebot_hint() {
  "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
from config import get_settings

settings = get_settings()
if not settings.onebot_enabled:
    print("· OneBot 网桥没开：协议端要接进来时把 .env 里 ONEBOT_ENABLED 改成 true 再 restart")
else:
    token = "已配（见 .env 的 ONEBOT_ACCESS_TOKEN）" if settings.onebot_access_token.strip() else "未配"
    print("· OneBot 网桥开着：协议端反向连 ws://{}:{}  ·  鉴权 {}".format(
        settings.onebot_host, settings.onebot_port, token))
PY
}

cmd_start() {
  if alive; then echo "· 已经在跑了（pid $(cat "$PID_FILE")）"; onebot_hint; return 0; fi
  local port; port="$(port_of)"
  setsid nohup "$PYTHON_BIN" server.py >>"$OUT_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  for _ in $(seq 1 40); do
    probe "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 && {
      echo "✓ 起来了（pid ${pid}） http://127.0.0.1:${port}/v1"; onebot_hint; return 0; }
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
  local body
  if ! body="$(probe "http://127.0.0.1:${port}/healthz")"; then
    echo "· 进程在，但 /healthz 没答话"
    return 1
  fi
  echo "$body"
  onebot_line "$body"
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
