#!/usr/bin/env bash
# 把 .env 里写好的 QQ 昵称/头像/签名再下发一次，并把结果念回来。
#
# 为什么要单独一个脚本：QQ 对改昵称有冷却，`set_qq_profile` 回了 ok 也可能没落地。
# 所以这里下发完一定回读一次 get_login_info，以回读为准——不然就是骗你。
#
# 用法：
#   scripts/push_qq_profile.sh            # 下发并回读
#   scripts/push_qq_profile.sh --check    # 只回读，不动账号
set -uo pipefail

cd "$(dirname "$0")/.."
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

if [[ $CHECK_ONLY -eq 1 ]]; then
  echo "· 只回读当前昵称（不动账号）"
  curl -s --max-time 8 http://127.0.0.1:11555/healthz \
    | "$PY" -c 'import json,sys; o=json.load(sys.stdin).get("onebot",{}); print("  协议端自报：", o.get("bot_names"), "· self_id", o.get("self_id"))'
  exit 0
fi

FLAG=$(grep -c '^ONEBOT_APPLY_PROFILE_ON_BOOT=true' .env 2>/dev/null || true)
if [[ "$FLAG" != "1" ]]; then
  cp .env /tmp/env.before-profile.$$
  sed -i 's/^ONEBOT_APPLY_PROFILE_ON_BOOT=.*/ONEBOT_APPLY_PROFILE_ON_BOOT=true/' .env
  RESTORE=1
else
  RESTORE=0
fi

echo "· 重启引擎以触发一次下发"
bash scripts/daemon.sh restart >/dev/null 2>&1 || { echo "  重启失败"; exit 1; }
sleep 30

LINE=$(grep -a '下发 QQ 资料' storage/logs/runtime.log | tail -1)
echo "  ${LINE##*| }"

if [[ "$RESTORE" == "1" ]]; then
  mv /tmp/env.before-profile.$$ .env
  echo "· 已把 ONEBOT_APPLY_PROFILE_ON_BOOT 关回原样，并重启一次留干净状态"
  bash scripts/daemon.sh restart >/dev/null 2>&1
  sleep 8
fi

echo "· 以回读为准："
curl -s --max-time 8 http://127.0.0.1:11555/healthz \
  | "$PY" -c 'import json,sys; o=json.load(sys.stdin).get("onebot",{}); print("  协议端自报：", o.get("bot_names"))'
