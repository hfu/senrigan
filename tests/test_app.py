from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


class FakeImage:
    def render(self, img_format: str = "PNG") -> bytes:
        assert img_format == "PNG"
        return b"png-bytes"


class FakeReader:
    minzoom = 1
    maxzoom = 12
    crs = "EPSG:4326"
    last_tile = None

    def __init__(self, url: str):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def info(self):
        return SimpleNamespace(bounds=(139.0, 35.0, 140.0, 36.0))

    def tile(self, x: int, y: int, z: int):
        type(self).last_tile = (x, y, z)
        return FakeImage()


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_oam_centers_cache(monkeypatch):
    monkeypatch.setattr(main_module, "_oam_centers_cache_payload", None)
    monkeypatch.setattr(main_module, "_oam_centers_cache_failed_pages", [])
    monkeypatch.setattr(main_module, "_oam_centers_cache_updated_at", None)
    monkeypatch.setattr(main_module, "_oam_centers_cache_rebuilding", False)


def test_view_uses_query_parameter_only():
    response = client.get("/view", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 200
    assert "maplibre-gl" in response.text
    assert "new URLSearchParams(window.location.search).get('url')" in response.text
    assert "/tilejson.json?url=${encodedUrl}" in response.text
    assert response.headers["cache-control"] == "public, max-age=60"


def test_oam_map_serves_docs_page():
    response = client.get("/oam-map")

    assert response.status_code == 200
    assert "Senrigan Catalog Map" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_oam_centers_returns_feature_collection(monkeypatch):
    def fake_fetch(page=1, limit=200):
        if page == 1:
            return (
                [
                    {
                        "title": "Center Point",
                        "uuid": "https://example.com/sample.tif",
                        "bbox": [139.0, 35.0, 140.0, 36.0],
                        "provider": "Test Provider",
                        "platform": "satellite",
                        "acquisition_start": "2025-01-01T00:00:00.000Z",
                        "properties": {"thumbnail": "https://example.com/thumb.png"},
                    }
                ],
                1,
                None,
            )

        return ([], 1, None)

    monkeypatch.setattr("app.main.fetch_oam_meta_results", fake_fetch)
    response = client.get("/api/oam-centers", params={"max_pages": 3, "limit": 200})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["x-oam-centers-cache"] == "miss"
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["geometry"]["type"] == "Point"
    assert feature["geometry"]["coordinates"] == [139.5, 35.5]
    assert feature["properties"]["title"] == "Center Point"
    assert feature["properties"]["url"] == "https://example.com/sample.tif"


def test_oam_centers_returns_partial_results_on_page_error(monkeypatch):
    def fake_fetch(page=1, limit=200):
        if page == 1:
            return (
                [
                    {
                        "title": "Page1",
                        "uuid": "https://example.com/p1.tif",
                        "bbox": [0.0, 0.0, 2.0, 2.0],
                    }
                ],
                None,
                None,
            )
        if page == 2:
            raise HTTPException(status_code=502, detail="upstream failed")
        return ([], None, None)

    monkeypatch.setattr("app.main.fetch_oam_meta_results", fake_fetch)
    response = client.get("/api/oam-centers", params={"max_pages": 3, "limit": 200})

    assert response.status_code == 200
    assert response.headers["x-oam-centers-cache"] == "miss"
    assert response.headers["x-oam-centers-failed-pages"] == "2"
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1


def test_oam_centers_stale_cache_returns_stale_and_kicks_rebuild(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "_oam_centers_cache_payload",
        {"type": "FeatureCollection", "features": []},
    )
    monkeypatch.setattr(main_module, "_oam_centers_cache_failed_pages", [2])
    monkeypatch.setattr(
        main_module,
        "_oam_centers_cache_updated_at",
        datetime.now(timezone.utc) - timedelta(minutes=21),
    )

    called = {"count": 0}

    def fake_trigger():
        called["count"] += 1
        return True

    monkeypatch.setattr(main_module, "trigger_oam_centers_rebuild_async", fake_trigger)

    response = client.get("/api/oam-centers")

    assert response.status_code == 200
    assert response.headers["x-oam-centers-cache"] == "stale"
    assert response.headers["x-oam-centers-failed-pages"] == "2"
    assert called["count"] == 1


def test_trigger_oam_centers_rebuild_async_blocks_duplicate(monkeypatch):
    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr(main_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(main_module, "_oam_centers_cache_rebuilding", False)

    first = main_module.trigger_oam_centers_rebuild_async()
    second = main_module.trigger_oam_centers_rebuild_async()

    assert first is True
    assert second is False


def test_oam_catalog_lists_view_links(monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_oam_meta_results",
        lambda page=1, limit=20: [
            {
                "title": "Sample OAM Image",
                "uuid": "https://oin-hotosm-temp.s3.us-east-1.amazonaws.com/sample/sample.tif",
            }
        ],
    )

    response = client.get("/oam-catalog")

    assert response.status_code == 200
    assert "OAM Catalog" in response.text
    assert "/view?url=https%3A%2F%2Foin-hotosm-temp.s3.us-east-1.amazonaws.com%2Fsample%2Fsample.tif" in response.text
    assert 'target="_blank"' in response.text
    assert "Sample OAM Image" in response.text
    assert response.headers["cache-control"] == "public, max-age=60"


def test_oam_catalog_paging_controls(monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_oam_meta_results",
        lambda page=2, limit=20: (
            [
                {
                    "title": "Paged OAM Image",
                    "uuid": "https://oin-hotosm-temp.s3.us-east-1.amazonaws.com/sample/paged.tif",
                    "provider": "Tester",
                    "platform": "uav",
                    "acquisition_start": "2025-01-01T00:00:00.000Z",
                    "gsd": 0.25,
                    "properties": {
                        "license": "CC-BY 4.0",
                        "thumbnail": "https://oin-hotosm-temp.s3.us-east-1.amazonaws.com/sample/paged_thumb.png",
                    },
                }
            ],
            45,
            "CC-BY 4.0",
        ),
    )

    response = client.get("/oam-catalog", params={"page": 2, "limit": 20})

    assert response.status_code == 200
    assert "Page 2 / Total 45 (limit 20)" in response.text
    assert "/oam-catalog?page=1&amp;limit=20" in response.text
    assert "/oam-catalog?page=3&amp;limit=20" in response.text
    assert "card-media" in response.text
    assert "sample/paged_thumb.png" in response.text


def test_tilejson_returns_expected_payload(monkeypatch):
    monkeypatch.setattr("app.main.Reader", FakeReader)

    response = client.get("/tilejson.json", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 200
    body = response.json()
    assert body["tilejson"] == "3.0.0"
    assert body["bounds"] == [139.0, 35.0, 140.0, 36.0]
    assert body["minzoom"] == 1
    assert body["maxzoom"] == 12
    assert (
        body["tiles"][0]
        == "http://testserver/tiles/{z}/{x}/{y}.png?url=https%3A%2F%2Fexample.com%2Fa.tif"
    )
    assert response.headers["cache-control"] == "public, max-age=300, s-maxage=3600"


def test_tiles_returns_png(monkeypatch):
    FakeReader.last_tile = None
    monkeypatch.setattr("app.main.Reader", FakeReader)

    response = client.get("/tiles/8/227/101.png", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400, s-maxage=604800"
    assert response.content == b"png-bytes"
    assert FakeReader.last_tile == (227, 101, 8)


def test_invalid_url_rejected():
    response = client.get("/tilejson.json", params={"url": "file:///tmp/a.tif"})

    assert response.status_code == 400
    assert response.json()["detail"] == "url must be an http(s) URL"


def test_tilejson_returns_502_on_reader_error(monkeypatch):
    class BrokenReader:
        def __init__(self, url: str):
            raise RuntimeError("cannot open")

    monkeypatch.setattr("app.main.Reader", BrokenReader)
    response = client.get("/tilejson.json", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 502
