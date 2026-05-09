# senrigan

Senrigan is a thin FastAPI wrapper that turns a remote GeoTIFF URL into:

- Viewer: `/view?url={remote-tiff-url}`
- TileJSON: `/tilejson.json?url={remote-tiff-url}`
- XYZ tiles: `/tiles/{z}/{x}/{y}.png?url={remote-tiff-url}`

It uses on-demand processing with `rio-tiler` and keeps the service stateless.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000/view?url=https%3A%2F%2Foin-hotosm-temp.s3.us-east-1.amazonaws.com%2F690585b76415e43597ffd7ea%2F0%2F690585b76415e43597ffd7eb.tif
```
