#!/usr/bin/env bash
# 裸机 QQ 协议端（LLOneBot CLI / NapCat）的配置脚手架。
#
#   bash scripts/qq/setup_onebot.sh                      # 探测 + 生成配置 + 体检
#   bash scripts/qq/setup_onebot.sh --llonebot <路径>     # 指定协议端主程序（不下载）
#   bash scripts/qq/setup_onebot.sh --napcat <路径>      # 同上，NapCat 那一侧
#   bash scripts/qq/setup_onebot.sh --merge <配置文件>    # 把反向 WS 那一条并进协议端自己的配置
#   bash scripts/qq/setup_onebot.sh --dry-run            # 只说要看什么、会写什么，一个字都不落盘
#
# 三条纪律写在这儿：
# 1. **不下载、不解包、不装任何东西**。要装哪个版本由你决定，脚本只接你给的路径。
# 2. **不碰 Docker**。这套是裸机跑的，协议端也裸机跑；脚本里不会出现一行 docker 命令。
# 3. **不冒充已知格式**。它生成的是「反向 WS 该连哪里」这一条事实，以及一份可并用的片段；
#    协议端自己的完整配置该由它自己写，`--merge` 只在你要的时候动那个文件，且先备份。
set -euo pipefail
cd "$(dirname "$0")/../.."

RUN_DIR="storage/run"
WORK_DIR="onebot"
STATE_FILE="$WORK_DIR/state.env"
DRY=0
LLONEBOT=""
NAPCAT=""
MERGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --llonebot) LLONEBOT="${2:?--llonebot 要给主程序路径}"; shift 2 ;;
    --napcat) NAPCAT="${2:?--napcat 要给主程序路径}"; shift 2 ;;
    --merge) MERGE="${2:?--merge 要给协议端配置文件路径}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "不认得这个参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PYTHON_BIN=".venv/bin/python"; else PYTHON_BIN="$(command -v python3)"; fi
fi
[[ -x "$PYTHON_BIN" ]] || { echo "✗ 找不到 python（设 PYTHON=/path/to/python）" >&2; exit 2; }

arch="$(uname -m)"
[[ "$arch" == "aarch64" || "$arch" == "arm64" ]] \
  && echo "· 架构：${arch}（协议端要挑 linux-arm64 那一份主程序）" \
  || echo "· 架构：${arch}（不是 aarch64，下面按本机架构自己核对主程序）"

# ---------------------------------------------------------------- 引擎侧的读数
# 用 `|` 分字段：token 可以是空串，空格分隔的 read 会把空字段直接吞掉、后面全体错位
IFS='|' read -r ONEBOT_ENABLED ONEBOT_HOST ONEBOT_PORT ONEBOT_TOKEN SERVER_HOST SERVER_PORT < <(
  "$PYTHON_BIN" - <<'PY'
from config import get_settings

s = get_settings()
print("|".join(str(v) for v in (
    "true" if s.onebot_enabled else "false", s.onebot_host, s.onebot_port,
    s.onebot_access_token or "", s.server_host, s.server_port,
)))
PY
)
WS_URL="ws://${ONEBOT_HOST}:${ONEBOT_PORT}"
echo "· 引擎：OneBot 网桥 ${ONEBOT_ENABLED} · 反向 WS 听 ${WS_URL} · 鉴权 $([[ -n "$ONEBOT_TOKEN" ]] && echo 已配 || echo 未配)"
if [[ "$ONEBOT_ENABLED" != "true" ]]; then
  echo "  ⚠ .env 里 ONEBOT_ENABLED=false ——现在这个端口上没人听。改 true 再 bash scripts/daemon.sh restart。"
fi
if [[ -z "$ONEBOT_TOKEN" ]]; then
  echo "  ⚠ 没配 ONEBOT_ACCESS_TOKEN：网桥只会绑回环（它是这么规定的），协议端也得连本机地址。"
fi

# ---------------------------------------------------------------- 目录初始化
if [[ "$DRY" == "0" ]]; then
  mkdir -p "$WORK_DIR"/{bin,config,data,logs} "$RUN_DIR"
  echo "· 工作目录：$WORK_DIR/{bin,config,data,logs}"
else
  echo "· dry-run：不建 $WORK_DIR 目录，不落任何文件"
fi

# ---------------------------------------------------------------- 找主程序（只看，不装）
detect_bin() {
  local candidate
  for candidate in "$LLONEBOT" "$NAPCAT" "${LLONEBOT_BIN:-}" "${NAPCAT_BIN:-}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  for candidate in \
    "$WORK_DIR"/bin/llonebot* "$WORK_DIR"/bin/NapCat* "$WORK_DIR"/bin/napcat* \
    "$HOME"/.config/LLOneBot/llonebot* /opt/NapCat/napcat* /opt/llonebot/llonebot* \
    "$(command -v llonebot 2>/dev/null || true)" "$(command -v napcat 2>/dev/null || true)"
  do
    [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  return 1
}

if BIN="$(detect_bin)"; then
  echo "· 协议端主程序：$BIN"
else
  BIN=""
  echo "· 没找到协议端主程序（这一步不下载，只探测）。给它一个路径就行："
  echo "    bash scripts/qq/setup_onebot.sh --llonebot /把主程序放这里的/完整路径"
  echo "  常见放法：把 linux-arm64 那一份解压进 $WORK_DIR/bin/ 再重跑本脚本。"
fi

# ---------------------------------------------------------------- 生成配置
snippet() {
  cat <<JSON
{
  "reverseWs": {
    "enable": true,
    "url": "${WS_URL}",
    "authorization": "$([[ -n "$ONEBOT_TOKEN" ]] && echo "Bearer ${ONEBOT_TOKEN}" || echo "")",
    "messageFormat": "array",
    "comment": "MySoulBot 是服务端，协议端反向连进来；这里填的是它监听的地址"
  },
  "engine": {
    "tavern": "http://${SERVER_HOST}:${SERVER_PORT}/v1",
    "panel": "http://${SERVER_HOST}:${SERVER_PORT}/panel",
    "healthz": "http://${SERVER_HOST}:${SERVER_PORT}/healthz"
  }
}
JSON
}

if [[ "$DRY" == "0" ]]; then
  snippet > "$WORK_DIR/config/reverse-ws.json"
  echo "· 已写 $WORK_DIR/config/reverse-ws.json（反向 WS 的那一条事实 + 引擎各端点）"
  if [[ -n "$BIN" ]]; then
    {
      echo "# 由 setup_onebot.sh 生成：run_onebot.sh 读它。含鉴权串，别提交进版本库。"
      echo "ONEBOT_BIN=$(printf '%q' "$BIN")"
      echo "REVERSE_WS_URL=$(printf '%q' "$WS_URL")"
      echo "ONEBOT_ACCESS_TOKEN=$(printf '%q' "$ONEBOT_TOKEN")"
    } > "$STATE_FILE"
    chmod 600 "$STATE_FILE"
    echo "· 已写 $STATE_FILE（0600，run_onebot.sh 用它）"
  fi
else
  echo "· dry-run：本该写的内容长这样——"
  snippet | sed 's/^/    /'
fi

# ---------------------------------------------------------------- 并进去（可选，先备份）
if [[ -n "$MERGE" ]]; then
  if [[ ! -f "$MERGE" ]]; then
    echo "✗ --merge 指的文件不存在：$MERGE" >&2
    exit 1
  fi
  if [[ "$DRY" == "1" ]]; then
    echo "· dry-run：本该把 wsClients 那一条并进 $MERGE（先备份成 $MERGE.bak-<时间戳>）"
  else
    "$PYTHON_BIN" - "$MERGE" "$WS_URL" "$ONEBOT_TOKEN" <<'PY'
import json, shutil, sys, time
from pathlib import Path

path, url, token = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
raw = path.read_text(encoding="utf-8")
try:
    config = json.loads(raw)
except json.JSONDecodeError as exc:
    sys.exit(f"✗ {path} 不是合法 JSON，不动它：{exc}")
if not isinstance(config, dict):
    sys.exit(f"✗ {path} 顶层不是对象，不动它")

entry = {"url": url, "enabled": True}
if token:
    entry["authorization"] = f"Bearer {token}"

# 协议端的反向 WS 列表各家写法不同：把这几种常见落点都找一遍，找到哪个动哪个
def find_lists(node, trail=()):
    hits = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {"wsclients", "websocketclients", "reversews", "ws"} and isinstance(value, list):
                hits.append((trail + (key,), value))
            hits.extend(find_lists(value, trail + (key,)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(find_lists(value, trail + (index,)))
    return hits

lists = find_lists(config)
if not lists:
    sys.exit(f"✗ 在 {path} 里没找到反向 WS 的列表（试过 wsClients / webSocketClients / reverseWs）。\n"
             f"  不动别人的文件——把 {url} 与 Bearer 串自己填进去就行。")
changed = 0
for trail, items in lists:
    if any(isinstance(item, dict) and str(item.get("url", "")).rstrip("/") == url.rstrip("/") for item in items):
        continue
    items.append(entry)
    changed += 1
    print(f"· 已并入 {'/'.join(str(part) for part in trail)}")
if not changed:
    print("· 这条反向 WS 已经在里面了，没重复加")
if changed:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"· 原文件备份在 {backup}")
PY
  fi
fi

# ---------------------------------------------------------------- 体检：端口上到底有没有人
if "$PYTHON_BIN" - "$ONEBOT_HOST" "$ONEBOT_PORT" <<'PY'
import socket, sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=1.5):
        pass
except OSError:
    sys.exit(1)
PY
then
  echo "✓ ${WS_URL} 上有人接（网桥已经在听了）"
else
  echo "· ${WS_URL} 现在没人接——先 bash scripts/daemon.sh start，再起协议端；顺序反了协议端会一直重试。"
fi

echo ""
echo "下一步："
echo "  1) 起引擎：      bash scripts/daemon.sh start && bash scripts/daemon.sh status"
[[ -n "$BIN" ]] && echo "  2) 起协议端：    bash scripts/qq/run_onebot.sh          # 终端里出登录二维码" \
  || echo "  2) 先把协议端主程序放好，再重跑本脚本 --llonebot <路径>"
[[ -z "$MERGE" ]] && echo "  3) 配置并进协议端：bash scripts/qq/setup_onebot.sh --merge <它的配置文件>"
exit 0
