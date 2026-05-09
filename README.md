# 千里眼 (Senrigan)

Remote GeoTIFF をブラウザで即時閲覧するための、薄い変換レイヤ。

---

## 背景

[OpenAerialMap (OAM)](https://openaerialmap.org/) には大量の GeoTIFF / COG がオブジェクトストレージ上に保存されている。しかし現状は、ブラウザだけで手軽に閲覧・共有することが難しい。

**千里眼 (Senrigan)** は、既存の GeoTIFF URL をそのまま参照しながら:

- ブラウザ閲覧 (MapLibre GL JS ビューア)
- XYZ タイル配信 (オンデマンド生成)
- TileJSON 3.0.0 提供

を実現する「薄い変換レイヤ」である。

---

## ミッション

このプロジェクトの問題意識は、[UNopenGIS/7 Issue #893](https://github.com/UNopenGIS/7/issues/893) 周辺で検討されていた精神を引き継ぐ。

> FaaS があれば Martin COG 要らないんじゃね？

特定のタイルサーバや重い前処理パイプラインに依存せず、

- FaaS / Stateless な実装
- rio-tiler / GDAL によるオンデマンド処理
- Remote GeoTIFF の直接参照

によって、必要十分な Web 配信体験を成立させる。

本プロジェクトは **[yuiseki/poc-cng-cog-tile](https://github.com/yuiseki/poc-cng-cog-tile)** の設計・実装・運用知見を深く継承しつつ、URL 設計とユーザー体験の面で発展させた後継構想である。

---

## エンドポイント

| パス | 説明 |
|---|---|
| `GET /` | サービス情報 |
| `GET /healthz` | ヘルスチェック |
| `GET /view?url={tiff-url}` | MapLibre GL JS ビューア |
| `GET /tilejson.json?url={tiff-url}` | TileJSON 3.0.0 |
| `GET /tiles/{z}/{x}/{y}.png?url={tiff-url}` | XYZ PNG タイル |

### ビューア

```
https://senrigan.optgeo.org/view?url=https%3A%2F%2Foin-hotosm-temp.s3.us-east-1.amazonaws.com%2F690585b76415e43597ffd7ea%2F0%2F690585b76415e43597ffd7eb.tif
```

- MapLibre GL JS で地図表示
- GeoTIFF の bounds に自動ズーム (fitBounds)
- footprint (外周ポリゴン) を黄色の線で重ね描き
- OSM ベースマップ付き

### TileJSON

```
https://senrigan.optgeo.org/tilejson.json?url=https%3A%2F%2Foin-hotosm-temp.s3.us-east-1.amazonaws.com%2F...
```

レスポンス例:

```json
{
  "tilejson": "3.0.0",
  "tiles": ["https://senrigan.optgeo.org/tiles/{z}/{x}/{y}.png?url=..."],
  "bounds": [-4.0, 5.0, -3.5, 5.6],
  "minzoom": 5,
  "maxzoom": 14,
  "center": [-3.75, 5.3, 10]
}
```

### XYZ タイル

```
https://senrigan.optgeo.org/tiles/13/3822/3984.png?url=https%3A%2F%2F...
```

オンデマンドで生成される。GDAL VSI Curl 経由で HTTP Range Request を使い、必要部分のみ読む。

---

## URL 設計方針

Senrigan では **Remote GeoTIFF の指定はクエリパラメータのみ**を採用する。

```
/view?url=https%3A%2F%2Fexample.com%2Fa.tif
/tilejson.json?url=https%3A%2F%2Fexample.com%2Fa.tif
/tiles/12/3456/1789.png?url=https%3A%2F%2Fexample.com%2Fa.tif
```

**パス埋め込み方式を採用しない理由:**

- `https://` や `/` を含む外部 URL を path として扱うのが不安定
- CDN・reverse proxy・framework によって解釈が異なりやすい
- デバッグ時に「どこまでが外部 URL か」が分かりにくい

**クエリパラメータ方式の利点:**

- FastAPI / CDN / proxy で安定して扱える
- ルーティングが単純
- 入力検証がしやすい
- TileJSON と tile endpoint の URL を一貫させやすい

---

## CDN / キャッシュ設計

| エンドポイント | Cache-Control |
|---|---|
| `/view` | `public, max-age=60` |
| `/tilejson.json` | `public, s-maxage=3600, max-age=300` |
| `/tiles/{z}/{x}/{y}.png` | `public, s-maxage=86400, max-age=3600` |

- キャッシュキーは **path + query string** 全体
- `url` パラメータが異なれば別リソースとして扱われる
- 同一 GeoTIFF URL への同一タイルリクエストは CDN でキャッシュ可能

---

## 設計思想: Thin Wrapper Philosophy

- GeoTIFF を**再保存しない** — 参照するだけ
- OAM 側データを**改変しない**
- メタデータ DB を**最小化する**
- URL だけで成立するステートレスな設計

---

## クイックスタート

### Docker Compose (ローカル開発)

```bash
# サービスを起動
docker compose up

# ビューアを開く (ブラウザで)
open "http://localhost:8080/view?url=https%3A%2F%2Foin-hotosm-temp.s3.amazonaws.com%2F5ea338e5c70abb0005869e8e%2F0%2F5ea338e5c70abb0005869e8f.tif"

# TileJSON を確認
make check-tilejson

# スモークテスト
make verify
```

### テスト実行

```bash
make test
# または直接:
pip install pytest httpx
pytest tests/ -v
```

---

## Knative デプロイ (Raspberry Pi 等)

```bash
# イメージをビルドしてローカルレジストリにプッシュ
docker build -t NODE_IP:5000/senrigan:latest .
docker push NODE_IP:5000/senrigan:latest

# マニフェストを適用
sed "s/NODE_IP/YOUR_NODE_IP/g" manifests/knative/senrigan.yaml | kubectl apply -f -
```

- **ゼロスケール対応**: リクエストがなければ Pod が 0 に縮小される
- **最大 5 Pod** まで自動スケールアウト

---

## リポジトリ構成

```
.
├── README.md
├── Dockerfile
├── Makefile
├── docker-compose.yml
├── requirements.txt
├── src/
│   └── main.py              FastAPI アプリ本体
├── tests/
│   └── test_main.py         pytest テストスイート
├── scripts/
│   └── verify.sh            スモークテストスクリプト
└── manifests/
    └── knative/
        └── senrigan.yaml    Knative Service マニフェスト
```

---

## 環境変数

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `TILE_SIZE` | `512` | タイルサイズ (px)。512 は Retina 対応推奨値。 |

---

## 技術スタック

| コンポーネント | 役割 |
|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | Web フレームワーク |
| [rio-tiler](https://cogeotiff.github.io/rio-tiler/) | COG オンデマンドタイル生成 |
| [rasterio](https://rasterio.readthedocs.io/) | 座標変換 (CRS → WGS84) |
| [GDAL VSI Curl](https://gdal.org/user/virtual_file_systems.html) | Remote GeoTIFF HTTP Range Read |
| [MapLibre GL JS](https://maplibre.org/) | ブラウザ地図ビューア |
| [Knative](https://knative.dev/) | FaaS / サーバーレスデプロイ |

---

## 先行実装との関係

千里眼は [yuiseki/poc-cng-cog-tile](https://github.com/yuiseki/poc-cng-cog-tile) の PoC を置き換えるものではなく、その知見をユーザー体験と URL 設計の面で発展させた後継構想である。

| 観点 | poc-cng-cog-tile | 千里眼 (Senrigan) |
|---|---|---|
| GeoTIFF 指定 | 環境変数 `COG_PATH` | クエリパラメータ `url` |
| ビューア | なし | MapLibre GL JS |
| TileJSON バージョン | 2.2.0 | 3.0.0 |
| 対象ユーザー | 開発者 | 一般ユーザー |
| URL 共有 | 困難 | URL のみで共有可能 |

---

## ライセンス

MIT