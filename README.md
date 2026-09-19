# Geotagged Photo Mapper

Upload geotagged photos and plot their locations on an interactive map. Export to any coordinate reference system in six spatial formats. Built with Python, ExifTool, GeoPandas, FastAPI, and Leaflet.js.

---

## Browser Demo (GitHub Pages)

A fully client-side version of the mapper lives in [`geotagged-photo-mapper-demo/`](geotagged-photo-mapper-demo/). It runs entirely in the browser, with no Python, no server, and no build tools. GPS coordinates are extracted from EXIF data using a built-in parser, and photos never leave the device.

**Live demo:** https://brekc.github.io/geotagged-photo-mapper/geotagged-photo-mapper-demo/

The full-featured Python app (export, CRS picker, etc.) requires the local/Docker setup below.

---

## Local & Private

This app runs as a **local web server** with no account or login. Photos are processed through short-lived, request-scoped temp files that are always cleaned up, and never leave this machine. Outbound connections are limited to basemap tiles (OpenStreetMap, Esri, USGS) and a couple of one-time reference-data downloads described below.

By default it only listens on `localhost`:

```bash
uvicorn geotagged_photo_mapper:app --reload
```

### Optional: trusted private-LAN use

Multiple people on the same trusted network can share one running instance (see [Multi-User Sessions](#multi-user-sessions)). Bind to all interfaces instead of just localhost:

```bash
uvicorn geotagged_photo_mapper:app --host 0.0.0.0 --port 8000
```

Other users then connect to the server machine's private IP, e.g. `http://192.168.1.25:8000`.

**Before doing this, understand what it does and does not protect:**

- Restrict inbound port 8000 to a trusted private network (e.g. a firewall rule, or simply an isolated LAN).
- The application has **no login**.
- The application has **no TLS**.
- Upload IDs isolate one person's dataset from another's, but they are **not authentication** -- anyone who can reach the port can use the app as any "user."
- **Do not** expose it directly to the internet.
- **Do not** port-forward it.
- **Do not** run it directly on a public cloud address.
- **Do not** use it on public or guest Wi-Fi.

---

## Quick Start (Choose Your Own Adventure)

```bash
git clone https://github.com/brekc/geotagged-photo-mapper.git
```

```bash
cd geotagged-photo-mapper
```

### Option A: Conda

1. Install ExifTool (system dependency):
   - **macOS:** `brew install exiftool`
   - **Linux (Ubuntu/Debian):** `sudo apt install libimage-exiftool-perl`
   - **Linux (Fedora/RHEL):** `sudo dnf install perl-Image-ExifTool`
   - **Linux (Arch):** `sudo pacman -S perl-image-exiftool`
   - **Windows** (pick one):

     Built into Windows 10/11
     ```bash
     winget install OliverBetz.ExifTool
     ```
     Chocolatey
     ```bash
     choco install exiftool
     ```
     Scoop
     ```bash
     scoop install exiftool
     ```
     Or manually: download the "Windows Executable" zip from [exiftool.org](https://exiftool.org), rename `exiftool(-k).exe` → `exiftool.exe`, and add it to your `PATH`.

   Confirm it's on your PATH before continuing:
   ```
   exiftool -ver
   ```

2. Create the environment and run:

   ```bash
   conda env create -f environment.yml
   ```

   ```bash
   conda activate geotagged-photo-mapper
   ```

   ```bash
   uvicorn geotagged_photo_mapper:app --reload
   ```

   If port 8000 is already in use: `uvicorn geotagged_photo_mapper:app --reload --port 8001`

### Option B: Docker

No additional installs needed. ExifTool and all geospatial dependencies are bundled in the image.

```bash
docker build -t geotagged-photo-mapper .
```

```bash
docker run -p 8000:8000 -v geotagged-photo-mapper-data:/app/data geotagged-photo-mapper
```

The `-v` flag mounts a named volume for the spatial data cache so it persists across container restarts.

Then open **http://localhost:8000**.

### Option C: pip + venv

> **Windows users:** GDAL and GeoPandas are unreliable via pip on Windows. Use Option A (Conda) or Option B (Docker) instead.

ExifTool must be installed and on your `PATH` first (see step 1 in Option A).

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
uvicorn geotagged_photo_mapper:app --reload
```

---

## How It Works

**Upload**

1. FastAPI receives the uploaded image files
2. PyExifTool extracts GPS metadata: latitude, longitude, altitude, datetime, and camera model
3. GeoPandas builds a GeoDataFrame from the extracted points
4. Leaflet.js renders circle markers on an interactive basemap

**Download**

1. GeoPandas reprojects the GeoDataFrame to the selected CRS
2. Optional metadata columns are appended: Photo Source path and Flight Altitude AGL
3. The file is written in the chosen format (GeoJSON, GeoPackage, File Geodatabase, Shapefile, KML, or CSV)

---

## Architecture

### Backend (Python / FastAPI)

- **`POST /upload`**: Receives image files (JPEG, PNG, HEIC, HEIF), extracts GPS/camera EXIF via PyExifTool, stores the result in a new isolated upload session (see [Multi-User Sessions](#multi-user-sessions)), and returns a GeoJSON FeatureCollection plus the session's `upload_id`
- **`POST /export`**: Reprojects a session's rows (filtered to the given `row_ids`, if any) to the selected CRS via GeoPandas and streams the file, given a matching `upload_id`
- **`DELETE /session/{upload_id}`** / **`POST /session/{upload_id}/close`**: Explicitly and idempotently deletes an upload session (Clear All uses the former; best-effort browser-unload cleanup uses the latter, since `navigator.sendBeacon()` can only POST)
- **`GET /crs-search`**: Queries pyproj's CRS database by region name for the region CRS dropdown
- **`GET /zone-geojson`**: Returns UTM or US State Plane zone polygons for the reference layer toggles. State Plane boundaries are built from the Census Bureau county shapefile and a reference CSV, then cached to `data/`
- **`POST /oriented-imagery/preflight`**: Per-file completeness counts for the Build Oriented Imagery panel
- **`POST /oriented-imagery/reference`** / **`POST /oriented-imagery/reference-preview`**: Mode A -- builds `oriented_imagery.csv` pointing at images that already exist at a user-supplied local path, UNC path, or URL
- **`POST /oriented-imagery/portable`**: Mode B -- re-receives the currently-included photos, re-extracts their metadata, converts them to privacy-stripped JPEG derivatives, and returns a portable ZIP package

### Frontend (JavaScript / Leaflet.js)

A single-page interface served from `templates/geotagged-photo-mapper.html`:

- Drag-and-drop or click-to-browse photo upload
- Leaflet map with selectable basemaps (OpenStreetMap, Esri Light Gray, USGS Imagery + Topo)
- Export panel: format selector, CRS picker (common presets, region search, or manual EPSG override), optional Photo Source and Flight Altitude AGL fields
- Reference layer toggles for UTM Zones and US State Plane Zones; clicking a zone polygon sets its CRS for export
- Photo popups with thumbnail previews and a zoom/pan lightbox
- Results list with click-to-fly navigation

---

## Features

**Upload & Map**
- Drag-and-drop or click-to-browse photo upload -- JPEG, PNG, HEIC, and HEIF, in any mixed batch
- Extracts latitude, longitude, altitude, datetime, and camera model from EXIF via ExifTool (on the untouched original file, regardless of format)
- Most browsers can't render HEIC/HEIF inline, so those get a bounded JPEG preview generated server-side (Pillow + pillow-heif); JPEG/PNG previews stay client-side as before
- One bad or unsupported file never aborts the rest of a batch -- per-file errors (unsupported type, corrupt image, missing GPS, unreadable metadata) are reported alongside the successful results
- Each point opens a popup with a photo thumbnail, metadata, and a click-to-zoom lightbox
- Map auto-fits to the uploaded photo locations

**Export**
- Formats: GeoJSON, GeoPackage, File Geodatabase, Shapefile, KML, CSV
  (File Geodatabase export requires GDAL's OpenFileGDB driver, included in Conda and Docker installs; a bare `pip install geopandas` may not have it)
- CRS options:
  - Common: WGS 84 (EPSG:4326) and Web Mercator (EPSG:3857)
  - Region search: any projected CRS by state, province, or country, with a units toggle (meters/feet) and datum filter
    (UTM zones will not appear here, since their area-of-use is defined by longitude bands, not state or country names; use the EPSG code field instead, or the UTM Zones map layer for WGS 84 UTM codes)
  - Manual EPSG code override
  - Custom CRS: paste a WKT or PROJ4 string, or upload a `.prj` file, for a project-specific datum or projection that isn't in the EPSG registry; takes priority over the EPSG code field when filled in
- CSV coordinate columns use `longitude`/`latitude` for geographic CRS and `easting`/`northing` for projected CRS
- Custom export filename
- Datum shifts (e.g. NAD83(HARN) &harr; NAD83(2011)) are handled by pyproj/PROJ, not skipped or approximated as identical. The highest-accuracy shift grids aren't bundled with the install, so on first export needing one, PROJ fetches it from its CDN and caches it to `data/proj_cache/` for every export after; if the server has no internet access, exports still work, just at whatever accuracy the bundled grids allow instead of the best available

**Optional export metadata** (applied at download time, columns omitted if left blank)
- **Photo Source**: a base path prepended to each filename, written to a `source` column (e.g. `S3://bucket/project/IMG_001.JPG`)
- **Flight Altitude AGL**: entered in feet or meters; only the entered unit's column (`flight_alt_ft` or `flight_alt_m`) is written to the export

**Reference Layers**
- Toggle UTM Zones or US State Plane Zones on the map
- Click any zone polygon to set its CRS for export
- State Plane zone boundaries are built from the Census Bureau county shapefile and a state plane reference CSV on first use (~2 MB download, cached to `data/`)

**Oriented Imagery Export**

After a successful upload, "Build Oriented Imagery" builds an oriented imagery table from the mapped photos. It reuses the sidebar's existing CRS selection and always states that "different cameras expose different metadata; missing values are left blank and are not inferred."

- **Reference existing images**: writes `oriented_imagery.csv` with `ImagePath` pointing at a local path, UNC path, or http(s) URL you supply. Only JPEG/JPG/TIF are referenced; PNG/HEIC/HEIF are excluded with a warning. A preview shows a few resolved paths before download, but the server only validates the *shape* of the path/URL -- it can't confirm a path on your machine actually exists.
- **Portable package (ZIP)**: reposts the currently-included photos, re-extracts their metadata, converts JPEG/PNG/HEIC/HEIF to orientation-normalized JPEG derivatives with EXIF/XMP/GPS/thumbnail/serial metadata stripped, and packages `oriented_imagery.csv` + `manifest.json` (source/derivative SHA-256 digests) + `README.txt` + `images/*.jpg` into one ZIP.

Only generic EXIF is understood (v1) -- no vendor pose adapters. Specifically:
- Pose/calibration fields (`CameraPitch`, `CameraRoll`, `Omega`, `Phi`, `Kappa`, `Matrix`, principal-point and distortion coefficients) are always left blank rather than guessed.
- `CameraHeading` is populated only when `GPSImgDirectionRef` confirms true north; a magnetic heading is left blank with a warning instead of an invented declination correction.
- Horizontal/vertical FOV is an approximate 35mm-equivalent estimate, never a calibration.
- `OrientedImageryType` must always be picked explicitly (Horizontal / Oblique / Nadir / 360 / Inspection), never guessed from the filename or camera model.

<a id="multi-user-sessions"></a>
**Multi-User Sessions (Trusted LAN)**

Each upload gets its own cryptographically random `upload_id` and a lock-protected, in-memory session holding only normalized metadata (never raw photo bytes or filesystem paths), with a 15-minute sliding expiration and bounded session/row counts. Every export requires the matching `upload_id`; an unknown, expired, deleted, or foreign id fails closed (404), so two people sharing an instance can never read or overwrite each other's data. Removing a marker or clicking Clear All immediately changes what the next export includes.

This store is in-memory and **process-local**, so it's only correct behind a single Uvicorn worker (the default). A multi-worker deployment would need a shared external store (Redis, a database) instead -- a session created on one worker is otherwise invisible to a request handled by another.

---

## Packages

| Package | Role |
|---|---|
| **[FastAPI](https://fastapi.tiangolo.com/)** | Web server & API |
| **[PyExifTool](https://github.com/smarnach/pyexiftool)** | EXIF/GPS metadata extraction |
| **[Pillow](https://python-pillow.org/) + [pillow-heif](https://github.com/bigcat88/pillow_heif)** | HEIC/HEIF decoding, JPEG preview/derivative generation, EXIF orientation handling |
| **[GeoPandas](https://geopandas.org/)** | GeoDataFrame construction, spatial format I/O & CRS reprojection |
| **[pandas](https://pandas.pydata.org/)** | Tabular data for CRS/zone joins |
| **[Shapely](https://shapely.readthedocs.io/)** | Point geometry creation |
| **[pyproj](https://pyproj4.github.io/pyproj/)** | CRS database search |
| **[Leaflet.js](https://leafletjs.com/)** | Interactive map (CDN) |
| **[OpenStreetMap](https://www.openstreetmap.org/) / [Esri](https://www.esri.com/en-us/arcgis/products/arcgis-living-atlas/services/basemaps) / [USGS](https://basemap.nationalmap.gov/)** | Basemap tile options (no API key required) |

Python dependencies are managed via Conda (`environment.yml`) or pip (`requirements.txt`).

---

## Relevant Resources

- [ExifTool Documentation](https://exiftool.org/): complete tag reference for EXIF/GPS metadata
- [EPSG Registry](https://epsg.io/): look up coordinate reference systems by name, region, or code
- [GeoPandas I/O](https://geopandas.org/en/stable/docs/reference/io.html): supported spatial formats and driver options
- [pyproj CRS](https://pyproj4.github.io/pyproj/stable/api/crs/crs.html): CRS object reference
- [Leaflet.js Documentation](https://leafletjs.com/reference.html): interactive map API reference
- [ret3/stateplane](https://github.com/ret3/stateplane): State Plane zone reference CSV (county-to-zone mapping with NAD83/NAD27 EPSG codes)
- [Census Bureau Cartographic Boundary Files](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html): county shapefile used to build State Plane zone boundaries

---

## License

MIT
