#!/usr/bin/env bash
# 探一条密钥能吃哪些模型：逐个问一次，只记「有没有被拒」。
# 用法：NEWAPI_KEY=... NEWAPI_BASE=https://x/v1 bash scripts/probe_models.sh [候选名...]
set -u
KEY="${NEWAPI_KEY:?需要 NEWAPI_KEY}"
BASE="${NEWAPI_BASE:?需要 NEWAPI_BASE}"
DEFAULT_MODELS=(
  glm-4.6 glm-4.6v glm-4.7 glm-4.7-flash glm-4.5 glm-4-air glm-4-plus glm-5 glm-5.1 glm-5.2 glm-5.3
  glm-5.3-flash glm-5.3-flash-free deepseek-v3.2 deepseek-chat deepseek-reasoner doubao-seed-1-6
  kimi-k2 kimi-k2.5 minimax-m2 qwen3-max qwen-plus gpt-4o-mini gpt-4.1 gemini-2.5-flash
  claude-sonnet-4-20250514
)
if [ "$#" -gt 0 ]; then
  CANDIDATES=("$@")
else
  CANDIDATES=("${DEFAULT_MODELS[@]}")
fi
for m in "${CANDIDATES[@]}"; do
  body=$(timeout 25 curl -s -X POST "$BASE/chat/completions" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"只回一个字\"}],\"max_tokens\":8}")
  if grep -q "no access to model" <<<"$body"; then
    verdict="拒（密钥不含）"
  elif grep -q '"choices"' <<<"$body"; then
    verdict="通"
  elif grep -q '"error"' <<<"$body"; then
    verdict="错 $(grep -o '"message":"[^"]*"' <<<"$body" | head -1 | cut -c12-70)"
  else
    verdict="怪 $(head -c 60 <<<"$body")"
  fi
  printf '%-26s %s\n' "$m" "$verdict"
done
