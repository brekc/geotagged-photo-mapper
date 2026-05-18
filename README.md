# Geotagged Photo Mapper

Upload geotagged photos and plot their locations on an interactive map. Export to any coordinate reference system in six spatial formats. Built with Python, ExifTool, GeoPandas, FastAPI, and Leaflet.js.

---

## Quick Start (Choose Your Own Adventure)

```bash
git clone https://github.com/your-username/geotagged-photo-mapper.git
cd geotagged-photo-mapper
```

### Option A — Conda

1. Install ExifTool (system dependency):
   - **macOS:** `brew install exiftool`
   - **Linux (Ubuntu/Debian):** `sudo apt install libimage-exiftool-perl`
   - **Linux (Fedora/RHEL):** `sudo dnf install perl-Image-ExifTool`
   - **Linux (Arch):** `sudo pacman -S perl-image-exiftool`
   - **Windows** — pick one:
     ```
     winget install OliverBetz.ExifTool   # built into Windows 10/11
     choco install exiftool               # Chocolatey
     scoop install exiftool               # Scoop
     ```
     Or manually: download the "Windows Executable" zip from [exiftool.org](https://exiftool.org), rename `exiftool(-k).exe` → `exiftool.exe`, and add it to your `PATH`.

   Confirm it's on your PATH before continuing:
   ```
   exiftool -ver
   ```

2. Create the environment and run:
   ```bash
   conda env create -f environment.yml
   conda activate geotagged-photo-mapper
   uvicorn geotagged_photo_mapper:app --reload
   ```
   If port 8000 is already in use: `uvicorn geotagged_photo_mapper:app --reload --port 8001`

### Option B — Docker

No additional installs needed — ExifTool and all geospatial dependencies are bundled in the image.

```bash
docker build -t photo-mapper .
docker run -p 8000:8000 -v photo-mapper-data:/app/data photo-mapper
```

The `-v` flag mounts a named volume for the spatial data cache so it persists across container restarts.

Then open **http://localhost:8000**.

### Option C — pip + venv

> **Windows users:** GDAL and GeoPandas are unreliable via pip on Windows. Use Option A (Conda) or Option B (Docker) instead.

ExifTool must be installed and on your `PATH` first (see step 1 in Option A).

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn geotagged_photo_mapper:app --reload
```

---

## How It Works

```
Upload images → FastAPI receives files
  → PyExifTool extracts GPS tags (latitude, longitude, altitude, datetime, camera)
  → GeoPandas builds a GeoDataFrame
  → Leaflet.js renders markers on an interactive basemap
     (OpenStreetMap, CartoDB Voyager, or USGS Imagery + Topo)

On Download → GeoPandas reprojects to the selected CRS
  → Optional metadata columns appended (Photo Source, Flight Altitude AGL)
  → Written to the chosen format via GeoPandas
```

---

## Features

**Upload & Map**
- Drag-and-drop or click-to-browse photo upload
- Extracts latitude, longitude, altitude, datetime, and camera model from EXIF
- Each point opens a popup with a photo thumbnail, metadata, and a click-to-zoom lightbox
- Map auto-fits to the uploaded photo locations

**Export**
- Formats: GeoJSON, GeoPackage, File Geodatabase, Shapefile, KML, CSV
  (File Geodatabase export requires GDAL's OpenFileGDB driver, included in Conda and Docker installs; a bare `pip install geopandas` may not have it)
- CRS options:
  - Common: WGS 84 (EPSG:4326) and Web Mercator (EPSG:3857)
  - Region search: any projected CRS by state, province, or country, with a units toggle (meters/feet) and datum filter
  - Manual EPSG code override
- CSV coordinate columns use `longitude`/`latitude` for geographic CRS and `easting`/`northing` for projected CRS
- Custom export filename

**Optional export metadata** (applied at download time, columns omitted if left blank)
- **Photo Source** — a base path prepended to each filename, written to a `source` column (e.g. `S3://bucket/project/IMG_001.JPG`)
- **Flight Altitude AGL** — entered in feet or meters; both values stored in `flight_alt_ft` and `flight_alt_m` columns

**Reference Layers**
- Toggle UTM Zones or US State Plane Zones on the map
- Click any zone polygon to set its CRS for export
- State Plane zone boundaries are built from the Census Bureau county shapefile and a state plane reference CSV on first use (~2 MB download, cached to `data/`)

---

## Tech Stack

| Component | Role |
|---|---|
| **[FastAPI](https://fastapi.tiangolo.com/)** | Web server & API |
| **[PyExifTool](https://github.com/smarnach/pyexiftool)** | EXIF/GPS metadata extraction |
| **[GeoPandas](https://geopandas.org/)** | GeoDataFrame construction, spatial format I/O & CRS reprojection |
| **[pandas](https://pandas.pydata.org/)** | Tabular data for CRS/zone joins |
| **[Shapely](https://shapely.readthedocs.io/)** | Point geometry creation |
| **[pyproj](https://pyproj4.github.io/pyproj/)** | CRS database search |
| **[Leaflet.js](https://leafletjs.com/)** | Interactive map (CDN) |
| **[OpenStreetMap](https://www.openstreetmap.org/) / [CartoDB](https://carto.com/basemaps/) / [USGS](https://basemap.nationalmap.gov/)** | Basemap tile options (no API key required) |

Python dependencies are managed via Conda (`environment.yml`) or pip (`requirements.txt`).

---

## Relevant Resources

- [ExifTool Documentation](https://exiftool.org/) — complete tag reference for EXIF/GPS metadata
- [EPSG Registry](https://epsg.io/) — look up coordinate reference systems by name, region, or code
- [GeoPandas I/O](https://geopandas.org/en/stable/docs/reference/io.html) — supported spatial formats and driver options
- [pyproj CRS](https://pyproj4.github.io/pyproj/stable/api/crs/crs.html) — CRS object reference
- [Leaflet.js Documentation](https://leafletjs.com/reference.html) — interactive map API reference
- [ret3/stateplane](https://github.com/ret3/stateplane) — State Plane zone reference CSV (county-to-zone mapping with NAD83/NAD27 EPSG codes)
- [Census Bureau Cartographic Boundary Files](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html) — county shapefile used to build State Plane zone boundaries

---

## License

MIT
