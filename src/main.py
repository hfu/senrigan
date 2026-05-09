"""
senrigan: 千里眼 — Remote GeoTIFF をその場で Web 地図として見せる薄い変換レイヤ

Mission: FaaS があれば Martin COG 要らないんじゃね？
  (UNopenGIS/7 Issue #893 の精神を引き継ぐ)

yuiseki/poc-cng-cog-tile の設計・実装・運用上の知見を継承しつつ、
URL 設計とユーザー体験の面で発展させた後継構想。

主要エンドポイント:
  GET /view?url={remote-tiff-url}        — MapLibre GL JS ビューア
  GET /tilejson.json?url={remote-tiff-url} — TileJSON 3.0.0
  GET /tiles/{z}/{x}/{y}.png?url={remote-tiff-url} — XYZ タイル (オンデマンド生成)
"""

import html as html_module
import json
import logging
import os
from urllib.parse import quote, unquote

import rasterio
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from rasterio.warp import transform_bounds
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import COGReader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# デフォルトのタイルサイズ (512px は Retina ディスプレイ対応として推奨)
TILE_SIZE = int(os.environ.get("TILE_SIZE", "512"))

# --------------------------------------------------------------------- #
# FastAPI アプリ初期化                                                   #
# --------------------------------------------------------------------- #

app = FastAPI(
    title="千里眼 (Senrigan)",
    description="Remote GeoTIFF をブラウザで即時閲覧できる薄い変換レイヤ",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------- #
# ヘルパー関数                                                            #
# --------------------------------------------------------------------- #


def _vsicurl(url: str) -> str:
    """
    HTTP(S) URL を GDAL VSI Curl パスに変換する。

    rio-tiler / rasterio はローカルパスだけでなく `/vsicurl/https://...`
    形式の仮想パスを使ってリモート GeoTIFF を HTTP Range Request で読む。
    すでに `/vsicurl/` が付いている場合はそのまま返す。
    """
    if url.startswith("/vsicurl/"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return f"/vsicurl/{url}"
    raise ValueError(f"サポートされていない URL スキームです: {url!r}")


def _validate_url(url: str | None) -> str:
    """
    クエリパラメータ `url` の存在・スキームを検証し、デコード済み文字列を返す。
    無効な場合は HTTPException (400) を送出する。
    """
    if not url:
        raise HTTPException(
            status_code=400,
            detail="クエリパラメータ `url` が必要です。例: ?url=https://example.com/image.tif",
        )
    decoded = unquote(url)
    if not (decoded.startswith("http://") or decoded.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail="url は http:// または https:// で始まる必要があります。",
        )
    return decoded


def _get_wgs84_bounds(cog_path: str):
    """
    GeoTIFF を開き、WGS84 (EPSG:4326) の bounds を返す。
    戻り値: (left, bottom, right, top, center_lon, center_lat)
    """
    with rasterio.open(cog_path) as ds:
        src = ds.bounds
        left, bottom, right, top = transform_bounds(
            ds.crs,
            "EPSG:4326",
            src.left,
            src.bottom,
            src.right,
            src.top,
        )
    center_lon = (left + right) / 2
    center_lat = (bottom + top) / 2
    return left, bottom, right, top, center_lon, center_lat


# --------------------------------------------------------------------- #
# 基本エンドポイント                                                      #
# --------------------------------------------------------------------- #


@app.get("/", summary="サービス情報")
def root():
    """千里眼サービスの基本情報を返す。"""
    return {
        "service": "senrigan",
        "description": "Remote GeoTIFF をその場で Web 地図として見せる薄い変換レイヤ",
        "version": "1.0.0",
        "endpoints": {
            "viewer": "/view?url={remote-tiff-url}",
            "tilejson": "/tilejson.json?url={remote-tiff-url}",
            "tiles": "/tiles/{z}/{x}/{y}.png?url={remote-tiff-url}",
        },
    }


@app.get("/healthz", summary="ヘルスチェック")
def healthz():
    """Kubernetes / Knative のヘルスプローブに応答する。"""
    return {"status": "ok"}


# --------------------------------------------------------------------- #
# ビューア                                                                #
# --------------------------------------------------------------------- #

_VIEWER_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>千里眼 — {tiff_url_escaped}</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css"
  />
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: sans-serif; background: #111; color: #eee; }}
    #map {{ width: 100vw; height: 100vh; }}
    #info {{
      position: absolute;
      top: 10px;
      left: 10px;
      z-index: 10;
      background: rgba(0,0,0,0.65);
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 12px;
      max-width: 380px;
      word-break: break-all;
    }}
    #info a {{ color: #7cf; text-decoration: none; }}
    #info a:hover {{ text-decoration: underline; }}
    #loading {{
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      z-index: 20;
      background: rgba(0,0,0,0.75);
      padding: 16px 24px;
      border-radius: 8px;
      font-size: 16px;
    }}
  </style>
</head>
<body>
  <div id="loading">📡 読み込み中…</div>
  <div id="info">
    <strong>🔭 千里眼 (Senrigan)</strong><br />
    <a href="{tiff_url_escaped}" target="_blank" rel="noopener">{tiff_url_short_escaped}</a>
  </div>
  <div id="map"></div>

  <script src="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"></script>
  <script>
    const TILEJSON_URL = {tilejson_url_json};

    fetch(TILEJSON_URL)
      .then(r => {{
        if (!r.ok) throw new Error("TileJSON 取得失敗: " + r.status);
        return r.json();
      }})
      .then(tj => {{
        const bounds = tj.bounds; // [minLon, minLat, maxLon, maxLat]
        const centerLon = (bounds[0] + bounds[2]) / 2;
        const centerLat = (bounds[1] + bounds[3]) / 2;
        const zoom = Math.max(tj.minzoom, Math.min(tj.maxzoom, 10));

        const map = new maplibregl.Map({{
          container: "map",
          style: {{
            version: 8,
            sources: {{
              osm: {{
                type: "raster",
                tiles: ["https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png"],
                tileSize: 256,
                attribution: "© <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors",
              }},
            }},
            layers: [{{ id: "osm", type: "raster", source: "osm" }}],
          }},
          center: [centerLon, centerLat],
          zoom: zoom,
        }});

        map.on("load", () => {{
          // リモート GeoTIFF をタイルソースとして追加
          map.addSource("senrigan", {{
            type: "raster",
            url: TILEJSON_URL,
            tileSize: {tile_size},
          }});
          map.addLayer({{
            id: "senrigan-layer",
            type: "raster",
            source: "senrigan",
            paint: {{ "raster-opacity": 1.0 }},
          }});

          // footprint (bounds) をポリゴンとして重ねて表示
          const [minLon, minLat, maxLon, maxLat] = bounds;
          map.addSource("footprint", {{
            type: "geojson",
            data: {{
              type: "Feature",
              geometry: {{
                type: "Polygon",
                coordinates: [[
                  [minLon, minLat],
                  [maxLon, minLat],
                  [maxLon, maxLat],
                  [minLon, maxLat],
                  [minLon, minLat],
                ]],
              }},
            }},
          }});
          map.addLayer({{
            id: "footprint-outline",
            type: "line",
            source: "footprint",
            paint: {{
              "line-color": "#ff0",
              "line-width": 2,
              "line-opacity": 0.8,
            }},
          }});

          // 自動ズーム: GeoTIFF の bounds に fitBounds
          map.fitBounds(
            [[minLon, minLat], [maxLon, maxLat]],
            {{ padding: 40, duration: 1200 }}
          );

          document.getElementById("loading").style.display = "none";
        }});

        map.addControl(new maplibregl.NavigationControl(), "top-right");
        map.addControl(new maplibregl.ScaleControl(), "bottom-left");
        map.addControl(
          new maplibregl.AttributionControl({{ compact: true }}),
          "bottom-right"
        );
      }})
      .catch(err => {{
        document.getElementById("loading").textContent = "❌ " + err.message;
        console.error(err);
      }});
  </script>
</body>
</html>
"""


@app.get(
    "/view",
    response_class=HTMLResponse,
    summary="Web 地図ビューア",
    description=(
        "MapLibre GL JS を使用して Remote GeoTIFF を Web 地図として表示する。"
        "クエリパラメータ `url` に GeoTIFF の URL を渡す。"
    ),
)
def view(
    request: Request,
    url: str = Query(
        default=None,
        description="表示する GeoTIFF / COG の URL (http:// または https://)",
    ),
):
    """
    リモート GeoTIFF を MapLibre GL JS で表示する HTML ページを返す。

    - 自動ズーム: GeoTIFF の bounds に自動フィット
    - footprint 表示: bounds をポリゴンで重ね描き
    - attribution: MapLibre と OSM の帰属表示付き
    """
    tiff_url = _validate_url(url)
    base_url = str(request.base_url).rstrip("/")
    encoded_url = quote(tiff_url, safe="")
    tilejson_url = f"{base_url}/tilejson.json?url={encoded_url}"
    # 長い URL を表示用に省略 (HTML エスケープして XSS を防ぐ)
    tiff_url_short = tiff_url if len(tiff_url) <= 60 else tiff_url[:57] + "…"

    html = _VIEWER_HTML_TEMPLATE.format(
        tiff_url_escaped=html_module.escape(tiff_url, quote=True),
        tiff_url_short_escaped=html_module.escape(tiff_url_short),
        tilejson_url_json=json.dumps(tilejson_url),
        tile_size=TILE_SIZE,
    )
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "public, max-age=60"},
    )


# --------------------------------------------------------------------- #
# TileJSON                                                               #
# --------------------------------------------------------------------- #


@app.get(
    "/tilejson.json",
    summary="TileJSON 3.0.0",
    description=(
        "Remote GeoTIFF の bounds / zoom 情報を TileJSON 3.0.0 形式で返す。"
        "CDN キャッシュ可能 (Cache-Control: public, s-maxage=3600)。"
    ),
)
def tilejson(
    request: Request,
    url: str = Query(
        default=None,
        description="対象 GeoTIFF / COG の URL",
    ),
):
    """
    TileJSON 3.0.0 レスポンスを返す。

    MapLibre GL JS や QGIS などの地図クライアントがこのエンドポイントを
    `url` ソースとして参照できる。
    """
    tiff_url = _validate_url(url)
    cog_path = _vsicurl(tiff_url)
    base_url = str(request.base_url).rstrip("/")
    encoded_url = quote(tiff_url, safe="")

    try:
        left, bottom, right, top, center_lon, center_lat = _get_wgs84_bounds(cog_path)
        with COGReader(cog_path) as cog:
            minzoom = cog.minzoom
            maxzoom = cog.maxzoom
    except Exception as exc:
        logger.error("TileJSON 生成エラー url=%s: %s", tiff_url, exc)
        raise HTTPException(status_code=502, detail=f"GeoTIFF の読み取りに失敗しました: {exc}") from exc

    tile_url = f"{base_url}/tiles/{{z}}/{{x}}/{{y}}.png?url={encoded_url}"
    payload = {
        "tilejson": "3.0.0",
        "name": tiff_url.split("/")[-1],
        "description": "千里眼 (Senrigan) — on-the-fly COG tile service",
        "version": "1.0.0",
        "attribution": (
            "Senrigan / rio-tiler | "
            "© <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors"
        ),
        "scheme": "xyz",
        "tiles": [tile_url],
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "bounds": [left, bottom, right, top],
        "center": [center_lon, center_lat, max(minzoom, min(maxzoom, 10))],
        "tileSize": TILE_SIZE,
    }
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "public, s-maxage=3600, max-age=300",
        },
    )


# --------------------------------------------------------------------- #
# XYZ タイル                                                             #
# --------------------------------------------------------------------- #


@app.get(
    "/tiles/{z}/{x}/{y}.png",
    summary="XYZ PNG タイル",
    description=(
        "指定した z/x/y タイルを Remote GeoTIFF からオンデマンドで生成して返す。"
        "CDN キャッシュ可能 (Cache-Control: public, s-maxage=86400)。"
    ),
)
def get_tile(
    z: int,
    x: int,
    y: int,
    url: str = Query(
        default=None,
        description="対象 GeoTIFF / COG の URL",
    ),
):
    """
    rio-tiler を用いて Remote GeoTIFF から XYZ タイルをオンデマンド生成する。

    - 前処理不要: 任意の COG / GeoTIFF に対応
    - HTTP Range Request: GDAL VSI Curl 経由で必要部分のみ読む
    - タイル外 (404): bounds 外のタイルは 404 を返す
    """
    tiff_url = _validate_url(url)
    cog_path = _vsicurl(tiff_url)

    try:
        with COGReader(cog_path) as cog:
            img = cog.tile(x, y, z, tilesize=TILE_SIZE)
            data = img.render(img_format="PNG")
    except TileOutsideBounds:
        raise HTTPException(
            status_code=404,
            detail=f"タイル {z}/{x}/{y} は GeoTIFF の範囲外です。",
        )
    except Exception as exc:
        logger.error("タイル生成エラー z=%s x=%s y=%s url=%s: %s", z, x, y, tiff_url, exc)
        raise HTTPException(
            status_code=502,
            detail=f"タイル生成に失敗しました: {exc}",
        ) from exc

    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "public, s-maxage=86400, max-age=3600",
            "X-Tile": f"{z}/{x}/{y}",
        },
    )
