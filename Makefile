.PHONY: build up up-detach down verify check-root check-health check-tilejson

# サンプル GeoTIFF URL (OpenAerialMap / Abidjan Mosaic)
SAMPLE_URL := https://oin-hotosm-temp.s3.amazonaws.com/5ea338e5c70abb0005869e8e/0/5ea338e5c70abb0005869e8f.tif
ENCODED_URL := $(shell python3 -c "from urllib.parse import quote; print(quote('$(SAMPLE_URL)', safe=''))")

BASE := http://localhost:8080

# Docker イメージをビルド
build:
	docker compose build

# サービスを起動 (フォアグラウンド)
up:
	docker compose up

# サービスをバックグラウンドで起動
up-detach:
	docker compose up -d

# サービスを停止
down:
	docker compose down

# スモークテスト (verify.sh)
verify:
	bash scripts/verify.sh

# --- 個別チェック ---

check-root:
	curl -sf "$(BASE)/" | python3 -m json.tool

check-health:
	curl -sf "$(BASE)/healthz" | python3 -m json.tool

check-tilejson:
	curl -sf "$(BASE)/tilejson.json?url=$(ENCODED_URL)" | python3 -m json.tool

check-view:
	@echo "ブラウザで以下の URL を開いてください:"
	@echo "  $(BASE)/view?url=$(ENCODED_URL)"

# サンプルタイルを /tmp に保存
save-tile:
	@mkdir -p /tmp/senrigan-tiles
	curl -so /tmp/senrigan-tiles/test.png \
	  "$(BASE)/tiles/13/3822/3984.png?url=$(ENCODED_URL)" && \
	  echo "保存先: /tmp/senrigan-tiles/test.png ($(shell wc -c < /tmp/senrigan-tiles/test.png) bytes)"

# pytest でユニットテストを実行
test:
	pytest tests/ -v

clean:
	docker compose down --rmi local
