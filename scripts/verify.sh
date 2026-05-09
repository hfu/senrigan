#!/usr/bin/env bash
# 千里眼 (Senrigan) スモークテスト
# 実行中の Senrigan サービスに対してエンドポイントを検証する。
#
# 使用方法:
#   bash scripts/verify.sh
#   SENRIGAN_HOST=myhost SENRIGAN_PORT=9090 bash scripts/verify.sh

set -euo pipefail

HOST="${SENRIGAN_HOST:-localhost}"
PORT="${SENRIGAN_PORT:-8080}"
BASE="http://$HOST:$PORT"

# テスト用サンプル GeoTIFF URL (OpenAerialMap / Abidjan)
SAMPLE_URL="https://oin-hotosm-temp.s3.amazonaws.com/5ea338e5c70abb0005869e8e/0/5ea338e5c70abb0005869e8f.tif"
ENCODED_URL=$(python3 -c "from urllib.parse import quote; print(quote('$SAMPLE_URL', safe=''))")

echo "=== 千里眼 (Senrigan) スモークテスト ==="
echo "対象: $BASE"
echo ""

# --- GET / ---
echo "--- GET / ---"
curl -sf "$BASE/" | python3 -m json.tool
echo ""

# --- GET /healthz ---
echo "--- GET /healthz ---"
curl -sf "$BASE/healthz" | python3 -m json.tool
echo ""

# --- GET /tilejson.json ---
echo "--- GET /tilejson.json?url=... ---"
curl -sf "$BASE/tilejson.json?url=$ENCODED_URL" | python3 -m json.tool
echo ""

# --- GET /view ---
echo "--- GET /view?url=... (HTTP ステータス確認) ---"
HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" "$BASE/view?url=$ENCODED_URL" || echo "000")
echo "HTTP status: $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
  echo "SUCCESS: ビューア HTML を返した"
else
  echo "WARN: 予期しないステータス: $HTTP_CODE"
fi
echo ""

# --- GET /tiles/{z}/{x}/{y}.png ---
# Abidjan 近辺: lon=-3.98, lat=5.35 → z13 タイル 3822/3984 付近
echo "--- GET /tiles/13/3822/3984.png?url=... ---"
TILE_FILE="/tmp/senrigan-verify-tile.png"
HTTP_CODE=$(curl -sf -o "$TILE_FILE" -w "%{http_code}" \
  "$BASE/tiles/13/3822/3984.png?url=$ENCODED_URL" || echo "000")
echo "HTTP status: $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
  echo "タイルサイズ: $(wc -c < "$TILE_FILE") bytes"
  echo "SUCCESS: タイルを配信した"
else
  echo "INFO: bounds 外の可能性あり (TileJSON の bounds を確認してください)"
fi
echo ""

# --- エラーハンドリング確認 ---
echo "--- エラーハンドリング: url パラメータなし (400 期待) ---"
HTTP_CODE=$(curl -o /dev/null -w "%{http_code}" -s "$BASE/tilejson.json" || echo "000")
echo "HTTP status: $HTTP_CODE"
[ "$HTTP_CODE" = "400" ] && echo "SUCCESS: 400 Bad Request を返した" || echo "WARN: $HTTP_CODE"
echo ""

echo "=== スモークテスト完了 ==="
