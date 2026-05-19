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


// ======== STATE ========
const MAX_PHOTOS = 10;
let photos = []; // { filename, lat, lon, datetime, marker }


// ======== DOM REFS ========
const dropZone    = document.getElementById('drop-zone');
const fileInput   = document.getElementById('file-input');
const statusEl    = document.getElementById('status');
const resultsSec  = document.getElementById('results-section');
const resultsList = document.getElementById('results-list');
const clearBtn    = document.getElementById('clear-btn');


// ======== FILE HANDLING ========
dropZone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  handleFiles(fileInput.files);
  fileInput.value = '';
});

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  handleFiles(e.dataTransfer.files);
});

function isAccepted(file) {
  const type = file.type.toLowerCase();
  const name = file.name.toLowerCase();
  return (
    type === 'image/jpeg' ||
    type === 'image/heic' ||
    type === 'image/heif' ||
    name.endsWith('.jpg') ||
    name.endsWith('.jpeg') ||
    name.endsWith('.heic') ||
    name.endsWith('.heif')
  );
}

async function handleFiles(fileList) {
  if (typeof exifr === 'undefined') {
    statusEl.textContent = 'Error: EXIF library failed to load. Reload the page.';
    return;
  }

  const incoming = Array.from(fileList).filter(isAccepted);

  if (incoming.length === 0) {
    statusEl.textContent = 'No JPEG or HEIC files found in selection.';
    return;
  }

  const slots = MAX_PHOTOS - photos.length;
  if (slots <= 0) {
    statusEl.textContent = `Session limit reached (${MAX_PHOTOS} photos max). Use Clear All to start over.`;
    return;
  }

  const toProcess = incoming.slice(0, slots);
  const overLimit = incoming.length - toProcess.length;

  statusEl.textContent = `Reading EXIF from ${toProcess.length} photo${toProcess.length !== 1 ? 's' : ''}…`;

  let mapped = 0;
  let noGps  = 0;

  for (const file of toProcess) {
    try {
      const [gps, meta] = await Promise.all([
        exifr.gps(file).catch(() => null),
        exifr.parse(file, ['DateTimeOriginal']).catch(() => null),
      ]);

      if (!gps || gps.latitude == null || gps.longitude == null) {
        noGps++;
        continue;
      }

      const lat      = gps.latitude;
      const lon      = gps.longitude;
      const datetime = meta?.DateTimeOriginal ? formatDatetime(meta.DateTimeOriginal) : null;

      const marker = addMarker(file.name, lat, lon, datetime);
      photos.push({ filename: file.name, lat, lon, datetime, marker });
      addResultItem(file.name, lat, lon, datetime, marker);
      mapped++;
    } catch (_) {
      noGps++;
    }
  }

  const parts = [];
  if (mapped > 0)    parts.push(`${mapped} photo${mapped !== 1 ? 's' : ''} mapped`);
  if (noGps > 0)     parts.push(`${noGps} had no GPS data`);
  if (overLimit > 0) parts.push(`${overLimit} skipped (session limit)`);

  const totals = photos.length > 0 ? ` · ${photos.length}/${MAX_PHOTOS} total` : '';
  statusEl.textContent = (parts.join(' · ') || 'Nothing to map') + totals + '.';

  if (photos.length > 0) {
    resultsSec.style.display = 'flex';
    fitMapToPhotos();
  }
}


// ======== MARKERS ========
function addMarker(filename, lat, lon, datetime) {
  const marker = L.circleMarker([lat, lon], {
    radius: 8,
    fillColor: '#00d2ff',
    color: '#ffffff',
    weight: 2,
    fillOpacity: 0.8,
  });

  const metaLines = [
    `Lat: ${lat.toFixed(6)}`,
    `Lon: ${lon.toFixed(6)}`,
  ];
  if (datetime) metaLines.unshift(`Time: ${escapeHtml(datetime)}`);

  const content = `<div class="photo-popup">
    <div class="popup-meta">
      <strong>${escapeHtml(filename)}</strong>
      ${metaLines.join('<br>')}
    </div>
  </div>`;

  marker.bindPopup(content, { maxWidth: 220 });
  markerLayer.addLayer(marker);
  return marker;
}

function fitMapToPhotos() {
  if (photos.length === 0) return;
  const bounds = L.latLngBounds(photos.map(p => [p.lat, p.lon]));
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
}


// ======== RESULTS LIST ========
function addResultItem(filename, lat, lon, datetime, marker) {
  const li = document.createElement('li');
  li.innerHTML = `
    <div class="result-text">
      <div class="result-filename">${escapeHtml(filename)}</div>
      <div class="result-coords">${lat.toFixed(5)}, ${lon.toFixed(5)}</div>
      ${datetime ? `<div class="result-dt">${escapeHtml(datetime)}</div>` : ''}
    </div>
  `;
  li.addEventListener('click', () => {
    map.flyTo([lat, lon], 16);
    marker.openPopup();
  });
  resultsList.appendChild(li);
}


// ======== CLEAR ALL ========
clearBtn.addEventListener('click', () => {
  photos = [];
  markerLayer.clearLayers();
  resultsList.innerHTML = '';
  resultsSec.style.display = 'none';
  statusEl.textContent = 'Cleared. Ready for new photos.';
});


// ======== UTILITIES ========
function formatDatetime(dt) {
  if (dt instanceof Date) {
    const pad = n => String(n).padStart(2, '0');
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ` +
           `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
  }
  return String(dt);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
