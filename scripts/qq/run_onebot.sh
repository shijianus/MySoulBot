#!/usr/bin/env bash
# 把裸机 QQ 协议端拉起来，并把登录二维码送到终端上（无头机器也能扫码）。
#
#   bash scripts/qq/run_onebot.sh                     # 用 setup_onebot.sh 记下的主程序
#   bash scripts/qq/run_onebot.sh --bin <路径>         # 现场指定主程序
#   bash scripts/qq/run_onebot.sh -- 它的 参数 --们     # 剩下的参数原样交给协议端
#   FORCE=1 bash scripts/qq/run_onebot.sh             # 引擎没在听也硬起（默认会拦一下）
#
# 它不下载、不装、不碰 Docker；只做三件事：确认引擎在等人、把进程拉在前台、
# 把它打印出来的登录链接转成终端二维码（装了 qrencode 才转，没装就把链接念给你）。
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PYTHON_BIN=".venv/bin/python"; else PYTHON_BIN="$(command -v python3)"; fi
fi
[[ -x "$PYTHON_BIN" ]] || { echo "✗ 找不到 python（设 PYTHON=/path/to/python）" >&2; exit 2; }

OUT_FILE="storage/logs/onebot.out"
mkdir -p storage/logs
FORCE="${FORCE:-0}"
BIN=""
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin) BIN="${2:?--bin 要给主程序路径}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "不认得这个参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

# setup_onebot.sh 记下的那一条是默认值；命令行上给了就以命令行为准。
# 在子 shell 里 source：那文件带着鉴权串，不该变成这个进程的环境变量去盖掉 .env
if [[ -z "$BIN" && -f onebot/state.env ]]; then
  BIN="$(. ./onebot/state.env; printf '%s' "${ONEBOT_BIN:-}")"
fi

if [[ -z "$BIN" || ! -x "$BIN" ]]; then
  echo "✗ 没找到可执行的协议端主程序${BIN:+（给了：$BIN）}。" >&2
  echo "  先跑：bash scripts/qq/setup_onebot.sh --llonebot <路径>，或现场指定 --bin <路径>" >&2
  exit 1
fi

# `|` 分字段：token 可能是空串，空格分隔的 read 会把空字段吞掉、后面全体错位
IFS='|' read -r WS_HOST WS_PORT WS_TOKEN < <(
  "$PYTHON_BIN" - <<'PY'
from config import get_settings

s = get_settings()
print("|".join(str(v) for v in (s.onebot_host, s.onebot_port, s.onebot_access_token or "")))
PY
)
if [[ "$WS_TOKEN" == *" "* ]]; then
  echo "✗ ONEBOT_ACCESS_TOKEN 里有空格，写进配置会把这一条切断——换成不带空格的串" >&2
  exit 1
fi

LISTENING=0
if "$PYTHON_BIN" - "$WS_HOST" "$WS_PORT" <<'PY'
import socket, sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1.5):
        pass
except OSError:
    sys.exit(1)
PY
then
  LISTENING=1
fi

if [[ "$LISTENING" == "1" ]]; then
  echo "· 反向 WS 目标 ws://${WS_HOST}:${WS_PORT} 有人接 ✓"
else
  echo "· ws://${WS_HOST}:${WS_PORT} 现在没人接。" >&2
  echo "  引擎得先起来：bash scripts/daemon.sh start（并确认 .env 里 ONEBOT_ENABLED=true）" >&2
  if [[ "$FORCE" != "1" ]]; then
    echo "  就是要先起协议端：FORCE=1 bash scripts/qq/run_onebot.sh" >&2
    exit 1
  fi
fi

echo "· 协议端：$BIN"
echo "· 日志：  $OUT_FILE（前台输出同时落这里）"
echo "· 扫码：  登录链接会打在这块终端上；装了 qrencode 就直接是二维码"
echo "  扫完码、QQ 上线之后，私聊她一句「在吗」就是第一次真实对话。"
echo ""

render_login() {
  # 协议端自己画二维码（一片 ANSI 方块）时原样放行；只把「裸 URL 那一行」变成二维码
  local line url
  while IFS= read -r line; do
    if [[ "$line" == *"://"* && "$line" != *"█"* && "$line" != *"▄"* && "$line" != *"[m"* ]]; then
      url="${line##* }"
      if command -v qrencode >/dev/null 2>&1; then
        printf '\n\033[1;33m▸ 用手机 QQ 扫这个码登录\033[0m\n'
        qrencode -t ANSIUTF8 "$url"
        continue
      fi
      printf '\n\033[1;33m▸ 没装 qrencode，把这个链接发到手机上打开登录：\033[0m\n  %s\n' "$url"
      continue
    fi
    printf '%s\n' "$line"
  done
}

STDBUF=()
command -v stdbuf >/dev/null 2>&1 && STDBUF=(stdbuf -oL -eL)
"${STDBUF[@]}" "$BIN" "${EXTRA[@]}" 2>&1 | tee -a "$OUT_FILE" | render_login
