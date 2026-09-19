/* exported openLightbox, setCrsForExport */
// Called from onclick="" attributes in HTML strings to builds itself
// and enable linting.

// ======== MAP INIT ========
const map = L.map('map').setView([20, 0], 2);

// Three basemap choices and switchable from the layers control top-right.
const basemaps = {
  'OpenStreetMap': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }),
  'Light Gray (Esri)': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Esri, HERE, Garmin, © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 16,
  }),
  'Imagery + Topo (USGS)': L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles courtesy of the <a href="https://usgs.gov">U.S. Geological Survey</a>',
    maxZoom: 16,
  }),
};

basemaps['OpenStreetMap'].addTo(map);
L.control.layers(basemaps, {}, { position: 'topright' }).addTo(map);

// Photo markers are in their own layer group so they can all be cleared
// without touching the reference layers.
const markerLayer = L.layerGroup().addTo(map);

// ======== STATE ========
// One entry per photo currently on the map, so the results list and the
// remove/clear buttons can find and remove the matching marker.
let mappedPhotos = []; // { filename, lat, lon, marker, row_id }

// Opaque id for this tab's upload session (see upload_sessions.py). Every
// export/Oriented Imagery call sends it; the server refuses without a match.
let currentUploadId = null;

function visibleRowIds() {
  return mappedPhotos.map(p => p.row_id).filter(Boolean);
}

// Best-effort session cleanup on Clear All / a new upload / tab close, so
// sessions don't linger for their full 15-minute TTL. sendBeacon survives
// page unload, unlike a normal fetch.
function closeSession(uploadId) {
  if (!uploadId) return;
  const url = `/session/${encodeURIComponent(uploadId)}/close`;
  if (navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob());
  } else {
    fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
  }
}

window.addEventListener('pagehide', () => closeSession(currentUploadId));

// ======== FILE HANDLING ========
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadBtn = document.getElementById('upload-btn');
const statusEl = document.getElementById('status');
const clearBtn = document.getElementById('clear-btn');

let selectedFiles = [];
// filename -> local blob URL for thumbnails. Photos do not leave the browser
// until Upload is clicked.
let photoURLs = new Map();

// Some browsers/OSes report no useful Content-Type for HEIC/HEIF (often
// "" or "application/octet-stream"), so fall back to the extension.
function isSupportedImage(f) {
  if (f.type && f.type.startsWith('image/')) return true;
  return /\.(jpe?g|png|heic|heif)$/i.test(f.name || '');
}

function setFiles(files) {
  // Revoke previous object URLs to avoid leaking them from the session
  photoURLs.forEach(url => { if (url.startsWith('blob:')) URL.revokeObjectURL(url); });
  photoURLs = new Map();

  selectedFiles = Array.from(files).filter(isSupportedImage);
  selectedFiles.forEach(f => {
    // HEIC/HEIF blob URLs won't render in most browsers' <img> tags; the
    // server-generated preview from /upload replaces these afterward.
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

  // A new upload replaces this tab's dataset; let the old session expire
  // immediately instead of lingering for its full TTL.
  const previousUploadId = currentUploadId;

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

    const { geojson, total_uploaded, total_geotagged, upload_id, previews, errors } = data;
    currentUploadId = upload_id;
    closeSession(previousUploadId);

    // Server-rendered previews (HEIC/HEIF) replace the unreliable client
    // blob URL for those filenames; JPEG/PNG keep their existing blob URL.
    if (previews) {
      Object.entries(previews).forEach(([filename, dataUri]) => {
        const existing = photoURLs.get(filename);
        if (existing && existing.startsWith('blob:')) URL.revokeObjectURL(existing);
        photoURLs.set(filename, dataUri);
      });
    }

    let statusMsg = `${total_geotagged} of ${total_uploaded} photo${total_uploaded !== 1 ? 's' : ''} had GPS data.`;
    if (errors && errors.length) {
      statusMsg += ` ${errors.length} file${errors.length !== 1 ? 's' : ''} had issues (see console for details).`;
      console.warn('Upload issues:', errors);
    }
    statusEl.textContent = statusMsg;

    plotGeoJSON(geojson);
    populateResults(geojson);

    // Show Export, Flight Details, Results, and Oriented Imagery when there
    // is at least one geotagged photo to act on.
    if (total_geotagged > 0) {
      document.getElementById('export-section').style.display = 'flex';
      document.getElementById('flight-details-section').style.display = 'flex';
      document.getElementById('results-section').style.display = 'flex';
      document.getElementById('oi-open-btn').style.display = '';
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
    mappedPhotos.push({ filename: p.filename, lat, lon, marker, row_id: p.row_id });
  });

  // Zoom/pan to fit every plotted photo. Use try/catch since
  // fitBounds may get empty/invalid bounds.
  try {
    if (mappedPhotos.length > 0) {
      const bounds = L.latLngBounds(mappedPhotos.map(ph => [ph.lat, ph.lon]));
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
    }
  } catch (_) {
    // Nothing valid to fit — fine.
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

  // Only show a thumbnail if there is a blob URL. A fresh file selection
  // clears photoURLs, so marker data can outlive its image.
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

// Datum priority: lower = newer/preferred. The filter below collapses a
// zone's older realizations to just the newest, unless "Show all" is checked.
const DATUM_PRIORITY = {
  'NAD83(2011)': 1, 'NAD83(2011)(IERS)': 1,
  'NAD83(NSRS2007)': 2, 'NAD83(PA11)': 2, 'NAD83(MA11)': 2,
  'NAD83(HARN)': 3, 'NAD83(HARN/MATLAB)': 3,
  'NAD83': 4,
  'NAD27': 5,
};

function parseCrs(name) {
  // " / " for datum vs. zone; strip the feet marker so zones group by base name.
  const slash = name.indexOf(' / ');
  const datum = slash >= 0 ? name.slice(0, slash) : '';
  const zone = slash >= 0 ? name.slice(slash + 3) : name;
  const isFeet = /\(ft(?:US)?\)/i.test(zone);
  const baseZone = zone.replace(/\s*\(ft(?:US)?\)/i, '').trim();
  const datumPriority = datum in DATUM_PRIORITY ? DATUM_PRIORITY[datum] : null;
  return { datum, baseZone, isFeet, datumPriority };
}

function applyFilters(list) {
  // 1. units filter
  let filtered = list.filter(r => {
    const { isFeet } = parseCrs(r.name);
    if (activeUnits === 'meters') return !isFeet;
    if (activeUnits === 'feet')   return isFeet;
    return true;
  });

  // 2. latest-datum filter (skip if "show all" is checked): per base zone,
  //  keep only the entry with the lowest (newest) datum priority.
  if (!showAllDatumsChk.checked) {
    const best = new Map();
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

// Units toggle
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
    // Fall back to letting the region picker be used.
    regionCrsSection.style.display = '';
    return;
  }
  const label = commonCrsSelect.options[commonCrsSelect.selectedIndex].text;
  selectedEpsg = code;
  crsSelectedLabel.textContent = `Using: ${label}`;
  // Only one CRS source should be "active" at a time.
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

  // Picking a region resets the common CRS picker.
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
  clearCustomCrs();
});

// Highest-priority CRS source: non-empty textarea at download time overrides
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

// A manually entered EPSG code is also a CRS choice.
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
  // Textarea cleared: fall back to the currently selected EPSG.
  crsSelectedLabel.textContent = `Using: EPSG:${currentEpsgValue()}`;
}

// Effective EPSG: manual field if typed or whatever is picked last.
function currentEpsgValue() {
  const customEpsg = document.getElementById('custom-epsg').value.trim();
  return customEpsg !== '' ? customEpsg : String(selectedEpsg);
}

// ======== EXPORT / DOWNLOAD ========
// File extension for each export format and matched to the
// format-select dropdown and backend's /export format handlers.
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

  if (!currentUploadId) {
    alert('Upload photos first.');
    return;
  }

  const formData = new FormData();
  formData.append('format', format);
  formData.append('upload_id', currentUploadId);
  formData.append('row_ids', visibleRowIds().join(','));
  formData.append('epsg', epsg);
  // custom_crs takes priority over epsg when non-empty.
  if (customCrs) formData.append('custom_crs', customCrs);
  formData.append('source_path', document.getElementById('source-path').value.trim());
  formData.append('flight_altitude', document.getElementById('flight-altitude').value.trim());
  formData.append('altitude_unit', document.getElementById('altitude-unit').value);
  // The backend uses this to name layers inside multi-file formats (FileGDB, Shapefile).
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

    // Trigger download via a throwaway <a download> link since the export
    // is a blob, not a navigable URL.
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

  // Hide sections when data can be shown or exported.
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
  closeOrientedImageryModal();
  closeSession(currentUploadId);
  currentUploadId = null;
  statusEl.textContent = 'Cleared. Ready for new upload.';
});

// ======== REFERENCE LAYERS ========
// Click "Use for export" in a zone popup to set that zone's CRS directly
// from the map.
function setCrsForExport(epsg, name) {
  selectedEpsg = epsg;
  crsSelectedLabel.textContent = `Using: ${escapeHtml(name)} (EPSG:${epsg})`;
  document.getElementById('custom-epsg').value = epsg;
  // Clear all CRS pickers so the EPSG field is the single source of truth.
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
// UTM zones are simple 6° rectangles generated client-side, unlike State
// Plane's irregular county-based borders which need a server fetch.
function buildUtmLayer(datum) {
  const useNad83 = datum === 'nad83';
  const features = [];
  for (let z = 1; z <= 60; z++) {
    const w = -180 + (z - 1) * 6, e = w + 6;
    // NAD83 northern zones only exist for 1-23 (North America). Higher
    // zones and southern hemisphere fall back to WGS 84.
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
const SP_LABEL_ZOOM = 6; // show labels at or above this zoom level

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
      // Strip datum prefix so tooltip reads "Washington North" not
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

  // Below SP_LABEL_ZOOM, labels only show on hover; above it, every zone
  // gets a permanent label.
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
  // Rebuild the layer since datum changes every zone's EPSG code and name.
  if (document.getElementById('layer-utm').checked) {
    if (utmLayer) map.removeLayer(utmLayer);
    utmLayer = buildUtmLayer(utmDatum);
    utmLayer.addTo(map);
  }
});

document.getElementById('layer-sp').addEventListener('change', async function () {
  if (this.checked) {
    // Fetched once and cached. Toggling off/on re-adds the existing layer.
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
  // removeAttribute rather than src = '': empty src can make browsers
  // re-request the current page as an "image".
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

// Clicking the backdrop closes the lightbox, and clicking the image must not
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

// Drag tracking on `document` so the drag continues even if the cursor
// briefly leaves the image.
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
// On blur, ensure the path ends in a separator so the backend can safely
// concatenate it with each filename.
document.getElementById('source-path').addEventListener('blur', function () {
  const val = this.value.trim();
  if (!val) { this.value = ''; return; }
  if (!/[/\\]$/.test(val)) {
    this.value = val + (val.includes('\\') ? '\\' : '/');
  } else {
    this.value = val;
  }
});

// ======== ORIENTED IMAGERY ========
const oiModal = document.getElementById('oi-modal');
const oiModalPanel = document.getElementById('oi-modal-panel');
const oiOpenBtn = document.getElementById('oi-open-btn');
const oiCloseBtn = document.getElementById('oi-modal-close');
const oiTypeSelect = document.getElementById('oi-type-select');
const oiModeButtons = document.querySelectorAll('.oi-mode-toggle .toggle-btn');
const oiReferenceFields = document.getElementById('oi-reference-fields');
const oiPortableFields = document.getElementById('oi-portable-fields');
const oiBaseLocation = document.getElementById('oi-base-location');
const oiPreviewBtn = document.getElementById('oi-preview-btn');
const oiPreviewOutput = document.getElementById('oi-preview-output');
const oiExportName = document.getElementById('oi-export-name');
const oiCrsSummary = document.getElementById('oi-crs-summary');
const oiRowStatus = document.getElementById('oi-row-status');
const oiDownloadBtn = document.getElementById('oi-download-btn');
const oiSummary = document.getElementById('oi-summary');

let oiMode = 'reference';
let oiPreviouslyFocused = null;

function setOiRowStatus(msg, isError) {
  oiRowStatus.textContent = msg || '';
  oiRowStatus.classList.toggle('oi-error', !!isError);
}

// Elements a keyboard user can reach, for the modal's Tab focus trap.
function oiFocusableElements() {
  return Array.from(oiModalPanel.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  )).filter(el => !el.disabled && el.offsetParent !== null);
}

function oiKeydownHandler(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    closeOrientedImageryModal();
    return;
  }
  if (e.key === 'Tab') {
    const focusable = oiFocusableElements();
    if (focusable.length === 0) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
}

function openOrientedImageryModal() {
  if (!currentUploadId || mappedPhotos.length === 0) {
    alert('Upload geotagged photos first.');
    return;
  }
  oiPreviouslyFocused = document.activeElement;
  oiModal.hidden = false;
  oiCrsSummary.textContent = crsSelectedLabel.textContent;
  oiPreviewOutput.classList.remove('visible');
  oiPreviewOutput.innerHTML = '';
  setOiRowStatus('');
  oiModalPanel.focus();
  loadOiPreflight();
  document.addEventListener('keydown', oiKeydownHandler);
}

function closeOrientedImageryModal() {
  if (oiModal.hidden) return;
  oiModal.hidden = true;
  document.removeEventListener('keydown', oiKeydownHandler);
  if (oiPreviouslyFocused && typeof oiPreviouslyFocused.focus === 'function') {
    oiPreviouslyFocused.focus();
  }
}

oiOpenBtn.addEventListener('click', openOrientedImageryModal);
oiCloseBtn.addEventListener('click', closeOrientedImageryModal);
document.getElementById('oi-modal-backdrop').addEventListener('click', closeOrientedImageryModal);

oiModeButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    oiMode = btn.dataset.mode;
    oiModeButtons.forEach(b => {
      const active = b === btn;
      b.classList.toggle('active', active);
      b.setAttribute('aria-checked', String(active));
    });
    oiReferenceFields.hidden = oiMode !== 'reference';
    oiPortableFields.hidden = oiMode !== 'portable';
    setOiRowStatus('');
  });
});

async function loadOiPreflight() {
  oiSummary.textContent = 'Loading…';
  try {
    const formData = new FormData();
    formData.append('upload_id', currentUploadId);
    formData.append('row_ids', visibleRowIds().join(','));
    const res = await fetch('/oriented-imagery/preflight', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      oiSummary.textContent = `Error: ${data.detail || res.statusText}`;
      return;
    }
    oiSummary.innerHTML = [
      `Total: ${data.total_files}`,
      `Valid GPS: ${data.valid_gps}`,
      `Acquisition date: ${data.acquisition_date_available}`,
      `True heading: ${data.true_heading_available}`,
      `Focal length: ${data.focal_length_available}`,
      `Approx. FOV: ${data.approximate_fov_available}`,
      `Advanced pose: ${data.advanced_pose_available}`,
      `Needs conversion: ${data.files_requiring_conversion}`,
      `Excluded: ${data.excluded_files}`,
      `Warnings: ${data.warnings}`,
    ].map(escapeHtml).join(' &middot; ');
  } catch (err) {
    oiSummary.textContent = `Network error: ${err.message}`;
  }
}

oiPreviewBtn.addEventListener('click', async () => {
  if (!oiTypeSelect.value) {
    setOiRowStatus('Select an Oriented Imagery Type first.', true);
    return;
  }
  const base = oiBaseLocation.value.trim();
  if (!base) {
    setOiRowStatus('Enter a base location first.', true);
    return;
  }
  oiPreviewBtn.disabled = true;
  try {
    const formData = new FormData();
    formData.append('upload_id', currentUploadId);
    formData.append('row_ids', visibleRowIds().join(','));
    formData.append('base_location', base);
    formData.append('oriented_imagery_type', oiTypeSelect.value);
    formData.append('epsg', currentEpsgValue());
    if (customCrsInput.value.trim()) formData.append('custom_crs', customCrsInput.value.trim());

    const res = await fetch('/oriented-imagery/reference-preview', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      setOiRowStatus(`Error: ${data.detail || res.statusText}`, true);
      return;
    }
    const lines = (data.preview_paths || []).map(escapeHtml).join('<br>');
    oiPreviewOutput.innerHTML = `
      <strong>${data.row_count} image${data.row_count !== 1 ? 's' : ''} would be included</strong>${data.excluded_count ? `, ${data.excluded_count} excluded` : ''}.<br>
      ${lines || '(no images matched)'}<br><br>
      <em>${escapeHtml(data.note || '')}</em>
    `;
    oiPreviewOutput.classList.add('visible');
    setOiRowStatus('');
  } catch (err) {
    setOiRowStatus(`Network error: ${err.message}`, true);
  } finally {
    oiPreviewBtn.disabled = false;
  }
});

oiDownloadBtn.addEventListener('click', async () => {
  if (!oiTypeSelect.value) {
    setOiRowStatus('Select an Oriented Imagery Type first.', true);
    return;
  }
  const exportName = oiExportName.value.trim().replace(/[\\/:*?"<>|]/g, '_') || 'oriented_imagery';
  oiDownloadBtn.disabled = true;
  setOiRowStatus('Building…');

  try {
    let res;
    if (oiMode === 'reference') {
      const base = oiBaseLocation.value.trim();
      if (!base) {
        setOiRowStatus('Enter a base location first.', true);
        return;
      }
      const formData = new FormData();
      formData.append('upload_id', currentUploadId);
      formData.append('row_ids', visibleRowIds().join(','));
      formData.append('base_location', base);
      formData.append('oriented_imagery_type', oiTypeSelect.value);
      formData.append('epsg', currentEpsgValue());
      if (customCrsInput.value.trim()) formData.append('custom_crs', customCrsInput.value.trim());
      formData.append('export_name', exportName);
      res = await fetch('/oriented-imagery/reference', { method: 'POST', body: formData });
    } else {
      const visibleNames = new Set(mappedPhotos.map(p => p.filename));
      const filesToRepost = selectedFiles.filter(f => visibleNames.has(f.name));
      if (filesToRepost.length === 0) {
        setOiRowStatus('No currently-mapped photos are available to repost. Upload again if the page was reloaded.', true);
        return;
      }
      const formData = new FormData();
      formData.append('upload_id', currentUploadId);
      formData.append('oriented_imagery_type', oiTypeSelect.value);
      formData.append('epsg', currentEpsgValue());
      if (customCrsInput.value.trim()) formData.append('custom_crs', customCrsInput.value.trim());
      formData.append('export_name', exportName);
      filesToRepost.forEach(f => formData.append('photos', f));
      res = await fetch('/oriented-imagery/portable', { method: 'POST', body: formData });
    }

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setOiRowStatus(`Error: ${data.detail || res.statusText}`, true);
      return;
    }

    const blob = await res.blob();
    const ext = oiMode === 'reference' ? 'csv' : 'zip';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${exportName}.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setOiRowStatus('Download complete.');
  } catch (err) {
    setOiRowStatus(`Network error: ${err.message}`, true);
  } finally {
    oiDownloadBtn.disabled = false;
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
