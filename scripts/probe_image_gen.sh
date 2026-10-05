#!/usr/bin/env bash
# 探一条网关到底能不能画图：直接打 /images/generations，逐个候选模型名试。
# 用法：IMG_KEY=... IMG_BASE=https://x/v1 bash scripts/probe_image_gen.sh [候选名...]
set -u
KEY="${IMG_KEY:?需要 IMG_KEY}"; BASE="${IMG_BASE:?需要 IMG_BASE}"
DEFAULTS=(cogview-3-flash cogview-3 cogview-4 cogviewx3dtext "glm-4.5-flashx"
  dall-e-3 gpt-image-1 "black-forest-labs/FLUX.1-schnell" flux-dev-stable
  stable-diffusion-xl sdxl-turbo kolors-dev hunyuan-dit seedream-4.0
  seedream-3.0 t2i-stable "tongyi-wanx" wan2.2-t2i)
CAND="${*:-}"
if [ -n "$CAND" ]; then names=($CAND); else names=("${DEFAULTS[@]}"); fi
for m in "${names[@]}"; do
  out=$(timeout 25 curl -s -X POST "$BASE/images/generations" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"prompt\":\"一头蓝色的小鲸鱼，简笔画\",\"size\":\"1024x1024\",\"response_format\":\"b64_json\"}")
  if grep -q '"b64_json"' <<<"$out"; then
    printf '%-34s 能画（回了 b64_json，%s 字节）\n' "$m" "${#out}"
  elif grep -q '"url"' <<<"$out"; then
    printf '%-34s 能画（回了 url）\n' "$m"
  else
    printf '%-34s %s\n' "$m" "$(grep -o '"message":"[^"]*"' <<<"$out" | head -1 | cut -c12-96)"
  fi
done
