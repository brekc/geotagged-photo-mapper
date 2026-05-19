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
let photos = [];


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
    type === 'image/jpeg' || type === 'image/heic' || type === 'image/heif' ||
    name.endsWith('.jpg') || name.endsWith('.jpeg') ||
    name.endsWith('.heic') || name.endsWith('.heif')
  );
}

async function handleFiles(fileList) {
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
    const exif = await readJpegGps(file);

    if (!exif || exif.lat == null || exif.lon == null) {
      noGps++;
      continue;
    }

    const marker = addMarker(file.name, exif.lat, exif.lon, exif.datetime);
    photos.push({ filename: file.name, lat: exif.lat, lon: exif.lon, datetime: exif.datetime, marker });
    addResultItem(file.name, exif.lat, exif.lon, exif.datetime, marker);
    mapped++;
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

  const metaLines = [`Lat: ${lat.toFixed(6)}`, `Lon: ${lon.toFixed(6)}`];
  if (datetime) metaLines.unshift(`Time: ${escapeHtml(datetime)}`);

  marker.bindPopup(`<div class="photo-popup"><div class="popup-meta">
    <strong>${escapeHtml(filename)}</strong>
    ${metaLines.join('<br>')}
  </div></div>`, { maxWidth: 220 });

  markerLayer.addLayer(marker);
  return marker;
}

function fitMapToPhotos() {
  if (!photos.length) return;
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
    </div>`;
  li.addEventListener('click', () => { map.flyTo([lat, lon], 16); marker.openPopup(); });
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


// ======== INLINE JPEG EXIF / GPS PARSER ========
// Reads only the first 128 KB of the file (EXIF is always near the start).
// Returns { lat, lon, datetime } or null.

async function readJpegGps(file) {
  try {
    const buf  = await file.slice(0, 131072).arrayBuffer();
    const view = new DataView(buf);

    if (view.getUint16(0) !== 0xFFD8) return null; // not a JPEG

    let pos = 2;
    while (pos + 4 <= view.byteLength) {
      const marker = view.getUint16(pos);
      if (marker === 0xFFDA) break; // start of image data — stop

      const segLen = view.getUint16(pos + 2); // includes the 2-byte length field

      // APP1 with Exif signature
      if (marker === 0xFFE1 && segLen > 10) {
        const sig = readAscii(view, pos + 4, 4);
        if (sig === 'Exif') {
          // TIFF data starts after "Exif\0\0" (6 bytes after APP1 data start)
          const result = parseTiff(view, pos + 10);
          if (result) return result;
        }
      }

      pos += 2 + segLen;
    }
  } catch (_) {}
  return null;
}

function parseTiff(view, base) {
  if (base + 8 > view.byteLength) return null;

  const order = view.getUint16(base);
  const le    = order === 0x4949; // 'II' = little-endian, 'MM' = big-endian

  if (view.getUint16(base + 2, le) !== 42) return null; // TIFF magic

  const ifd0Off = view.getUint32(base + 4, le);
  const ifd0    = readIfd(view, base, ifd0Off, le);

  // GPS IFD pointer
  const gpsOff = ifd0[0x8825];
  if (!gpsOff) return null;
  const gps = readIfd(view, base, gpsOff, le);

  const latRef = gps[0x01];
  const latRaw = gps[0x02];
  const lonRef = gps[0x03];
  const lonRaw = gps[0x04];
  if (!latRaw || !lonRaw) return null;

  let lat = dmsToDecimal(latRaw);
  let lon = dmsToDecimal(lonRaw);
  if (latRef === 'S') lat = -lat;
  if (lonRef === 'W') lon = -lon;

  // DateTimeOriginal from Exif sub-IFD
  let datetime = null;
  const exifOff = ifd0[0x8769];
  if (exifOff) {
    const exif = readIfd(view, base, exifOff, le);
    const raw  = exif[0x9003]; // DateTimeOriginal: "YYYY:MM:DD HH:MM:SS"
    if (raw) datetime = raw.slice(0, 10).replace(/:/g, '-') + raw.slice(10);
  }

  return { lat, lon, datetime };
}

function readIfd(view, base, ifdOff, le) {
  const tags  = {};
  const start = base + ifdOff;
  if (start + 2 > view.byteLength) return tags;

  const count = view.getUint16(start, le);

  for (let i = 0; i < count; i++) {
    const e    = start + 2 + i * 12;
    if (e + 12 > view.byteLength) break;

    const tag  = view.getUint16(e, le);
    const type = view.getUint16(e + 2, le);
    const num  = view.getUint32(e + 4, le);
    const fits = typeSz(type) * num <= 4;
    const dOff = fits ? e + 8 : base + view.getUint32(e + 8, le);

    if (type === 2) {
      // ASCII string
      tags[tag] = readAscii(view, dOff, num - 1);
    } else if (type === 4 || type === 9) {
      // LONG / SLONG — used for sub-IFD offsets
      tags[tag] = view.getUint32(dOff, le);
    } else if (type === 5) {
      // UNSIGNED RATIONAL — GPS coordinates
      const vals = [];
      for (let j = 0; j < num; j++) {
        const n = view.getUint32(dOff + j * 8,     le);
        const d = view.getUint32(dOff + j * 8 + 4, le);
        vals.push(d ? n / d : 0);
      }
      tags[tag] = vals;
    }
  }

  return tags;
}

function typeSz(t) {
  return { 1:1, 2:1, 3:2, 4:4, 5:8, 6:1, 7:1, 8:2, 9:4, 10:8, 11:4, 12:8 }[t] || 1;
}

function readAscii(view, off, len) {
  let s = '';
  for (let i = 0; i < len; i++) {
    const c = view.getUint8(off + i);
    if (!c) break;
    s += String.fromCharCode(c);
  }
  return s;
}

function dmsToDecimal([deg, min, sec]) {
  return deg + min / 60 + (sec || 0) / 3600;
}


// ======== UTILITIES ========
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
