# 千里眼 (Senrigan) — Remote GeoTIFF on-the-fly tile server
# yuiseki/poc-cng-cog-tile の Dockerfile を継承・発展させた構成

FROM python:3.12-slim

# GDAL は rio-tiler が内部で使う rasterio の依存ライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# タイルサイズは環境変数で上書き可能 (デフォルト 512px)
ENV TILE_SIZE=512

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
