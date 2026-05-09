from __future__ import annotations

from html import escape
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

app = FastAPI(title="Senrigan", version="0.1.0")

VIEWER_CACHE_CONTROL = "public, max-age=60"
TILEJSON_CACHE_CONTROL = "public, max-age=300, s-maxage=3600"
TILE_CACHE_CONTROL = "public, max-age=86400, s-maxage=604800"


def validate_remote_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="url must be an http(s) URL")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must include a host")
    return url


@app.get("/tilejson.json")
def tilejson(
    request: Request, url: str = Query(..., description="Remote GeoTIFF URL")
) -> JSONResponse:
    remote_url = validate_remote_url(url)
    try:
        with Reader(remote_url) as reader:
            info = reader.info()
            bounds = [
                float(info.bounds.left),
                float(info.bounds.bottom),
                float(info.bounds.right),
                float(info.bounds.top),
            ]
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
    remote_url = validate_remote_url(url)
    encoded = quote(remote_url, safe="")
    escaped_url = escape(remote_url)

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
      const tileJsonUrl = '/tilejson.json?url={encoded}';
      const map = new maplibregl.Map({{
        container: 'map',
        style: {{
          version: 8,
          sources: {{
            senrigan: {{
              type: 'raster',
              tileSize: 256,
              tiles: [`/tiles/{{z}}/{{x}}/{{y}}.png?url={encoded}`],
              attribution: 'Source: {escaped_url}'
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
