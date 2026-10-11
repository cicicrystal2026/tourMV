#!/usr/bin/env bash
# 用 cURL 直连火山方舟出图 —— 不经过项目代码，用来最快确认
# 「Key 能不能用 / 模型 ID 对不对 / 尺寸吃不吃」。
#
# 用法：
#   bash _probe/ark_curl.sh              # 默认尺寸 2560x1440（16:9）
#   bash _probe/ark_curl.sh 2K           # 换尺寸
#
# Key 与模型 ID 从项目 .env 读取（避免把密钥写进命令行历史）。
set -euo pipefail

MV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="C:/Users/thoma/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
OUT="$MV/data/_probe_t2i"

_read_env() {
  # 注意：.env 里很多键是注释状态，grep 会返回 1；
  # 配合 set -o pipefail 会让整条管道失败并静默退出，所以必须 `|| true`
  [ -f "$MV/.env" ] || return 0
  grep -E "^$1=" "$MV/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r"' \
    | sed 's/[[:space:]]*$//' || true
}

KEY="$(_read_env ARK_API_KEY)"
MODEL="$(_read_env SEEDREAM_IMAGE_MODEL)"
MODEL="${MODEL:-doubao-seedream-5-0-flash-260915}"
SIZE="${1:-2560x1440}"
BASE="${ARK_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"

if [ -z "$KEY" ]; then
  echo "❌ .env 里 ARK_API_KEY 是空的。先到火山方舟控制台「API Key 管理」创建并填入。"
  exit 1
fi

echo "模型   : $MODEL"
echo "尺寸   : $SIZE"
echo "Key    : ${KEY:0:8}…（长度 ${#KEY}）"
echo

PROMPT="吉林省长白山天池，冬季雪原，火山口湖面结冰，远处十六峰环抱，清晨冷蓝色调，写实纪实摄影，真实照片，专业单反拍摄"

RESP="$(curl -sS -m 200 -w $'\n%{http_code}' -X POST "$BASE/images/generations" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"$PROMPT\",\"size\":\"$SIZE\",\"response_format\":\"url\",\"watermark\":false}" \
  || true)"

CODE="$(printf '%s' "$RESP" | tail -n1)"
BODY="$(printf '%s' "$RESP" | sed '$d')"

echo "HTTP $CODE"
if [ "$CODE" != "200" ]; then
  printf '%s\n' "$BODY" | head -c 500; echo
  case "$CODE" in
    401) echo "→ 鉴权失败：确认用的是方舟「API Key 管理」创建的 API Key（Bearer 认证），不是 IAM 的 AK/SK" ;;
    403) echo "→ 无权限：确认已实名认证、且模型已在「开通管理」里开通" ;;
    404) echo "→ 模型不存在：核对 SEEDREAM_IMAGE_MODEL 与控制台 Model ID 一致" ;;
    400) echo "→ 参数问题：5.0 的 size 只接受 1K/1.5K/2K 或「宽x高」像素值，不可混用" ;;
    429) echo "→ 限流：稍后重试" ;;
  esac
  exit 1
fi

mkdir -p "$OUT"
URL="$(printf '%s' "$BODY" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print((d.get('data') or [{}])[0].get('url',''))")"
if [ -z "$URL" ]; then
  echo "→ 返回 200 但没有图片 URL（可能被内容审核拦截）："
  printf '%s' "$BODY" | head -c 400; echo
  exit 1
fi
DEST="$OUT/ark_$(date +%m%d_%H%M%S).jpg"
curl -sS -m 180 -o "$DEST" "$URL"
echo "✅ 出图成功：$DEST  ($(du -h "$DEST" | cut -f1))"
