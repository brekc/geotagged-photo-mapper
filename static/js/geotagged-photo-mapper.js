/* exported openLightbox, setCrsForExport */
// Called from onclick="" attributes in HTML strings this file builds itself
// (photo popups and zone popups), not from anywhere a linter can see.

// ======== MAP INIT ========
const map = L.map('map').setView([20, 0], 2);

// Three basemap choices, switchable from the layers control top-right.
// OpenStreetMap is the default; the others are just alternate tile sources.
const basemaps = {
  'OpenStreetMap': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }),
  'Voyager (CartoDB)': L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/">CARTO</a>',
    maxZoom: 19,
  }),
  'Imagery + Topo (USGS)': L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles courtesy of the <a href="https://usgs.gov">U.S. Geological Survey</a>',
    maxZoom: 16,
  }),
};

basemaps['OpenStreetMap'].addTo(map);
L.control.layers(basemaps, {}, { position: 'topright' }).addTo(map);

// Photo markers live in their own layer group so they can all be cleared at
// once (on re-upload or Clear All) without touching the reference layers.
const markerLayer = L.layerGroup().addTo(map);


// ======== STATE ========
// One entry per photo currently on the map, so the results list and the
// remove/clear buttons can find and remove the matching marker.
let mappedPhotos = []; // { filename, lat, lon, marker }


// ======== FILE HANDLING ========
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadBtn = document.getElementById('upload-btn');
const statusEl = document.getElementById('status');
const clearBtn = document.getElementById('clear-btn');

let selectedFiles = [];
// filename -> local blob URL, so popups and the results list can show
// thumbnails without re-reading the file. Nothing leaves the browser until
// Upload is clicked.
let photoURLs = new Map();

function setFiles(files) {
  // Revoke previous object URLs before replacing them, otherwise each new
  // selection would leak the URLs from the last one for the life of the tab.
  photoURLs.forEach(url => URL.revokeObjectURL(url));
  photoURLs = new Map();

  selectedFiles = Array.from(files).filter(f => f.type.startsWith('image/'));
  selectedFiles.forEach(f => {
    photoURLs.set(f.name, URL.createObjectURL(f));
  });

  if (selectedFiles.length === 0) {
    statusEl.textContent = 'No image files found in selection.';
    uploadBtn.disabled = true;
  } else {
    statusEl.textContent = `${selectedFiles.length} image${selectedFiles.length > 1 ? 's' : ''} selected.`;
    uploadBtn.disabled = false;
  }
}

// Clicking anywhere in the drop zone opens the native file picker, since the
// visible <div> isn't itself a form control.
dropZone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => setFiles(fileInput.files));

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  setFiles(e.dataTransfer.files);
});


// ======== UPLOAD ========
uploadBtn.addEventListener('click', async () => {
  if (selectedFiles.length === 0) return;

  uploadBtn.disabled = true;
  statusEl.textContent = 'Uploading and extracting GPS data...';

  const formData = new FormData();
  selectedFiles.forEach(f => formData.append('photos', f));

  try {
    const res = await fetch('/upload', { method: 'POST', body: formData });
    const data = await res.json();

    if (!res.ok) {
      statusEl.textContent = `Error: ${data.detail || res.statusText}`;
      uploadBtn.disabled = false;
      return;
    }

    const { geojson, total_uploaded, total_geotagged } = data;
    statusEl.textContent = `${total_geotagged} of ${total_uploaded} photo${total_uploaded !== 1 ? 's' : ''} had GPS data.`;

    plotGeoJSON(geojson);
    populateResults(geojson);

    // The Export / Flight Details / Results sections stay hidden until there's
    // at least one geotagged photo to act on.
    if (total_geotagged > 0) {
      document.getElementById('export-section').style.display = 'flex';
      document.getElementById('flight-details-section').style.display = 'flex';
      document.getElementById('results-section').style.display = 'flex';
    }
  } catch (err) {
    statusEl.textContent = `Network error: ${err.message}`;
  } finally {
    uploadBtn.disabled = false;
  }
});


// ======== PLOT GEOJSON ========
function plotGeoJSON(geojson) {
  markerLayer.clearLayers();
  mappedPhotos = [];

  const features = geojson.features || [];
  features.forEach(feature => {
    const p = feature.properties || {};
    const coords = feature.geometry?.coordinates || [];
    const lon = coords[0], lat = coords[1];
    if (lat == null || lon == null) return;

    const imgUrl = photoURLs.get(p.filename);
    const marker = buildMarker(p, lat, lon, imgUrl);
    markerLayer.addLayer(marker);
    mappedPhotos.push({ filename: p.filename, lat, lon, marker });
  });

  // Zoom/pan to fit every plotted photo. Wrapped in try/catch since
  // fitBounds throws on an empty/invalid bounds (e.g. right after Clear All).
  try {
    if (mappedPhotos.length > 0) {
      const bounds = L.latLngBounds(mappedPhotos.map(ph => [ph.lat, ph.lon]));
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
    }
  } catch (_) {
    // Nothing valid to fit to — fine.
  }
}

function buildMarker(p, lat, lon, imgUrl) {
  const marker = L.circleMarker([lat, lon], {
    radius: 8,
    fillColor: '#00d2ff',
    color: '#ffffff',
    weight: 2,
    fillOpacity: 0.8,
  });

  const meta = [];
  if (p.datetime)           meta.push(`Time: ${p.datetime}`);
  if (p.camera_model)       meta.push(`Camera: ${p.camera_model}`);
  if (p.altitude_m != null) meta.push(`Alt: ${Number(p.altitude_m).toFixed(1)} m / ${Number(p.altitude_ft).toFixed(1)} ft`);

  // Only show a thumbnail if we still have this photo's blob URL — a fresh
  // file selection clears photoURLs, so marker data can outlive its image.
  const imgTag = imgUrl
    ? `<img src="${imgUrl}" alt="${escapeHtml(p.filename || '')}" onclick="openLightbox('${escapeHtml(imgUrl)}')">`
    : '';
  const hint = imgUrl ? `<div class="popup-hint">Click photo to zoom and pan</div>` : '';

  const content = `<div class="photo-popup">
    ${imgTag}
    ${hint}
    <div class="popup-meta">
      <strong>${escapeHtml(p.filename || 'Unknown')}</strong>
      ${meta.join('<br>')}
    </div>
  </div>`;

  marker.bindPopup(content, { maxWidth: 240 });
  return marker;
}


// ======== CRS SELECTION ========
const regionSelect = document.getElementById('region-select');
const crsOptionsSelect = document.getElementById('crs-options-select');
const crsSelectedLabel = document.getElementById('crs-selected-label');
const commonCrsSelect = document.getElementById('common-crs-select');
const commonCrsSection = document.getElementById('common-crs-section');
const regionCrsSection = document.getElementById('region-crs-section');
const crsFilterControls = document.getElementById('crs-filter-controls');
const showAllDatumsChk = document.getElementById('show-all-datums');
const customCrsInput = document.getElementById('custom-crs-input');
const customCrsFile = document.getElementById('custom-crs-file');

let selectedEpsg = 4326; // default: WGS 84, matches the label below
let allCrsResults = [];   // full unfiltered list of CRS results for the current region
let activeUnits = 'meters';

// Datum priority: lower = newer/preferred (null = non-US, always kept).
// The "latest-datum filter" below uses this to collapse a zone's older
// realizations (HARN, plain NAD83, etc.) to just the newest one, unless
// "Show all datum realizations" is checked.
const DATUM_PRIORITY = {
  'NAD83(2011)': 1, 'NAD83(2011)(IERS)': 1,
  'NAD83(NSRS2007)': 2, 'NAD83(PA11)': 2, 'NAD83(MA11)': 2,
  'NAD83(HARN)': 3, 'NAD83(HARN/MATLAB)': 3,
  'NAD83': 4,
  'NAD27': 5,
};

function parseCrs(name) {
  // CRS names look like "NAD83(2011) / Washington North (ftUS)". Split on
  // " / " for datum vs. zone, and strip the feet marker so zones group by
  // base name regardless of units or datum.
  const slash = name.indexOf(' / ');
  const datum = slash >= 0 ? name.slice(0, slash) : '';
  const zone = slash >= 0 ? name.slice(slash + 3) : name;
  const isFeet = /\(ft(?:US)?\)/i.test(zone);
  const baseZone = zone.replace(/\s*\(ft(?:US)?\)/i, '').trim();
  const datumPriority = datum in DATUM_PRIORITY ? DATUM_PRIORITY[datum] : null;
  return { datum, baseZone, isFeet, datumPriority };
}

function applyFilters(list) {
  // 1. units filter: keep only meters, only feet, or everything, per the
  // active toggle button.
  let filtered = list.filter(r => {
    const { isFeet } = parseCrs(r.name);
    if (activeUnits === 'meters') return !isFeet;
    if (activeUnits === 'feet')   return isFeet;
    return true;
  });

  // 2. latest-datum filter (skip if "show all" is checked): for each
  // base zone + units combo, keep only the entry with the lowest datum
  // priority. Non-US zones have no priority mapping and are kept as-is.
  if (!showAllDatumsChk.checked) {
    const best = new Map(); // key: "baseZone|isFeet" -> best entry so far
    const nonUs = [];
    for (const r of filtered) {
      const { baseZone, isFeet, datumPriority } = parseCrs(r.name);
      if (datumPriority === null) { nonUs.push(r); continue; }
      const key = `${baseZone}|${isFeet}`;
      const prev = best.get(key);
      if (!prev || datumPriority < prev.priority) {
        best.set(key, { ...r, priority: datumPriority });
      }
    }
    filtered = [...best.values(), ...nonUs]
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  return filtered;
}

function renderCrsOptions() {
  const filtered = applyFilters(allCrsResults);
  if (filtered.length === 0) {
    crsOptionsSelect.innerHTML = '<option value="">No results for current filters</option>';
    crsOptionsSelect.disabled = true;
    return;
  }
  crsOptionsSelect.innerHTML = filtered
    .map(r => `<option value="${r.code}">${escapeHtml(r.name)} (EPSG:${r.code})</option>`)
    .join('');
  crsOptionsSelect.disabled = false;
}

// Units toggle (Meters / Both / Feet buttons above the region CRS results).
document.querySelectorAll('.toggle-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeUnits = btn.dataset.units;
    renderCrsOptions();
  });
});

showAllDatumsChk.addEventListener('change', renderCrsOptions);

commonCrsSelect.addEventListener('change', () => {
  const code = parseInt(commonCrsSelect.value, 10);
  if (!code) {
    // "-- Select --" was chosen again: fall back to letting the region
    // picker be used instead.
    regionCrsSection.style.display = '';
    return;
  }
  const label = commonCrsSelect.options[commonCrsSelect.selectedIndex].text;
  selectedEpsg = code;
  crsSelectedLabel.textContent = `Using: ${label}`;
  // Picking a common CRS resets the region picker and any custom CRS text,
  // since only one CRS source should be "active" at a time.
  regionSelect.value = '';
  clearCustomCrs();
  allCrsResults = [];
  crsOptionsSelect.innerHTML = '<option value="">-- Select a region first --</option>';
  crsOptionsSelect.disabled = true;
  crsFilterControls.style.display = 'none';
  regionCrsSection.style.display = 'none';
});

regionSelect.addEventListener('change', async () => {
  const region = regionSelect.value;

  if (!region) {
    crsOptionsSelect.innerHTML = '<option value="">-- Select a region first --</option>';
    crsOptionsSelect.disabled = true;
    crsFilterControls.style.display = 'none';
    allCrsResults = [];
    commonCrsSection.style.display = '';
    return;
  }

  // Picking a region resets the common CRS picker, mirroring the reset that
  // commonCrsSelect does above.
  commonCrsSelect.value = '';
  commonCrsSection.style.display = 'none';
  crsOptionsSelect.innerHTML = '<option value="">Loading...</option>';
  crsOptionsSelect.disabled = true;
  crsFilterControls.style.display = 'none';

  try {
    const res = await fetch(`/crs-search?q=${encodeURIComponent(region)}`);
    const data = await res.json();

    if (!res.ok || !Array.isArray(data) || data.length === 0) {
      crsOptionsSelect.innerHTML = '<option value="">No CRS found for this region</option>';
      return;
    }

    allCrsResults = data;
    crsFilterControls.style.display = 'flex';
    renderCrsOptions();
  } catch (_) {
    crsOptionsSelect.innerHTML = '<option value="">Error loading CRS</option>';
  }
});

crsOptionsSelect.addEventListener('change', () => {
  const code = parseInt(crsOptionsSelect.value, 10);
  if (!code) return;
  const label = crsOptionsSelect.options[crsOptionsSelect.selectedIndex].text;
  selectedEpsg = code;
  crsSelectedLabel.textContent = `Using: ${label}`;
  // Picking a region CRS is also a definitive choice, so any leftover
  // custom CRS text must not silently win at export time.
  clearCustomCrs();
});

// ── Custom CRS (paste WKT/PROJ4, or upload a .prj file) ──
// Highest-priority CRS source: a non-empty textarea at download time
// overrides the EPSG field, which overrides the region/common pickers (see
// the download handler below). Every other CRS-picking path must clear it
// via clearCustomCrs(), or the export would silently keep using it.
function clearCustomCrs() {
  customCrsInput.value = '';
  customCrsFile.value = '';
}

customCrsFile.addEventListener('change', () => {
  const file = customCrsFile.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    customCrsInput.value = String(reader.result || '').trim();
    updateCustomCrsLabel();
  };
  reader.onerror = () => {
    alert(`Could not read ${file.name}. Try pasting its contents into the text field instead.`);
  };
  reader.readAsText(file);
});

customCrsInput.addEventListener('input', updateCustomCrsLabel);

// A manually typed EPSG code is also a definitive CRS choice.
document.getElementById('custom-epsg').addEventListener('input', function () {
  if (this.value.trim() !== '') {
    customCrsInput.value = '';
    customCrsFile.value = '';
  }
});

function updateCustomCrsLabel() {
  const text = customCrsInput.value.trim();
  if (text) {
    crsSelectedLabel.textContent = 'Using: Custom CRS (pasted/uploaded)';
    return;
  }
  // Textarea was cleared: fall back to whatever EPSG is currently selected,
  // so the label doesn't keep claiming a custom CRS is active once it isn't.
  crsSelectedLabel.textContent = `Using: EPSG:${currentEpsgValue()}`;
}

// Effective EPSG: the manual field if typed, else whatever the
// common/region/zone pickers last set. Shared by the label update above
// and the download handler below so the two can't drift.
function currentEpsgValue() {
  const customEpsg = document.getElementById('custom-epsg').value.trim();
  return customEpsg !== '' ? customEpsg : String(selectedEpsg);
}


// ======== EXPORT / DOWNLOAD ========
// File extension for each export format, alphabetized to match the
// format-select dropdown and the backend's /export format handlers.
const FORMAT_EXT = {
  csv:        f => `${f}.csv`,
  filegdb:    f => `${f}.zip`,
  geojson:    f => `${f}.geojson`,
  geopackage: f => `${f}.gpkg`,
  kml:        f => `${f}.kml`,
  shapefile:  f => `${f}.zip`,
};

document.getElementById('download-btn').addEventListener('click', async (e) => {
  const format = document.getElementById('format-select').value;
  const epsg = currentEpsgValue();
  const customCrs = customCrsInput.value.trim();
  const baseName = document.getElementById('export-name').value.trim().replace(/[\\/:*?"<>|]/g, '_') || 'photo_locations';

  const formData = new FormData();
  formData.append('format', format);
  formData.append('epsg', epsg);
  // Only sent when non-empty; the backend treats a present, non-empty
  // custom_crs as taking priority over the epsg field above.
  if (customCrs) formData.append('custom_crs', customCrs);
  formData.append('source_path', document.getElementById('source-path').value.trim());
  formData.append('flight_altitude', document.getElementById('flight-altitude').value.trim());
  formData.append('altitude_unit', document.getElementById('altitude-unit').value);
  // The backend uses this to name the file/layer *inside* multi-file
  // formats (FileGDB, GeoPackage, Shapefile) — the rename below only
  // renames the outer downloaded blob, which those insides don't see.
  formData.append('export_name', baseName);

  const dlBtn = e.currentTarget;
  dlBtn.disabled = true;
  dlBtn.textContent = 'Exporting...';

  try {
    const res = await fetch('/export', { method: 'POST', body: formData });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(`Export error: ${data.detail || res.statusText}`);
      return;
    }

    const filename = (FORMAT_EXT[format] || (f => `${f}.${format}`))(baseName);

    // The export arrives as a blob, not a URL, so trigger the download via
    // a throwaway <a download> link rather than navigating the page.
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`Download failed: ${err.message}`);
  } finally {
    dlBtn.disabled = false;
    dlBtn.textContent = 'Download';
  }
});


// ======== RESULTS LIST ========
function populateResults(geojson) {
  const list = document.getElementById('results-list');
  list.innerHTML = '';

  const features = geojson.features || [];
  if (features.length === 0) return;

  features.forEach(feature => {
    const p = feature.properties || {};
    const coords = feature.geometry?.coordinates || [];
    const lon = coords[0], lat = coords[1];
    const imgUrl = photoURLs.get(p.filename);

    const li = document.createElement('li');

    const thumbEl = imgUrl
      ? `<img class="result-thumb" src="${imgUrl}" alt="">`
      : `<div class="result-thumb-placeholder">No image</div>`;

    li.innerHTML = `
      ${thumbEl}
      <div class="result-text">
        <div class="result-filename">${escapeHtml(p.filename || 'Unknown')}</div>
        <div class="result-coords">${lat != null ? lat.toFixed(5) : '?'}, ${lon != null ? lon.toFixed(5) : '?'}</div>
      </div>
      <button class="remove-btn" title="Remove photo">×</button>
    `;
    li.querySelector('.remove-btn').addEventListener('click', e => {
      e.stopPropagation();
      removePhoto(p.filename, li);
    });
    li.addEventListener('click', () => {
      if (lat != null && lon != null) map.flyTo([lat, lon], 16);
    });
    list.appendChild(li);
  });
}

function removePhoto(filename, li) {
  const idx = mappedPhotos.findIndex(ph => ph.filename === filename);
  if (idx !== -1) {
    markerLayer.removeLayer(mappedPhotos[idx].marker);
    mappedPhotos.splice(idx, 1);
  }
  li.remove();

  // Once the last photo is removed, hide the sections that only make sense
  // when there's data to show or export.
  if (mappedPhotos.length === 0) {
    document.getElementById('results-section').style.display = 'none';
    document.getElementById('export-section').style.display = 'none';
    document.getElementById('flight-details-section').style.display = 'none';
    statusEl.textContent = 'All photos removed. Ready for new upload.';
  }
}


// ======== CLEAR ALL ========
clearBtn.addEventListener('click', () => {
  mappedPhotos = [];
  markerLayer.clearLayers();
  document.getElementById('results-list').innerHTML = '';
  document.getElementById('results-section').style.display = 'none';
  document.getElementById('export-section').style.display = 'none';
  document.getElementById('flight-details-section').style.display = 'none';
  statusEl.textContent = 'Cleared. Ready for new upload.';
});


// ======== REFERENCE LAYERS ========

// Shared popup body for both reference layers (UTM and State Plane): click
// "Use for export" to set that zone's CRS directly from the map, instead of
// hunting for it in the region search.
function setCrsForExport(epsg, name) {
  selectedEpsg = epsg;
  crsSelectedLabel.textContent = `Using: ${escapeHtml(name)} (EPSG:${epsg})`;
  document.getElementById('custom-epsg').value = epsg;
  // Clear both CRS pickers (and any custom CRS text) so the EPSG field is
  // the single source of truth for what's about to be exported.
  commonCrsSelect.value = '';
  regionSelect.value = '';
  clearCustomCrs();
  crsOptionsSelect.innerHTML = '<option value="">-- Select a region first --</option>';
  crsOptionsSelect.disabled = true;
  crsFilterControls.style.display = 'none';
  commonCrsSection.style.display = '';
  regionCrsSection.style.display = '';
  map.closePopup();

  const exportSection = document.getElementById('export-section');
  if (exportSection.style.display !== 'none') {
    exportSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function zonePopupHtml(name, epsg, area) {
  return `<div style="font-size:13px;line-height:1.6;min-width:180px;">
    <strong style="font-size:14px;">${escapeHtml(name)}</strong><br>
    EPSG: ${epsg}<br>
    <span style="font-size:12px;color:#475569;">${escapeHtml(area || '')}</span><br>
    <button onclick="setCrsForExport(${epsg}, '${name.replace(/'/g, "\\'")}')"
      style="margin-top:6px;padding:4px 10px;background:#0284c7;color:#fff;
             border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:600;width:auto;">
      Use for export
    </button>
  </div>`;
}

// ── UTM Zones ──
// Generated client-side: every UTM zone is a simple 6-degree-wide rectangle
// by definition, unlike State Plane's irregular, county-based borders,
// which do need a server fetch.
function buildUtmLayer(datum) {
  const useNad83 = datum === 'nad83';
  const features = [];
  for (let z = 1; z <= 60; z++) {
    const w = -180 + (z - 1) * 6, e = w + 6;
    // NAD83 northern zones only exist for zones 1-23 (North America coverage);
    // higher zones and all southern-hemisphere zones fall back to WGS 84.
    const nad83Available = useNad83 && z <= 23;
    const nEpsg = nad83Available ? 26900 + z : 32600 + z;
    const nName = nad83Available ? `NAD83 / UTM Zone ${z}N` : `WGS 84 / UTM Zone ${z}N`;
    features.push({
      type: 'Feature',
      properties: { name: nName, epsg: nEpsg, area: 'Northern Hemisphere' },
      geometry: { type: 'Polygon', coordinates: [[[w,0],[e,0],[e,84],[w,84],[w,0]]] },
    });
    features.push({
      type: 'Feature',
      properties: { name: `WGS 84 / UTM Zone ${z}S`, epsg: 32700 + z, area: 'Southern Hemisphere' },
      geometry: { type: 'Polygon', coordinates: [[[w,-80],[e,-80],[e,0],[w,0],[w,-80]]] },
    });
  }
  return L.geoJSON({ type: 'FeatureCollection', features }, {
    style: { color: '#00aaff', weight: 1, fillOpacity: 0.04, fillColor: '#00aaff' },
    onEachFeature(f, layer) {
      const p = f.properties;
      layer.bindPopup(zonePopupHtml(p.name, p.epsg, p.area));
      layer.on('mouseover', () => layer.setStyle({ fillOpacity: 0.15 }));
      layer.on('mouseout',  () => layer.setStyle({ fillOpacity: 0.04 }));
    },
  });
}

// ── US State Plane Zones ──
const SP_LABEL_ZOOM = 6; // show permanent (always-on) labels at or above this zoom level

async function buildStatePlaneLayer() {
  const res = await fetch('/zone-geojson?type=state_plane');
  const data = await res.json();

  const layer = L.geoJSON(data, {
    style: {
      color: '#ff8800', weight: 1.5,
      fill: true, fillColor: '#ff8800', fillOpacity: 0,
    },
    onEachFeature(f, lyr) {
      const p = f.properties;
      // Strip the datum prefix so the tooltip reads "Washington North"
      // instead of "NAD83(2011) / Washington North".
      const shortName = p.name.includes(' / ') ? p.name.split(' / ')[1] : p.name;
      lyr.bindTooltip(shortName, {
        permanent: false, direction: 'center',
        className: 'zone-label',
      });
      lyr.bindPopup(zonePopupHtml(p.name, p.epsg, p.area));
      lyr.on('mouseover', function () {
        this.setStyle({ fillOpacity: 0.15 });
        if (map.getZoom() < SP_LABEL_ZOOM) this.openTooltip();
      });
      lyr.on('mouseout', function () {
        this.setStyle({ fillOpacity: 0 });
        if (map.getZoom() < SP_LABEL_ZOOM) this.closeTooltip();
      });
    },
  });

  // Below SP_LABEL_ZOOM there isn't room for every label, so they only show
  // on hover; at or above it, every zone gets a permanent label.
  function updateLabels() {
    const permanent = map.getZoom() >= SP_LABEL_ZOOM;
    layer.eachLayer(lyr => {
      lyr.unbindTooltip();
      const p = lyr.feature.properties;
      const shortName = p.name.includes(' / ') ? p.name.split(' / ')[1] : p.name;
      lyr.bindTooltip(shortName, {
        permanent, direction: 'center', className: 'zone-label',
      });
    });
  }

  map.on('zoomend', updateLabels);
  return layer;
}

let utmLayer = null;
let spLayer = null;
let utmDatum = 'wgs84';

document.getElementById('layer-utm').addEventListener('change', function () {
  if (this.checked) {
    utmLayer = buildUtmLayer(utmDatum);
    utmLayer.addTo(map);
  } else if (utmLayer) {
    map.removeLayer(utmLayer);
    utmLayer = null;
  }
});

document.getElementById('utm-datum').addEventListener('change', function () {
  utmDatum = this.value;
  // Rebuild (rather than mutate) the layer, since the datum choice changes
  // every zone's EPSG code and name, not just its style.
  if (document.getElementById('layer-utm').checked) {
    if (utmLayer) map.removeLayer(utmLayer);
    utmLayer = buildUtmLayer(utmDatum);
    utmLayer.addTo(map);
  }
});

document.getElementById('layer-sp').addEventListener('change', async function () {
  if (this.checked) {
    // Fetched once and cached in spLayer; toggling off and back on just
    // re-adds the existing layer instead of re-fetching from the server.
    if (!spLayer) spLayer = await buildStatePlaneLayer();
    spLayer.addTo(map);
  } else if (spLayer) {
    map.removeLayer(spLayer);
  }
});


// ======== LIGHTBOX ========
const lightbox = document.getElementById('lightbox');
const lightboxStage = document.getElementById('lightbox-stage');
const lightboxImg = document.getElementById('lightbox-img');
const lightboxClose = document.getElementById('lightbox-close');

// Pan/zoom state for the lightbox image: scale plus x/y translation, and a
// separate set of variables to track an in-progress drag.
let lbScale = 1, lbTx = 0, lbTy = 0;
let lbDragging = false, lbDragStartX = 0, lbDragStartY = 0, lbDragTx = 0, lbDragTy = 0;

function openLightbox(src) {
  lightboxImg.src = src;
  lbScale = 1; lbTx = 0; lbTy = 0;
  applyLbTransform();
  lightbox.style.display = 'flex';
}

function closeLightbox() {
  lightbox.style.display = 'none';
  // removeAttribute rather than src = '': an empty string src can make some
  // browsers re-request the current page as an "image".
  lightboxImg.removeAttribute('src');
  lbDragging = false;
}

function applyLbTransform() {
  lightboxImg.style.transform = `translate(${lbTx}px, ${lbTy}px) scale(${lbScale})`;
}

lightboxClose.addEventListener('click', e => {
  e.stopPropagation();
  closeLightbox();
});

// Clicking the backdrop closes the lightbox; clicking the image must not
// (that's how dragging starts), hence the target === stage check.
lightboxStage.addEventListener('click', e => {
  if (e.target === lightboxStage) closeLightbox();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && lightbox.style.display !== 'none') closeLightbox();
});

lightboxImg.addEventListener('dblclick', () => {
  lbScale = 1; lbTx = 0; lbTy = 0;
  applyLbTransform();
});

lightboxImg.addEventListener('wheel', e => {
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  lbScale = Math.min(Math.max(lbScale * factor, 0.25), 12);
  applyLbTransform();
}, { passive: false });

lightboxImg.addEventListener('mousedown', e => {
  e.preventDefault();
  lbDragging = true;
  lbDragStartX = e.clientX;
  lbDragStartY = e.clientY;
  lbDragTx = lbTx;
  lbDragTy = lbTy;
  lightboxImg.classList.add('dragging');
});

// Drag tracking lives on `document`, not the image, so the drag continues
// smoothly even if the cursor briefly leaves the image while moving fast.
document.addEventListener('mousemove', e => {
  if (!lbDragging) return;
  lbTx = lbDragTx + (e.clientX - lbDragStartX);
  lbTy = lbDragTy + (e.clientY - lbDragStartY);
  applyLbTransform();
});

document.addEventListener('mouseup', () => {
  if (!lbDragging) return;
  lbDragging = false;
  lightboxImg.classList.remove('dragging');
});


// ======== SOURCE PATH AUTO-SLASH ========
// On blur, make sure the Photo Source field ends in a separator so the
// backend can safely concatenate it with each filename. Separator style
// (\ vs /) is inferred from what's already typed, so Windows paths keep
// backslashes and URL/Unix paths keep forward slashes.
document.getElementById('source-path').addEventListener('blur', function () {
  const val = this.value.trim();
  if (!val) { this.value = ''; return; }
  if (!/[/\\]$/.test(val)) {
    this.value = val + (val.includes('\\') ? '\\' : '/');
  } else {
    this.value = val;
  }
});


// ======== UTILITIES ========
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
