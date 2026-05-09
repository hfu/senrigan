from __future__ import annotations

import json
from html import escape
from urllib.parse import quote, urlsplit
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from rasterio.warp import transform_bounds

app = FastAPI(title="Senrigan", version="0.1.0")

VIEWER_CACHE_CONTROL = "public, max-age=60"
TILEJSON_CACHE_CONTROL = "public, max-age=300, s-maxage=3600"
TILE_CACHE_CONTROL = "public, max-age=86400, s-maxage=604800"
OAM_META_API_URL = "https://api.openaerialmap.org/meta"
OAM_CATALOG_DEFAULT_LIMIT = 20
OAM_CATALOG_MAX_LIMIT = 100


def validate_remote_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="url must be an http(s) URL")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must include a host")
    return url


def bounds_to_lonlat(bounds: tuple[float, float, float, float], crs) -> list[float]:
    if crs is None:
        raise HTTPException(status_code=502, detail="remote GeoTIFF CRS is unavailable")

    crs_string = str(crs)
    if crs_string in {"EPSG:4326", "http://www.opengis.net/def/crs/EPSG/0/4326"}:
        return [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]

    left, bottom, right, top = transform_bounds(crs_string, "EPSG:4326", *bounds, densify_pts=21)
    return [float(left), float(bottom), float(right), float(top)]


def fetch_oam_meta_results(page: int = 1, limit: int = OAM_CATALOG_DEFAULT_LIMIT) -> list[dict]:
    params = urlencode({"page": page, "limit": limit})
    request = UrlRequest(f"{OAM_META_API_URL}?{params}", headers={"Accept": "application/json"})

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"failed to read OAM catalog: {exc}") from exc

    results = payload.get("results")
    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="failed to read OAM catalog: invalid response")
    return results


@app.get("/oam-catalog", response_class=HTMLResponse)
def oam_catalog(
    page: int = Query(1, ge=1, description="OAM meta API page"),
    limit: int = Query(OAM_CATALOG_DEFAULT_LIMIT, ge=1, le=OAM_CATALOG_MAX_LIMIT, description="Number of entries to show"),
) -> HTMLResponse:
    results = fetch_oam_meta_results(page=page, limit=limit)

    items_html: list[str] = []
    for result in results:
        url = result.get("uuid")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue

        title = str(result.get("title") or result.get("name") or url)
        view_url = f"/view?url={quote(url, safe='')}"
        items_html.append(
            "<li>"
            f'<a href="{escape(view_url, quote=True)}">{escape(title)}</a>'
            f'<div><small>{escape(url)}</small></div>'
            "</li>"
        )

    if items_html:
        list_html = "<ul>" + "".join(items_html) + "</ul>"
    else:
        list_html = "<p>No OAM imagery found on this page.</p>"

    html = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>OAM Catalog - Senrigan</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.5; }}
      ul {{ padding-left: 1.25rem; }}
      li {{ margin-bottom: 1rem; }}
      small {{ color: #555; word-break: break-all; }}
    </style>
  </head>
  <body>
    <h1>OAM Catalog</h1>
    <p>OpenAerialMap の meta API から取得した画像一覧です。各リンクは Senrigan の viewer を開きます。</p>
    {list_html}
  </body>
</html>"""

    return HTMLResponse(html, headers={"Cache-Control": VIEWER_CACHE_CONTROL})


@app.get("/tilejson.json")
def tilejson(
    request: Request, url: str = Query(..., description="Remote GeoTIFF URL")
) -> JSONResponse:
    remote_url = validate_remote_url(url)
    try:
        with Reader(remote_url) as reader:
            info = reader.info()
            bounds_value = getattr(info, "bounds", None) or getattr(reader, "bounds", None)
            bounds = bounds_to_lonlat(bounds_value, getattr(reader, "crs", None))
            minzoom = int(getattr(reader, "minzoom", 0) or 0)
            maxzoom = int(getattr(reader, "maxzoom", 22) or 22)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"failed to read remote GeoTIFF: {exc}"
        ) from exc

    encoded = quote(remote_url, safe="")
    base_url = str(request.base_url).rstrip("/")
    payload = {
        "tilejson": "3.0.0",
        "name": "Senrigan layer",
        "scheme": "xyz",
        "tiles": [f"{base_url}/tiles/{{z}}/{{x}}/{{y}}.png?url={encoded}"],
        "bounds": bounds,
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "attribution": f"Source: {remote_url}",
    }
    return JSONResponse(payload, headers={"Cache-Control": TILEJSON_CACHE_CONTROL})


@app.get("/tiles/{z}/{x}/{y}.png")
def tiles(
    z: int,
    x: int,
    y: int,
    url: str = Query(..., description="Remote GeoTIFF URL"),
) -> Response:
    remote_url = validate_remote_url(url)
    try:
        with Reader(remote_url) as reader:
            image = reader.tile(x, y, z)
            content = image.render(img_format="PNG")
    except TileOutsideBounds as exc:
        raise HTTPException(status_code=404, detail="tile outside bounds") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to read remote GeoTIFF: {exc}") from exc

    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": TILE_CACHE_CONTROL},
    )


@app.get("/view", response_class=HTMLResponse)
def view(url: str = Query(..., description="Remote GeoTIFF URL")) -> HTMLResponse:
    validate_remote_url(url)

    html = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Senrigan Viewer</title>
    <link href=\"https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.css\" rel=\"stylesheet\" />
    <style>
      html, body, #map {{ margin: 0; padding: 0; width: 100%; height: 100%; }}
      .maplibregl-ctrl-attrib {{ max-width: 75%; }}
    </style>
  </head>
  <body>
    <div id=\"map\"></div>
    <script src=\"https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.js\"></script>
    <script>
      const remoteUrl = new URLSearchParams(window.location.search).get('url');
      if (!remoteUrl) {{
        document.body.textContent = 'Missing url query parameter';
      }}
      const encodedUrl = encodeURIComponent(remoteUrl || '');
      const tileJsonUrl = `/tilejson.json?url=${{encodedUrl}}`;
      const map = new maplibregl.Map({{
        container: 'map',
        style: {{
          version: 8,
          sources: {{
            senrigan: {{
              type: 'raster',
              tileSize: 256,
              tiles: [`/tiles/{{z}}/{{x}}/{{y}}.png?url=${{encodedUrl}}`],
              attribution: 'Source: ' + (remoteUrl || '')
            }}
          }},
          layers: [{{ id: 'senrigan-raster', type: 'raster', source: 'senrigan' }}]
        }},
        hash: true
      }});

      fetch(tileJsonUrl)
        .then((res) => res.json())
        .then((tj) => {{
          if (Array.isArray(tj.bounds) && tj.bounds.length === 4) {{
            const b = tj.bounds;
            map.fitBounds([[b[0], b[1]], [b[2], b[3]]], {{ padding: 20, maxZoom: tj.maxzoom ?? 22 }});

            const footprint = {{
              type: 'Feature',
              geometry: {{
                type: 'Polygon',
                coordinates: [[
                  [b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]
                ]]
              }},
              properties: {{}}
            }};

            map.on('load', () => {{
              map.addSource('footprint', {{ type: 'geojson', data: footprint }});
              map.addLayer({{
                id: 'footprint-line',
                type: 'line',
                source: 'footprint',
                paint: {{ 'line-color': '#ff5500', 'line-width': 2 }}
              }});
            }});
          }}
        }})
        .catch(() => {{
          // no-op; map still renders tiles if possible
        }});
    </script>
  </body>
</html>"""

    return HTMLResponse(html, headers={"Cache-Control": VIEWER_CACHE_CONTROL})
