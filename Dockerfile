FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    gdal-bin \
    libgdal-dev \
  && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY geotagged_photo_mapper.py .
COPY templates/ templates/
COPY static/ static/

# /app/data holds the state plane zone cache, downloaded census files, and
# (under proj_cache/) datum-shift grid files PROJ fetches over the network
# on first use. Mount a volume here to persist all of it across container
# restarts and avoid re-downloading on every cold start.
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["uvicorn", "geotagged_photo_mapper:app", "--host", "0.0.0.0", "--port", "8000"]
