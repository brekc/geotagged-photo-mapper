// ======== MAP INIT ========
const map = L.map('map').setView([20, 0], 2);

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

const markerLayer = L.layerGroup().addTo(map);


// ======== FILE HANDLING ========
const dropZone    = document.getElementById('drop-zone');
const fileInput   = document.getElementById('file-input');
const uploadBtn   = document.getElementById('upload-btn');
const statusEl    = document.getElementById('status');

let selectedFiles = [];
let photoURLs = new Map();

function setFiles(files) {
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

  const layer = L.geoJSON(geojson, {
    pointToLayer(feature, latlng) {
      return L.circleMarker(latlng, {
        radius: 8,
        fillColor: '#00d2ff',
        color: '#ffffff',
        weight: 2,
        fillOpacity: 0.8,
      });
    },
    onEachFeature(feature, layer) {
      const p = feature.properties || {};
      const imgUrl = photoURLs.get(p.filename);

      const meta = [];
      if (p.datetime)           meta.push(`Time: ${p.datetime}`);
      if (p.camera_model)       meta.push(`Camera: ${p.camera_model}`);
      if (p.altitude_m != null) meta.push(`Alt: ${Number(p.altitude_m).toFixed(1)} m / ${Number(p.altitude_ft).toFixed(1)} ft`);

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

      layer.bindPopup(content, { maxWidth: 240 });
    },
  });

  markerLayer.addLayer(layer);

  try {
    const bounds = layer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
  } catch (_) {}
}


// ======== CRS SELECTION ========
const regionSelect      = document.getElementById('region-select');
const crsOptionsSelect  = document.getElementById('crs-options-select');
const crsSelectedLabel  = document.getElementById('crs-selected-label');
const commonCrsSelect   = document.getElementById('common-crs-select');
const commonCrsSection  = document.getElementById('common-crs-section');
const regionCrsSection  = document.getElementById('region-crs-section');
const crsFilterControls = document.getElementById('crs-filter-controls');
const showAllDatumsChk  = document.getElementById('show-all-datums');

let selectedEpsg  = 4326;
let allCrsResults = [];   // full unfiltered list for current region
let activeUnits   = 'meters';

// Datum priority — lower number = newer/preferred (null = non-US, always keep)
const DATUM_PRIORITY = {
  'NAD83(2011)': 1, 'NAD83(2011)(IERS)': 1,
  'NAD83(NSRS2007)': 2, 'NAD83(PA11)': 2, 'NAD83(MA11)': 2,
  'NAD83(HARN)': 3, 'NAD83(HARN/MATLAB)': 3,
  'NAD83': 4,
  'NAD27': 5,
};

function parseCrs(name) {
  const slash = name.indexOf(' / ');
  const datum = slash >= 0 ? name.slice(0, slash) : '';
  const zone  = slash >= 0 ? name.slice(slash + 3) : name;
  const isFeet = /\(ft(?:US)?\)/i.test(zone);
  const baseZone = zone.replace(/\s*\(ft(?:US)?\)/i, '').trim();
  const datumPriority = datum in DATUM_PRIORITY ? DATUM_PRIORITY[datum] : null;
  return { datum, baseZone, isFeet, datumPriority };
}

function applyFilters(list) {
  // 1 — units filter
  let filtered = list.filter(r => {
    const { isFeet } = parseCrs(r.name);
    if (activeUnits === 'meters') return !isFeet;
    if (activeUnits === 'feet')   return isFeet;
    return true;
  });

  // 2 — latest-datum filter (skip if "show all" is checked)
  if (!showAllDatumsChk.checked) {
    const best = new Map(); // key: "baseZone|isFeet" -> best entry
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
    regionCrsSection.style.display = '';
    return;
  }
  const label = commonCrsSelect.options[commonCrsSelect.selectedIndex].text;
  selectedEpsg = code;
  crsSelectedLabel.textContent = `Using: ${label}`;
  regionSelect.value = '';
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

  commonCrsSelect.value = '';
  commonCrsSection.style.display = 'none';
  crsOptionsSelect.innerHTML = '<option value="">Loading...</option>';
  crsOptionsSelect.disabled = true;
  crsFilterControls.style.display = 'none';

  try {
    const res  = await fetch(`/crs-search?q=${encodeURIComponent(region)}`);
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
});


// ======== EXPORT / DOWNLOAD ========
const FORMAT_EXT = {
  geojson:    f => `${f}.geojson`,
  geopackage: f => `${f}.gpkg`,
  filegdb:    f => `${f}.zip`,
  shapefile:  f => `${f}.zip`,
  kml:        f => `${f}.kml`,
  csv:        f => `${f}.csv`,
};

document.getElementById('download-btn').addEventListener('click', async (e) => {
  const format     = document.getElementById('format-select').value;
  const customEpsg = document.getElementById('custom-epsg').value.trim();
  const epsg       = customEpsg !== '' ? customEpsg : String(selectedEpsg);
  const baseName   = document.getElementById('export-name').value.trim().replace(/[\\/:*?"<>|]/g, '_') || 'photo_locations';

  const formData = new FormData();
  formData.append('format', format);
  formData.append('epsg', epsg);
  formData.append('source_path', document.getElementById('source-path').value.trim());
  formData.append('flight_altitude', document.getElementById('flight-altitude').value.trim());
  formData.append('altitude_unit', document.getElementById('altitude-unit').value);

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

    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
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

  const features = (geojson.features || []);
  if (features.length === 0) return;

  features.forEach(feature => {
    const p      = feature.properties || {};
    const coords = feature.geometry?.coordinates || [];
    const lon    = coords[0], lat = coords[1];
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
    `;
    li.addEventListener('click', () => {
      if (lat != null && lon != null) map.flyTo([lat, lon], 16);
    });
    list.appendChild(li);
  });
}


// ======== REFERENCE LAYERS ========

// Shared popup: click "Use for export" to set CRS directly from the map
function setCrsForExport(epsg, name) {
  selectedEpsg = epsg;
  crsSelectedLabel.textContent = `Using: ${escapeHtml(name)} (EPSG:${epsg})`;
  document.getElementById('custom-epsg').value = epsg;
  // Clear both CRS pickers so the EPSG field is the source of truth
  commonCrsSelect.value = '';
  regionSelect.value = '';
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
function buildUtmLayer() {
  const features = [];
  for (let z = 1; z <= 60; z++) {
    const w = -180 + (z - 1) * 6, e = w + 6;
    features.push({
      type: 'Feature',
      properties: { name: `UTM Zone ${z}N`, epsg: 32600 + z, area: 'Northern Hemisphere' },
      geometry: { type: 'Polygon', coordinates: [[[w,0],[e,0],[e,84],[w,84],[w,0]]] },
    });
    features.push({
      type: 'Feature',
      properties: { name: `UTM Zone ${z}S`, epsg: 32700 + z, area: 'Southern Hemisphere' },
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
const SP_LABEL_ZOOM = 6; // show permanent labels at or above this zoom

async function buildStatePlaneLayer() {
  const res  = await fetch('/zone-geojson?type=state_plane');
  const data = await res.json();

  const layer = L.geoJSON(data, {
    style: {
      color: '#ff8800', weight: 1.5,
      fill: true, fillColor: '#ff8800', fillOpacity: 0,
    },
    onEachFeature(f, lyr) {
      const p = f.properties;
      // Short label: strip datum prefix (everything before " / ")
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

  // Switch tooltips between hover-only and permanent based on zoom
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
let spLayer  = null;

document.getElementById('layer-utm').addEventListener('change', async function () {
  if (this.checked) {
    if (!utmLayer) utmLayer = buildUtmLayer();
    utmLayer.addTo(map);
  } else if (utmLayer) {
    map.removeLayer(utmLayer);
  }
});

document.getElementById('layer-sp').addEventListener('change', async function () {
  if (this.checked) {
    if (!spLayer) spLayer = await buildStatePlaneLayer();
    spLayer.addTo(map);
  } else if (spLayer) {
    map.removeLayer(spLayer);
  }
});


// ======== LIGHTBOX ========
const lightbox      = document.getElementById('lightbox');
const lightboxStage = document.getElementById('lightbox-stage');
const lightboxImg   = document.getElementById('lightbox-img');
const lightboxClose = document.getElementById('lightbox-close');

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
  lightboxImg.src = '';
  lbDragging = false;
}

function applyLbTransform() {
  lightboxImg.style.transform = `translate(${lbTx}px, ${lbTy}px) scale(${lbScale})`;
}

lightboxClose.addEventListener('click', e => {
  e.stopPropagation();
  closeLightbox();
});

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
