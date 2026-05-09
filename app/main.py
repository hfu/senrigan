from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
OAM_MAP_CACHE_CONTROL = "no-store"
OAM_META_API_URL = "https://api.openaerialmap.org/meta"
OAM_CATALOG_DEFAULT_LIMIT = 20
OAM_CATALOG_MAX_LIMIT = 100
OAM_CENTERS_DEFAULT_MAX_PAGES = 15
OAM_CENTERS_MAX_PAGES_LIMIT = 50
OAM_CENTERS_DEFAULT_LIMIT = 200
OAM_CENTERS_FETCH_WORKERS = 6
OAM_CENTERS_CACHE_MAX_PAGES = OAM_CENTERS_MAX_PAGES_LIMIT
OAM_CENTERS_CACHE_LIMIT = OAM_CENTERS_DEFAULT_LIMIT
OAM_CENTERS_CACHE_TTL = timedelta(minutes=20)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OAM_MAP_HTML_PATH = PROJECT_ROOT / "docs" / "index.html"

_oam_centers_cache_lock = threading.Lock()
_oam_centers_cache_payload: dict | None = None
_oam_centers_cache_failed_pages: list[int] = []
_oam_centers_cache_updated_at: datetime | None = None
_oam_centers_cache_rebuilding = False


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


def fetch_oam_meta_results(
  page: int = 1, limit: int = OAM_CATALOG_DEFAULT_LIMIT
) -> tuple[list[dict], int | None, str | None]:
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

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    found_raw = meta.get("found") if isinstance(meta, dict) else None
    found = int(found_raw) if isinstance(found_raw, int) or (isinstance(found_raw, str) and found_raw.isdigit()) else None
    default_license = meta.get("license") if isinstance(meta, dict) and isinstance(meta.get("license"), str) else None
    return results, found, default_license


def center_from_bbox(bbox: object) -> list[float] | None:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None

    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox):
        return None

    left, bottom, right, top = bbox
    return [float((left + right) / 2), float((bottom + top) / 2)]


def build_oam_centers_payload(max_pages: int, limit: int) -> tuple[dict, list[int]]:
    features: list[dict] = []
    page_results: dict[int, list[dict]] = {}
    failed_pages: list[int] = []

    worker_count = min(max_pages, OAM_CENTERS_FETCH_WORKERS)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_page = {
            executor.submit(fetch_oam_meta_results, page, limit): page
            for page in range(1, max_pages + 1)
        }

        for future in as_completed(future_to_page):
            page = future_to_page[future]
            try:
                results, _, _ = future.result()
                page_results[page] = results
            except Exception:
                failed_pages.append(page)

    for page in range(1, max_pages + 1):
        if page in failed_pages:
            continue

        results = page_results.get(page, [])
        if not results:
            break

        for item in results:
            url = item.get("uuid")
            center = center_from_bbox(item.get("bbox"))
            if not isinstance(url, str) or not url.startswith(("http://", "https://")) or center is None:
                continue

            properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": center},
                    "properties": {
                        "title": str(item.get("title") or item.get("name") or url),
                        "url": url,
                        "provider": str(item.get("provider") or "Unknown"),
                        "platform": str(item.get("platform") or "Unknown"),
                        "acquired": str(item.get("acquisition_start") or item.get("uploaded_at") or "Unknown"),
                        "thumbnail": str(properties.get("thumbnail") or ""),
                    },
                }
            )

    payload = {"type": "FeatureCollection", "features": features}
    return payload, failed_pages


def _update_oam_centers_cache(payload: dict, failed_pages: list[int]) -> None:
    global _oam_centers_cache_payload
    global _oam_centers_cache_failed_pages
    global _oam_centers_cache_updated_at

    with _oam_centers_cache_lock:
      _oam_centers_cache_payload = payload
      _oam_centers_cache_failed_pages = sorted(failed_pages)
      _oam_centers_cache_updated_at = datetime.now(timezone.utc)


def _run_oam_centers_rebuild() -> None:
    global _oam_centers_cache_rebuilding
    try:
        payload, failed_pages = build_oam_centers_payload(
            max_pages=OAM_CENTERS_CACHE_MAX_PAGES,
            limit=OAM_CENTERS_CACHE_LIMIT,
        )
        _update_oam_centers_cache(payload, failed_pages)
    finally:
        with _oam_centers_cache_lock:
            _oam_centers_cache_rebuilding = False


def trigger_oam_centers_rebuild_async() -> bool:
    global _oam_centers_cache_rebuilding
    with _oam_centers_cache_lock:
        if _oam_centers_cache_rebuilding:
            return False
        _oam_centers_cache_rebuilding = True

    threading.Thread(target=_run_oam_centers_rebuild, daemon=True).start()
    return True


@app.get("/oam-map", response_class=HTMLResponse)
def oam_map() -> HTMLResponse:
    if not OAM_MAP_HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="oam map page is not available")

    html = OAM_MAP_HTML_PATH.read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": OAM_MAP_CACHE_CONTROL})


@app.get("/api/oam-centers")
def oam_centers(
    max_pages: int = Query(
        OAM_CENTERS_DEFAULT_MAX_PAGES,
        ge=1,
        le=OAM_CENTERS_MAX_PAGES_LIMIT,
        description="Number of pages to fetch from OAM meta API",
    ),
    limit: int = Query(
        OAM_CENTERS_DEFAULT_LIMIT,
        ge=1,
        le=OAM_CENTERS_DEFAULT_LIMIT,
        description="Page size used when fetching OAM meta API",
    ),
) -> JSONResponse:
    # The endpoint now serves a cached full dataset for cluster rendering.
    # Request params are accepted for backward compatibility and ignored.
    _ = max_pages
    _ = limit

    with _oam_centers_cache_lock:
      cached_payload = _oam_centers_cache_payload
      cached_failed_pages = list(_oam_centers_cache_failed_pages)
      cached_updated_at = _oam_centers_cache_updated_at

    if cached_payload is None:
        try:
            payload, failed_pages = build_oam_centers_payload(
                max_pages=OAM_CENTERS_CACHE_MAX_PAGES,
                limit=OAM_CENTERS_CACHE_LIMIT,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"failed to build OAM centers cache: {exc}") from exc

        _update_oam_centers_cache(payload, failed_pages)
        headers = {
            "Cache-Control": VIEWER_CACHE_CONTROL,
            "X-OAM-Centers-Cache": "miss",
        }
        if failed_pages:
            headers["X-OAM-Centers-Failed-Pages"] = ",".join(str(page) for page in sorted(failed_pages))
        return JSONResponse(payload, headers=headers)

    now = datetime.now(timezone.utc)
    is_stale = cached_updated_at is None or (now - cached_updated_at) > OAM_CENTERS_CACHE_TTL
    if is_stale:
        trigger_oam_centers_rebuild_async()

    headers = {
      "Cache-Control": VIEWER_CACHE_CONTROL,
      "X-OAM-Centers-Cache": "stale" if is_stale else "hit",
    }
    if cached_failed_pages:
      headers["X-OAM-Centers-Failed-Pages"] = ",".join(str(page) for page in cached_failed_pages)
    return JSONResponse(cached_payload, headers=headers)


@app.get("/oam-catalog", response_class=HTMLResponse)
def oam_catalog(
    page: int = Query(1, ge=1, description="OAM meta API page"),
    limit: int = Query(OAM_CATALOG_DEFAULT_LIMIT, ge=1, le=OAM_CATALOG_MAX_LIMIT, description="Number of entries to show"),
) -> HTMLResponse:
    fetched = fetch_oam_meta_results(page=page, limit=limit)
    if isinstance(fetched, tuple):
        results, total_found, default_license = fetched
    else:
        # Backward compatibility for tests or monkeypatches returning only a list.
        results = fetched
        total_found = None
        default_license = None

    items_html: list[str] = []
    for result in results:
        url = result.get("uuid")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue

        title = str(result.get("title") or result.get("name") or url)
        provider = str(result.get("provider") or "Unknown")
        platform = str(result.get("platform") or "Unknown")
        acquired = str(result.get("acquisition_start") or result.get("uploaded_at") or "Unknown")
        gsd = result.get("gsd")
        gsd_text = f"{float(gsd):.8g}" if isinstance(gsd, (int, float)) else "Unknown"
        properties = result.get("properties") if isinstance(result.get("properties"), dict) else {}
        item_license = result.get("license") or properties.get("license") or default_license or "Unknown"
        view_url = f"/view?url={quote(url, safe='')}"
        thumbnail_candidate = result.get("thumbnail") or properties.get("thumbnail")
        if isinstance(thumbnail_candidate, str) and thumbnail_candidate.startswith(("http://", "https://")):
            thumb_html = (
                f'<a class="card-media-link" href="{escape(view_url, quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'<img class="card-media" src="{escape(thumbnail_candidate, quote=True)}" alt="Thumbnail of {escape(title, quote=True)}" loading="lazy" />'
                "</a>"
            )
        else:
            thumb_html = '<div class="card-media card-media-fallback">No thumbnail</div>'

        items_html.append(
            "<article class=\"card\">"
            + thumb_html
            +
            f'<h2><a href="{escape(view_url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(title)}</a></h2>'
            "<div class=\"meta-grid\">"
            f'<div><span class=\"meta-key\">Provider</span><span class=\"meta-value\">{escape(provider)}</span></div>'
            f'<div><span class=\"meta-key\">Platform</span><span class=\"meta-value\">{escape(platform)}</span></div>'
            f'<div><span class=\"meta-key\">Acquired</span><span class=\"meta-value\">{escape(acquired)}</span></div>'
            f'<div><span class=\"meta-key\">GSD</span><span class=\"meta-value\">{escape(gsd_text)}</span></div>'
            f'<div><span class=\"meta-key\">License</span><span class=\"meta-value\">{escape(str(item_license))}</span></div>'
            "</div>"
            +
            f'<p class=\"source\">{escape(url)}</p>'
            "</article>"
        )

    if items_html:
        list_html = "<section class=\"cards\">" + "".join(items_html) + "</section>"
    else:
        list_html = "<p class=\"empty\">No OAM imagery found on this page.</p>"

    prev_link = (
        f'/oam-catalog?page={page - 1}&limit={limit}' if page > 1 else None
    )
    has_next = total_found is None or (page * limit) < total_found
    next_link = f'/oam-catalog?page={page + 1}&limit={limit}' if has_next else None

    total_label = str(total_found) if isinstance(total_found, int) else "unknown"
    paging_html = (
        "<nav class=\"pager\">"
        + (
            f'<a class=\"pager-btn\" href="{escape(prev_link, quote=True)}">&larr; Previous</a>'
            if prev_link
            else '<span class=\"pager-btn is-disabled\">&larr; Previous</span>'
        )
        + f'<span class=\"pager-state\">Page {page} / Total {total_label} (limit {limit})</span>'
        + (
            f'<a class=\"pager-btn\" href="{escape(next_link, quote=True)}">Next &rarr;</a>'
            if next_link
            else '<span class=\"pager-btn is-disabled\">Next &rarr;</span>'
        )
        + "</nav>"
    )

    html = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>OAM Catalog - Senrigan</title>
    <style>
      :root {{
        --bg: #f4efe6;
        --panel: #fffaf2;
        --ink: #202124;
        --muted: #5c615f;
        --accent: #0f766e;
        --accent-soft: #d7f0ec;
        --line: #d7cfc0;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        padding: 1.25rem;
        line-height: 1.5;
        color: var(--ink);
        background:
          radial-gradient(circle at 85% 10%, #e0f4ec 0%, transparent 38%),
          radial-gradient(circle at 10% 100%, #f7e8d9 0%, transparent 42%),
          var(--bg);
        font-family: "Avenir Next", "Hiragino Sans", "Noto Sans JP", sans-serif;
      }}
      .container {{ max-width: 1120px; margin: 0 auto; }}
      h1 {{ margin: 0 0 .5rem; font-size: clamp(1.5rem, 2vw + 1rem, 2.4rem); letter-spacing: .02em; }}
      .lead {{ margin: 0 0 1rem; color: var(--muted); }}
      .pager {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: .75rem;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: .8rem;
        padding: .75rem;
        margin: 0 0 1rem;
        flex-wrap: wrap;
      }}
      .pager-btn {{
        text-decoration: none;
        color: var(--accent);
        border: 1px solid var(--accent);
        border-radius: .55rem;
        padding: .45rem .8rem;
        font-weight: 600;
      }}
      .pager-btn.is-disabled {{ color: #9ca3af; border-color: #d1d5db; }}
      .pager-state {{ color: var(--muted); font-size: .95rem; }}
      .cards {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: .9rem;
      }}
      .card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: .9rem;
        padding: .9rem;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
      }}
      .card-media-link {{ display: block; margin: -.9rem -.9rem .7rem; }}
      .card-media {{
        display: block;
        width: 100%;
        aspect-ratio: 16 / 9;
        object-fit: cover;
        border-radius: .85rem .85rem 0 0;
        border-bottom: 1px solid var(--line);
        background: #f3f4f6;
      }}
      .card-media-fallback {{
        display: grid;
        place-items: center;
        margin: -.9rem -.9rem .7rem;
        color: var(--muted);
        font-size: .9rem;
        background: repeating-linear-gradient(
          -45deg,
          #e5e7eb,
          #e5e7eb 10px,
          #f3f4f6 10px,
          #f3f4f6 20px
        );
      }}
      .card h2 {{ margin: 0 0 .7rem; font-size: 1rem; line-height: 1.35; }}
      .card a {{ color: #115e59; text-decoration-color: #99f6e4; }}
      .meta-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: .45rem .6rem;
        margin-bottom: .7rem;
      }}
      .meta-key {{ display: block; font-size: .74rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
      .meta-value {{ display: block; font-size: .9rem; word-break: break-word; }}
      .source {{
        margin: 0;
        color: #4b5563;
        font-size: .8rem;
        word-break: break-all;
        background: var(--accent-soft);
        border-radius: .45rem;
        padding: .35rem .45rem;
      }}
      .empty {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: .8rem;
        padding: 1rem;
      }}
    </style>
  </head>
  <body>
    <main class=\"container\">
      <h1>OAM Catalog</h1>
      <p class=\"lead\">OpenAerialMap の meta API から取得した画像一覧です。各リンクは Senrigan の viewer を新しいタブで開きます。</p>
      {paging_html}
      {list_html}
      {paging_html}
    </main>
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
def view(url: str = Query(..., description="Remote GeoTIFF URL"), provider: str | None = Query(None, description="Optional provider name")) -> HTMLResponse:
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
      #loading {{
        position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
        background: rgba(255,255,255,0.9); padding: 2rem; border-radius: 0.5rem;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15); font-size: 1.1rem; z-index: 9999;
      }}
    </style>
  </head>
  <body>
    <div id=\"loading\">Loading...</div>
    <div id=\"map\"></div>
    <script src=\"https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.js\"></script>
    <script>
      const remoteUrl = new URLSearchParams(window.location.search).get('url');
      const provider = new URLSearchParams(window.location.search).get('provider');
      if (!remoteUrl) {{
        document.body.textContent = 'Missing url query parameter';
      }}
      const encodedUrl = encodeURIComponent(remoteUrl || '');
      const tileJsonUrl = `/tilejson.json?url=${{encodedUrl}}`;
      const sourceText = provider ? `${{provider}}` : (remoteUrl || '');
      const map = new maplibregl.Map({{
        container: 'map',
        style: {{
          version: 8,
          sources: {{
            senrigan: {{
              type: 'raster',
              tileSize: 256,
              tiles: [`/tiles/{{z}}/{{x}}/{{y}}.png?url=${{encodedUrl}}`],
              attribution: 'Source: ' + sourceText
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
              document.getElementById('loading').style.display = 'none';
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
