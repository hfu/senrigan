import maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import { DARK, layers } from "@protomaps/basemaps";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

const BASEMAP_TILEJSON = "https://tunnel.optgeo.org/martin/protomaps-basemap";
const OAM_CENTERS_API = "/api/oam-centers";
const VIEW_ENDPOINT = "/view";
const EMPTY_CARD_HTML = `<div class="card-empty"></div>`;

function setCardMode(mode) {
  const panel = document.getElementById("catalog-card");
  if (!panel) return;
  panel.classList.toggle("is-empty", mode === "empty");
  panel.classList.toggle("is-expanded", mode === "expanded");
}

function attachPreviewButtonHandler() {
  const btn = document.querySelector(".card-preview-btn");
  if (!btn) return;

  btn.addEventListener("click", () => {
    const url = btn.getAttribute("data-url");
    const provider = btn.getAttribute("data-provider");
    if (!url) return;

    const panel = document.getElementById("catalog-card");
    if (!panel) return;

    const iframeUrl = `${VIEW_ENDPOINT}?url=${encodeURIComponent(url)}&provider=${encodeURIComponent(provider)}`;
    panel.innerHTML = `<iframe class="card-viewer" src="${iframeUrl}" title="Senrigan Viewer" allow="geolocation"></iframe>`;
    panel.classList.add("has-iframe");
    const cardViewer = panel.querySelector(".card-viewer");
    if (cardViewer) {
      cardViewer.style.height = "100dvh";
    }
  });
}

function clearCardViewer() {
  const panel = document.getElementById("catalog-card");
  if (!panel) return;
  const iframe = panel.querySelector(".card-viewer");
  if (iframe) {
    panel.classList.remove("has-iframe");
  }
}

function safeAddPmtilesProtocol() {
  try {
    const protocol = new Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
  } catch (error) {
    console.error("Failed to initialize PMTiles protocol", error);
  }
}

function ensureSansSerif(style) {
  for (const layer of style.layers ?? []) {
    if (layer.type === "symbol") {
      layer.layout = layer.layout ?? {};
      layer.layout["text-font"] = ["sans-serif"];
    }
  }
}

function buildBaseStyle() {
  const base = layers("protomaps", DARK);
  const style = Array.isArray(base)
    ? { version: 8, sources: {}, layers: base }
    : structuredClone(base);

  delete style.glyphs;
  delete style.sprite;
  ensureSansSerif(style);

  style.sources = style.sources ?? {};
  style.sources.protomaps = {
    type: "vector",
    url: BASEMAP_TILEJSON,
  };

  style.projection = { type: "globe" };

  return style;
}

async function fetchOamCenters(maxPages = 10, limit = 200) {
  const response = await fetch(`${OAM_CENTERS_API}?max_pages=${maxPages}&limit=${limit}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch OAM center points: ${response.status}`);
  }

  const payload = await response.json();
  if (payload?.type !== "FeatureCollection" || !Array.isArray(payload.features)) {
    throw new Error("Invalid OAM centers payload");
  }

  return payload;
}

function renderCatalogCard(feature) {
  const props = feature.properties ?? {};
  const title = props.title ?? "Untitled";
  const provider = props.provider ?? "Unknown";
  const platform = props.platform ?? "Unknown";
  const acquired = props.acquired ?? "Unknown";
  const sourceUrl = props.url ?? "";
  const thumb = typeof props.thumbnail === "string" ? props.thumbnail : "";

  return `
    <h2>${title}</h2>
    <div class="card-preview-container">
      ${thumb ? `<img class="card-thumb" src="${thumb}" alt="Thumbnail for ${title}" loading="lazy" />` : ""}
      <button class="card-preview-btn" data-url="${sourceUrl}" data-provider="${provider}" title="Preview in viewer">
        <svg class="preview-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
          <circle cx="12" cy="12" r="3"></circle>
        </svg>
        Open in viewer
      </button>
    </div>
    <div class="card-grid">
      <div><span>Provider</span><strong>${provider}</strong></div>
      <div><span>Platform</span><strong>${platform}</strong></div>
      <div><span>Acquired</span><strong>${acquired}</strong></div>
    </div>
    <p class="card-url">${sourceUrl}</p>
  `;
}

function showOverlay(show) {
  const node = document.getElementById("loading");
  if (!node) return;
  node.classList.toggle("is-hidden", !show);
}

async function bootstrap() {
  safeAddPmtilesProtocol();

  const app = document.getElementById("app");
  if (!app) return;
  app.innerHTML = `
    <section id="map-pane">
      <div id="map"></div>
      <div id="loading">Loading map data...</div>
    </section>
    <aside id="catalog-card" class="catalog-card is-empty">${EMPTY_CARD_HTML}</aside>
  `;

  const map = new maplibregl.Map({
    container: "map",
    style: buildBaseStyle(),
    center: [139.75, 35.68],
    zoom: 3,
    maxZoom: 22,
    hash: "map",
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");

  map.on("load", async () => {
    let oamGeojson;
    try {
      oamGeojson = await fetchOamCenters(15, 200);
    } catch (error) {
      console.error("Failed to fetch OAM center points", error);
      oamGeojson = { type: "FeatureCollection", features: [] };
    }

    map.addSource("oam-centers", {
      type: "geojson",
      data: oamGeojson,
      cluster: true,
      clusterMaxZoom: 14,
      clusterRadius: 48,
    });

    map.addLayer({
      id: "oam-clusters",
      type: "circle",
      source: "oam-centers",
      filter: ["has", "point_count"],
      paint: {
        "circle-color": ["step", ["get", "point_count"], "#7dd3fc", 20, "#38bdf8", 100, "#0ea5e9"],
        "circle-radius": ["step", ["get", "point_count"], 14, 20, 18, 100, 24],
        "circle-stroke-color": "#082f49",
        "circle-stroke-width": 1,
      },
    });

    map.addLayer({
      id: "oam-cluster-count",
      type: "symbol",
      source: "oam-centers",
      filter: ["has", "point_count"],
      layout: {
        "text-field": ["get", "point_count_abbreviated"],
        "text-font": ["sans-serif"],
        "text-size": 12,
      },
      paint: {
        "text-color": "#e2e8f0",
      },
    });

    map.addLayer({
      id: "oam-unclustered",
      type: "circle",
      source: "oam-centers",
      filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-color": "#fb7185",
        "circle-radius": 5,
        "circle-stroke-width": 1,
        "circle-stroke-color": "#9f1239",
      },
    });

    map.on("click", "oam-clusters", (event) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const clusterId = feature.properties?.cluster_id;
      const source = map.getSource("oam-centers");
      if (!source || typeof source.getClusterExpansionZoom !== "function") return;
      source.getClusterExpansionZoom(clusterId, (err, zoom) => {
        if (err) return;
        map.easeTo({
          center: feature.geometry.coordinates,
          zoom,
          duration: 450,
        });
      });
    });

    map.on("click", "oam-unclustered", (event) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const panel = document.getElementById("catalog-card");
      if (!panel) return;
      panel.innerHTML = renderCatalogCard(feature);
      setCardMode("expanded");
      attachPreviewButtonHandler();
    });

    map.on("mouseenter", "oam-clusters", () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", "oam-clusters", () => {
      map.getCanvas().style.cursor = "";
    });
    map.on("mouseenter", "oam-unclustered", () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", "oam-unclustered", () => {
      map.getCanvas().style.cursor = "";
    });

    map.on("click", (event) => {
      const features = map.queryRenderedFeatures(event.point, { layers: ["oam-unclustered"] });
      if (features.length > 0) return;
      const panel = document.getElementById("catalog-card");
      if (!panel) return;
      clearCardViewer();
      panel.innerHTML = EMPTY_CARD_HTML;
      setCardMode("empty");
    });
  });

  map.on("dataloading", () => showOverlay(true));
  map.on("idle", () => showOverlay(false));
}

bootstrap().catch((error) => {
  console.error("Failed to bootstrap map", error);
});
