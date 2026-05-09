"""
千里眼 (Senrigan) — pytest テストスイート

テスト対象:
  - URL バリデーション (正常系・異常系)
  - GET / — サービス情報
  - GET /healthz — ヘルスチェック
  - GET /view — HTML ビューア (構造検証)
  - GET /tilejson.json — TileJSON フォーマット検証
  - GET /tiles/{z}/{x}/{y}.png — タイルレスポンス検証

ネットワーク呼び出し (rio-tiler / rasterio) はモックで代替するため、
外部 GeoTIFF への実際のアクセスは行わない。
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

# --------------------------------------------------------------------- #
# テストクライアント                                                      #
# --------------------------------------------------------------------- #

client = TestClient(app, raise_server_exceptions=False)

# テスト用サンプル URL
SAMPLE_URL = "https://example.com/sample.tif"
ENCODED_URL = "https%3A%2F%2Fexample.com%2Fsample.tif"


# --------------------------------------------------------------------- #
# ヘルパー                                                               #
# --------------------------------------------------------------------- #

def _mock_cog_reader(minzoom=5, maxzoom=14):
    """COGReader のコンテキストマネージャモックを返す。"""
    mock_cog = MagicMock()
    mock_cog.minzoom = minzoom
    mock_cog.maxzoom = maxzoom
    mock_cog.bounds = MagicMock(left=-3.98, bottom=5.1, right=-3.8, top=5.5)
    # tile() が返す img の render() が PNG バイト列を返す
    mock_img = MagicMock()
    mock_img.render.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    mock_cog.tile.return_value = mock_img
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_cog)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _mock_rasterio_open():
    """rasterio.open のコンテキストマネージャモックを返す。"""
    mock_ds = MagicMock()
    mock_ds.crs = "EPSG:4326"
    mock_ds.bounds = MagicMock(left=-3.98, bottom=5.1, right=-3.8, top=5.5)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_ds)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# --------------------------------------------------------------------- #
# 基本エンドポイント                                                      #
# --------------------------------------------------------------------- #

class TestRoot:
    def test_returns_200(self):
        r = client.get("/")
        assert r.status_code == 200

    def test_service_name(self):
        r = client.get("/")
        data = r.json()
        assert data["service"] == "senrigan"

    def test_endpoints_listed(self):
        r = client.get("/")
        data = r.json()
        assert "viewer" in data["endpoints"]
        assert "tilejson" in data["endpoints"]
        assert "tiles" in data["endpoints"]


class TestHealthz:
    def test_returns_200(self):
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_status_ok(self):
        r = client.get("/healthz")
        assert r.json()["status"] == "ok"


# --------------------------------------------------------------------- #
# URL バリデーション                                                      #
# --------------------------------------------------------------------- #

class TestUrlValidation:
    """各エンドポイントの url パラメータバリデーションを横断テスト。"""

    def test_view_no_url(self):
        r = client.get("/view")
        assert r.status_code == 400

    def test_view_invalid_scheme(self):
        r = client.get("/view?url=ftp://example.com/a.tif")
        assert r.status_code == 400

    def test_tilejson_no_url(self):
        r = client.get("/tilejson.json")
        assert r.status_code == 400

    def test_tilejson_invalid_scheme(self):
        r = client.get("/tilejson.json?url=ftp://example.com/a.tif")
        assert r.status_code == 400

    def test_tiles_no_url(self):
        r = client.get("/tiles/10/0/0.png")
        assert r.status_code == 400

    def test_tiles_invalid_scheme(self):
        r = client.get("/tiles/10/0/0.png?url=ftp://example.com/a.tif")
        assert r.status_code == 400


# --------------------------------------------------------------------- #
# ビューア                                                               #
# --------------------------------------------------------------------- #

class TestView:
    def test_returns_html(self):
        r = client.get(f"/view?url={ENCODED_URL}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_contains_maplibre(self):
        r = client.get(f"/view?url={ENCODED_URL}")
        assert "maplibre-gl" in r.text

    def test_contains_tiff_url_reference(self):
        r = client.get(f"/view?url={ENCODED_URL}")
        # デコードされた URL が HTML に埋め込まれていること (HTML エスケープ済み形式)
        assert SAMPLE_URL in r.text or "example.com%2Fsample" in r.text or "example.com/sample" in r.text

    def test_cache_control_header(self):
        r = client.get(f"/view?url={ENCODED_URL}")
        assert "Cache-Control" in r.headers

    def test_tilejson_url_embedded(self):
        """HTML 内に /tilejson.json?url=... が埋め込まれていること。"""
        r = client.get(f"/view?url={ENCODED_URL}")
        assert "tilejson.json" in r.text

    def test_fitbounds_script_present(self):
        """自動ズーム用の fitBounds 呼び出しが HTML に含まれること。"""
        r = client.get(f"/view?url={ENCODED_URL}")
        assert "fitBounds" in r.text

    def test_footprint_script_present(self):
        """footprint 描画用のコードが HTML に含まれること。"""
        r = client.get(f"/view?url={ENCODED_URL}")
        assert "footprint" in r.text


# --------------------------------------------------------------------- #
# TileJSON                                                               #
# --------------------------------------------------------------------- #

class TestTileJSON:
    def _get(self):
        with (
            patch("src.main.rasterio.open", return_value=_mock_rasterio_open()),
            patch("src.main.transform_bounds", return_value=(-4.0, 5.0, -3.5, 5.6)),
            patch("src.main.COGReader", return_value=_mock_cog_reader()),
        ):
            return client.get(f"/tilejson.json?url={ENCODED_URL}")

    def test_returns_200(self):
        assert self._get().status_code == 200

    def test_tilejson_version(self):
        data = self._get().json()
        assert data["tilejson"] == "3.0.0"

    def test_tiles_field(self):
        data = self._get().json()
        assert len(data["tiles"]) == 1
        tile_url = data["tiles"][0]
        assert "{z}" in tile_url
        assert "{x}" in tile_url
        assert "{y}" in tile_url
        assert ".png" in tile_url
        # url クエリパラメータが tiles URL に含まれること
        assert "url=" in tile_url

    def test_bounds_is_list_of_4(self):
        data = self._get().json()
        assert isinstance(data["bounds"], list)
        assert len(data["bounds"]) == 4

    def test_center_field(self):
        data = self._get().json()
        assert isinstance(data["center"], list)
        assert len(data["center"]) == 3

    def test_minzoom_maxzoom(self):
        data = self._get().json()
        assert isinstance(data["minzoom"], int)
        assert isinstance(data["maxzoom"], int)
        assert data["minzoom"] <= data["maxzoom"]

    def test_cache_control_header(self):
        r = self._get()
        cc = r.headers.get("Cache-Control", "")
        assert "public" in cc
        assert "s-maxage" in cc

    def test_encoded_url_preserved_in_tiles(self):
        """tiles URL に元の GeoTIFF URL がエンコードされて含まれること。"""
        data = self._get().json()
        tile_url = data["tiles"][0]
        # URL エンコードされた example.com が tiles URL に含まれること
        assert "example.com" in tile_url or "example.com%2F" in tile_url


# --------------------------------------------------------------------- #
# XYZ タイル                                                             #
# --------------------------------------------------------------------- #

class TestTiles:
    def _get_tile(self, z=10, x=512, y=512):
        with patch("src.main.COGReader", return_value=_mock_cog_reader()):
            return client.get(f"/tiles/{z}/{x}/{y}.png?url={ENCODED_URL}")

    def test_returns_200(self):
        assert self._get_tile().status_code == 200

    def test_content_type_png(self):
        r = self._get_tile()
        assert r.headers["content-type"] == "image/png"

    def test_cache_control_header(self):
        r = self._get_tile()
        cc = r.headers.get("Cache-Control", "")
        assert "public" in cc
        assert "s-maxage" in cc

    def test_x_tile_header(self):
        r = self._get_tile(z=10, x=512, y=512)
        assert r.headers.get("X-Tile") == "10/512/512"

    def test_tile_outside_bounds_returns_404(self):
        from rio_tiler.errors import TileOutsideBounds

        mock_cog = MagicMock()
        mock_cog.tile.side_effect = TileOutsideBounds()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cog)
        ctx.__exit__ = MagicMock(return_value=False)
        with patch("src.main.COGReader", return_value=ctx):
            r = client.get(f"/tiles/20/0/0.png?url={ENCODED_URL}")
        assert r.status_code == 404

    def test_upstream_error_returns_502(self):
        mock_cog = MagicMock()
        mock_cog.tile.side_effect = RuntimeError("接続タイムアウト")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cog)
        ctx.__exit__ = MagicMock(return_value=False)
        with patch("src.main.COGReader", return_value=ctx):
            r = client.get(f"/tiles/10/512/512.png?url={ENCODED_URL}")
        assert r.status_code == 502
