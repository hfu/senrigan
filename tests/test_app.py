from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


class FakeImage:
    def render(self, img_format: str = "PNG") -> bytes:
        assert img_format == "PNG"
        return b"png-bytes"


class FakeReader:
    minzoom = 1
    maxzoom = 12

    def __init__(self, url: str):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def info(self):
        return SimpleNamespace(bounds=SimpleNamespace(left=139.0, bottom=35.0, right=140.0, top=36.0))

    def tile(self, x: int, y: int, z: int):
        assert (z, x, y) == (8, 227, 101)
        return FakeImage()


client = TestClient(app)


def test_view_uses_query_parameter_only():
    response = client.get("/view", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 200
    assert "maplibre-gl" in response.text
    assert "/tilejson.json?url=https%3A%2F%2Fexample.com%2Fa.tif" in response.text
    assert response.headers["cache-control"] == "public, max-age=60"


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
    monkeypatch.setattr("app.main.Reader", FakeReader)

    response = client.get("/tiles/8/227/101.png", params={"url": "https://example.com/a.tif"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400, s-maxage=604800"
    assert response.content == b"png-bytes"


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
