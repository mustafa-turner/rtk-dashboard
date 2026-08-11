import {
  countLabel,
  deviceTypeLabel,
  metricDefinitions,
  supportsPeerSafety,
} from "/device-ui.js";

const state = {
  data: null,
  selectedId: null,
  map: null,
  tilesetBounds: null,
  deviceMarkers: new Map(),
  peerMarkers: new Map(),
  eventStreamConnected: false,
  lastVersion: -1,
  stateFetchInFlight: false,
  fallbackPollMs: 5000,
  currentView: "live",
  statistics: null,
  statisticsFetchInFlight: false,
  statisticsRange: "24h",
  statisticsStream: null,
  statisticsStreamKey: "",
  statisticsStreamConnected: false,
  statisticsStreamVersion: -1,
  snapshotServerNowMs: 0,
  snapshotLocalReceivedMs: 0,
  liveSnapshot: null,
  replay: {
    range: null,
    atMs: 0,
    playing: false,
    speed: 1,
    timer: null,
    lastTickMs: 0,
    fetchInFlight: false,
    pendingAtMs: null,
  },
};

const ROVER_DISCONNECTED_MS = 5000;
const ROVER_ICON_HIDE_ZOOM = 14;
const ROVER_ICON_BASE_ZOOM = 16;
const ROVER_ICON_URL = "/icons/CC.png";
const ROVER_ICON_ROTATION_DEG = 27;
const ROVER_ICON_SOURCE_WIDTH = 124;
const ROVER_ICON_SOURCE_HEIGHT = 435;
const ROVER_ICON_BASE_WIDTH = 9.4;
const ROVER_ICON_MIN_WIDTH = 4.7;
const ROVER_ICON_MAX_WIDTH = 999;
const ROVER_ICON_ASPECT_RATIO = 435 / 124;

const fixLabels = {
  0: "NO FIX",
  1: "GNSS FIX",
  2: "DGPS",
  3: "RTK FLOAT",
  4: "RTK FIXED",
};

const ntripLabels = {
  0: "DISCONNECTED",
  1: "CONNECTED",
};

function byId(id) {
  return document.getElementById(id);
}

function valueOrDash(value, suffix = "") {
  if (value === null || value === undefined || value === "" || Number.isNaN(value)) {
    return "-";
  }
  return `${value}${suffix}`;
}

function numeric(value, digits = 1, suffix = "") {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  return `${number.toFixed(digits)}${suffix}`;
}

function ageLabel(lastSeenMs, nowMs) {
  if (!lastSeenMs) {
    return "never";
  }
  const seconds = Math.max(0, Math.round((nowMs - lastSeenMs) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  return `${Math.round(seconds / 60)}m`;
}

function dateTimeLabel(ms) {
  const number = Number(ms);
  if (!Number.isFinite(number) || number <= 0) {
    return "-";
  }
  return new Date(number).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function timeLabel(ms) {
  const number = Number(ms);
  if (!Number.isFinite(number) || number <= 0) {
    return "-";
  }
  return new Date(number).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function inputDateTimeValue(ms) {
  const number = Number(ms);
  if (!Number.isFinite(number) || number <= 0) {
    return "";
  }
  const date = new Date(number);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 19);
}

function msFromInputDateTime(value) {
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function percent(numerator, denominator, digits = 0) {
  const top = Number(numerator);
  const bottom = Number(denominator);
  if (!Number.isFinite(top) || !Number.isFinite(bottom) || bottom <= 0) {
    return "-";
  }
  return `${((top / bottom) * 100).toFixed(digits)}%`;
}

function snapshotNowMs(snapshot) {
  const serverNowMs = Number(snapshot?.server?.now_ms) || state.snapshotServerNowMs;
  if (snapshot?.server?.replay) {
    return serverNowMs || Date.now();
  }
  const localReceivedMs = Number(state.snapshotLocalReceivedMs);
  if (!Number.isFinite(serverNowMs) || serverNowMs <= 0) {
    return Date.now();
  }
  if (!Number.isFinite(localReceivedMs) || localReceivedMs <= 0) {
    return serverNowMs;
  }
  return serverNowMs + Math.max(0, Date.now() - localReceivedMs);
}

function snapshotVersion(snapshot) {
  const version = Number(snapshot?.version);
  return Number.isFinite(version) ? version : -1;
}

function ageMsFromLastSeen(lastSeenMs, snapshot) {
  lastSeenMs = Number(lastSeenMs);
  if (!Number.isFinite(lastSeenMs) || lastSeenMs <= 0) {
    return null;
  }
  return Math.max(0, snapshotNowMs(snapshot) - lastSeenMs);
}

function deviceTelemetrySeenMs(device) {
  return (
    Number(device?.last_telemetry_seen_ms) ||
    Number(device?.last_position_seen_ms) ||
    0
  );
}

function roverAgeMs(rover, snapshot) {
  return ageMsFromLastSeen(rover?.lastSeenMs, snapshot);
}

function roverPositionAgeMs(rover, snapshot) {
  return ageMsFromLastSeen(rover?.lastPositionSeenMs, snapshot);
}

function roverIsDisconnectedForSafety(rover, snapshot) {
  if (!rover || rover.kind === "placeholder" || rover.kind === "nearest") {
    return true;
  }
  const ageMs = roverAgeMs(rover, snapshot);
  return ageMs === null || ageMs > ROVER_DISCONNECTED_MS;
}

function roverIsWaitingForPosition(rover, snapshot) {
  if (!rover || rover.kind === "placeholder" || rover.kind === "nearest") {
    return true;
  }
  const ageMs = roverPositionAgeMs(rover, snapshot);
  return ageMs === null || ageMs > ROVER_DISCONNECTED_MS;
}

function deviceIsDisconnected(device, snapshot) {
  const ageMs = ageMsFromLastSeen(deviceTelemetrySeenMs(device), snapshot);
  return ageMs === null || ageMs > ROVER_DISCONNECTED_MS;
}

function statusClassForFix(fixMode) {
  if (fixMode === 4) return "good";
  if (fixMode === 3 || fixMode === 2) return "warn";
  return "bad";
}

function safeDistanceClass(value) {
  if (value === null || value === undefined || value === "") return "unknown";
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "unknown";
  if (number < 25) return "bad";
  return "good";
}

function safeDistanceLabel(value) {
  const status = safeDistanceClass(value);
  if (status === "good") return "SAFE";
  if (status === "bad") return "DANGER";
  return "WAITING";
}

function safeDistanceValue(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return "-";
  }
  if (number >= 100) {
    return number.toFixed(1);
  }
  return number.toFixed(2);
}

const nameCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const roverNameFields = [
  "display_name",
  "displayName",
  "rover_name",
  "roverName",
  "robot_name",
  "robotName",
  "device_name",
  "deviceName",
  "Device Name",
  "thing_name",
  "Thing Name",
  "hostname",
  "host_name",
  "hostName",
  "Name",
  "name",
];

function firstTextValue(source, keys) {
  if (!source) return "";
  for (const key of keys) {
    const value = source[key];
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function firstValue(source, keys) {
  if (!source) return undefined;
  for (const key of keys) {
    const value = source[key];
    if (value !== null && value !== undefined && value !== "") return value;
  }
  return undefined;
}

function displayNameForDevice(device) {
  return (
    firstTextValue(device, ["display_name", "displayName"]) ||
    firstTextValue(device?.telemetry, roverNameFields) ||
    firstTextValue(device?.info, roverNameFields) ||
    firstTextValue(device, ["device_id", "mqtt_client_id", "source_host"]) ||
    "-"
  );
}

function displayNameForPeer(peer) {
  return (
    firstTextValue(peer, ["display_name", "displayName"]) ||
    firstTextValue(peer, roverNameFields) ||
    firstTextValue(peer, ["device_id", "source_host"]) ||
    "-"
  );
}

function compareByDisplayName(a, b, nameGetter) {
  const byName = nameCollator.compare(nameGetter(a), nameGetter(b));
  if (byName !== 0) return byName;
  return nameCollator.compare(String(a.device_id || ""), String(b.device_id || ""));
}

function sortedDevices(snapshot) {
  return Object.values(snapshot?.devices || {}).sort((a, b) => compareByDisplayName(a, b, displayNameForDevice));
}

function sortedPeers(snapshot) {
  return Object.values(snapshot?.peers || {}).sort((a, b) => compareByDisplayName(a, b, displayNameForPeer));
}

function normalizedId(value) {
  return String(value ?? "").trim().toLowerCase();
}

function deviceIdentifiers(device) {
  return [
    device?.device_id,
    displayNameForDevice(device),
    device?.mqtt_client_id,
    device?.username,
    device?.source_host,
    firstTextValue(device?.telemetry, roverNameFields),
    firstTextValue(device?.info, roverNameFields),
  ].filter(Boolean);
}

function peerIdentifiers(peer) {
  return [
    peer?.device_id,
    displayNameForPeer(peer),
    peer?.source_host,
    firstTextValue(peer, roverNameFields),
  ].filter(Boolean);
}

function findDeviceByIdentifier(snapshot, identifier, excludeDeviceId = "") {
  const needle = normalizedId(identifier);
  if (!needle) return null;
  return (
    sortedDevices(snapshot).find((device) => {
      if (excludeDeviceId && device.device_id === excludeDeviceId) return false;
      return deviceIdentifiers(device).some((value) => normalizedId(value) === needle);
    }) || null
  );
}

function findPeerByIdentifier(snapshot, identifier) {
  const needle = normalizedId(identifier);
  if (!needle) return null;
  return sortedPeers(snapshot).find((peer) => peerIdentifiers(peer).some((value) => normalizedId(value) === needle)) || null;
}

function fallbackDeviceForPair(snapshot, device, peerId) {
  const otherDevices = sortedDevices(snapshot).filter((candidate) => candidate.device_id !== device?.device_id);
  if (!otherDevices.length) return null;
  if (!peerId || otherDevices.length === 1) return otherDevices[0];
  return null;
}

function getLatLng(telemetry) {
  const lat = Number(telemetry.latitude);
  const lon = Number(telemetry.longitude);
  if (Number.isFinite(lat) && Number.isFinite(lon) && (lat !== 0 || lon !== 0)) {
    return [lat, lon];
  }
  if (Array.isArray(telemetry.position) && telemetry.position.length >= 2) {
    const posLon = Number(telemetry.position[0]);
    const posLat = Number(telemetry.position[1]);
    if (Number.isFinite(posLat) && Number.isFinite(posLon) && (posLat !== 0 || posLon !== 0)) {
      return [posLat, posLon];
    }
  }
  return null;
}

function roverIconSizeForZoom() {
  const zoom = state.map?.getZoom() ?? ROVER_ICON_BASE_ZOOM;

  if (zoom < ROVER_ICON_HIDE_ZOOM) {
    return null;
  }

  const scale = 2 ** (zoom - ROVER_ICON_BASE_ZOOM);
  const width = Math.max(
    ROVER_ICON_MIN_WIDTH,
    Math.min(ROVER_ICON_MAX_WIDTH, ROVER_ICON_BASE_WIDTH * scale)
  );
  const height = Math.round(width * ROVER_ICON_ASPECT_RATIO);

  return { width, height };
}

function roverAntennaOffsetConfig() {
  const configured = state.data?.server?.dashboard?.roverAntennaOffset || {};
  const x = Number(configured.x);
  const y = Number(configured.y);
  return {
    x: Number.isFinite(x) ? x : 0,
    y: Number.isFinite(y) ? y : 0,
  };
}

function rotateScreenOffset(x, y, degrees) {
  const radians = (degrees * Math.PI) / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return {
    x: x * cos - y * sin,
    y: x * sin + y * cos,
  };
}

function roverAntennaOffsetForSize(size) {
  const configured = roverAntennaOffsetConfig();
  const scaled = {
    x: (configured.x * size.width) / ROVER_ICON_SOURCE_WIDTH,
    y: (configured.y * size.height) / ROVER_ICON_SOURCE_HEIGHT,
  };
  return rotateScreenOffset(scaled.x, scaled.y, ROVER_ICON_ROTATION_DEG);
}

function roverIconForMarker({ variant, status }) {
  if (variant === "generic-device") {
    return L.divIcon({
      className: `generic-device-marker ${status}`,
      html: `<span aria-hidden="true"></span>`,
      iconSize: [20, 20],
      iconAnchor: [10, 10],
      popupAnchor: [0, -12],
    });
  }
  const size = roverIconSizeForZoom();
  if (!size) return null;
  const antennaOffset = roverAntennaOffsetForSize(size);
  const anchorX = size.width / 2 + antennaOffset.x;
  const anchorY = size.height / 2 + antennaOffset.y;

  return L.divIcon({
    className: `rover-image-marker ${variant} ${status}`,
    html: `
      <div
        class="rover-image-marker-body"
        style="--rover-icon-rotation: ${ROVER_ICON_ROTATION_DEG}deg; --rover-antenna-x: ${antennaOffset.x}px; --rover-antenna-y: ${antennaOffset.y}px;"
      >
        <img src="${ROVER_ICON_URL}" alt="">
        <span class="rover-image-marker-dot" aria-hidden="true"></span>
      </div>
    `,
    iconSize: [size.width, size.height],
    iconAnchor: [anchorX, anchorY],
    popupAnchor: [0, -anchorY],
  });
}

function createRoverMarker(latLng, options) {
  const icon = roverIconForMarker(options);
  if (!icon) return null;

  const marker = L.marker(latLng, {
    icon,
    zIndexOffset: options.zIndexOffset || 0,
  });

  return marker.addTo(state.map);
}

function updateRoverMarker(marker, latLng, options) {
  const icon = roverIconForMarker(options);

  if (!icon) {
    if (marker) marker.remove();
    return null;
  }

  if (!marker) {
    return createRoverMarker(latLng, options);
  }

  marker.setLatLng(latLng);
  marker.setIcon(icon);
  return marker;
}

function primaryTileset(config) {
  const tilesets = Array.isArray(config?.mbtiles) ? config.mbtiles : [];
  return tilesets.find((tileset) => String(tileset.id || "").toLowerCase() === "psp") || tilesets[0] || null;
}

function leafletBoundsFromTileset(tileset) {
  const bounds = tileset?.bounds;
  if (!Array.isArray(bounds) || bounds.length !== 4) {
    return null;
  }
  const [west, south, east, north] = bounds.map(Number);
  if (![west, south, east, north].every(Number.isFinite)) {
    return null;
  }
  return L.latLngBounds([south, west], [north, east]);
}

function tileUrlForCoords(tileUrl, coords) {
  return tileUrl
    .replace("{z}", coords.z)
    .replace("{x}", coords.x)
    .replace("{y}", coords.y);
}

function makeWhiteTransparent(imageData) {
  const data = imageData.data;
  for (let index = 0; index < data.length; index += 4) {
    const red = data[index];
    const green = data[index + 1];
    const blue = data[index + 2];
    const brightest = Math.max(red, green, blue);
    const darkest = Math.min(red, green, blue);
    const brightness = (red + green + blue) / 3;
    const chroma = brightest - darkest;

    if (brightness > 238 && chroma < 28) {
      data[index + 3] = 0;
    } else if (brightness > 205 && chroma < 42) {
      data[index + 3] = Math.min(data[index + 3], Math.round(((238 - brightness) / 33) * 255));
    }
  }
}

function createTransparentMbtilesLayer(tileset) {
  return L.GridLayer.extend({
    createTile(coords, done) {
      const tileSize = this.getTileSize();
      const tile = document.createElement("canvas");
      tile.width = tileSize.x;
      tile.height = tileSize.y;

      const image = new Image();
      tile._mbtilesImage = image;
      image.onload = () => {
        const context = tile.getContext("2d");
        context.drawImage(image, 0, 0, tile.width, tile.height);
        try {
          const imageData = context.getImageData(0, 0, tile.width, tile.height);
          makeWhiteTransparent(imageData);
          context.putImageData(imageData, 0, 0);
        } catch (error) {
          console.warn("Unable to mask MBTiles no-data pixels", error);
        }
        done(null, tile);
      };
      image.onerror = () => {
        done(null, tile);
      };
      image.src = tileUrlForCoords(tileset.tileUrl, coords);
      return tile;
    },
  });
}

function addMbtilesOverlay(config) {
  const tileset = primaryTileset(config);
  if (!tileset?.tileUrl) {
    state.tilesetBounds = null;
    return;
  }

  const bounds = leafletBoundsFromTileset(tileset);
  state.tilesetBounds = bounds;
  const pane = state.map.getPane("mbtilesPane") || state.map.createPane("mbtilesPane");
  pane.style.zIndex = 350;
  pane.style.pointerEvents = "none";

  const MaskedMbtilesLayer = createTransparentMbtilesLayer(tileset);
  new MaskedMbtilesLayer({
    minZoom: Number(tileset.minZoom) || 0,
    maxZoom: 22,
    maxNativeZoom: Number(tileset.maxZoom) || 21,
    pane: "mbtilesPane",
    zIndex: 350,
    attribution: escapeHtml(tileset.name || tileset.id || "MBTiles overlay"),
  }).addTo(state.map);

  if (bounds) {
    fitToTilesetBounds(tileset);
  }
}

function fitToTilesetBounds(tileset) {
  if (!state.map || !state.tilesetBounds) return;
  window.requestAnimationFrame(() => {
    state.map.invalidateSize();
    state.map.fitBounds(state.tilesetBounds, {
      padding: [24, 24],
      maxZoom: Number(tileset?.maxZoom) || 21,
    });
  });
}

function initMap(config) {
  if (typeof L === "undefined") {
    const mapEl = byId("map");
    mapEl.classList.add("map-fallback");
    mapEl.textContent = "Map unavailable";
    byId("center-map").disabled = true;
    return;
  }

  const center = config?.dashboard?.defaultCenter || {};
  state.map = L.map("map", { zoomControl: true, maxZoom: 22 }).setView(
    [Number(center.latitude) || -2.5489, Number(center.longitude) || 118.0149],
    Number(center.zoom) || 5
  );
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 22,
    attribution: "Tiles &copy; Esri",
  }).addTo(state.map);
  addMbtilesOverlay(config);
  state.map.on("zoomend", () => updateMarkers(state.data || { devices: {}, peers: {} }));

  byId("center-map").addEventListener("click", () => {
    if (!state.map) return;
    if (state.tilesetBounds) {
      state.map.fitBounds(state.tilesetBounds, { padding: [24, 24], maxZoom: 21 });
      return;
    }
    const selected = selectedDevice();
    const latLng = selected ? getLatLng(selected.telemetry) : firstDeviceLatLng();
    if (latLng) {
      state.map.setView(latLng, Math.max(state.map.getZoom(), 16));
    }
  });
}

function selectedDevice() {
  const devices = state.data?.devices || {};
  if (state.selectedId && devices[state.selectedId]) {
    return devices[state.selectedId];
  }
  const first = sortedDevices(state.data)[0];
  return first || null;
}

function ensureSelectedDevice(snapshot) {
  if (state.selectedId && snapshot.devices[state.selectedId]) return;
  const first = sortedDevices(snapshot)[0];
  state.selectedId = first?.device_id || null;
}

function firstDeviceLatLng() {
  const devices = sortedDevices(state.data);
  for (const device of devices) {
    const latLng = getLatLng(device.telemetry || {});
    if (latLng) return latLng;
  }
  return null;
}

function updateHeader(snapshot) {
  const dashboard = snapshot.server.dashboard || {};
  const mqtt = snapshot.server.mqtt || {};
  const nowMs = snapshotNowMs(snapshot);
  byId("dashboard-title").textContent = dashboard.title || "IoT Device Dashboard";
  let mqttHost = String(mqtt.advertisedHost || mqtt.host || window.location.hostname);
  const browserHost = window.location.hostname;
  if (mqtt.advertisedHostSource === "auto" &&
      !["127.0.0.1", "localhost", "::1", "[::1]"].includes(browserHost)) {
    mqttHost = browserHost;
  }
  if (["0.0.0.0", "::", "[::]"].includes(mqttHost)) mqttHost = window.location.hostname;
  if (["127.0.0.1", "localhost", "::1", "[::1]"].includes(mqttHost) &&
      !["127.0.0.1", "localhost", "::1", "[::1]"].includes(window.location.hostname)) {
    mqttHost = window.location.hostname;
  }
  const displayHost = mqttHost.includes(":") && !mqttHost.startsWith("[") ? `[${mqttHost}]` : mqttHost;
  byId("mqtt-address").textContent = `MQTT mqtt://${displayHost}:${mqtt.port || 1883}`;
  byId("mqtt-address").title = "MQTT broker address";
  byId("device-count").textContent = countLabel(Object.keys(snapshot.devices).length);
  byId("peer-count").textContent = `${Object.keys(snapshot.peers).length} peers`;

  const selected = selectedDevice();
  const liveDot = byId("live-dot");
  const liveLabel = byId("live-label");
  liveDot.className = "status-dot";
  if (snapshot.server?.replay) {
    liveDot.classList.add("offline");
    liveLabel.textContent = "Replay";
    return;
  }
  if (!selected) {
    liveLabel.textContent = "Waiting";
    return;
  }
  const ageMs = nowMs - deviceTelemetrySeenMs(selected);
  if (Number.isFinite(ageMs) && ageMs <= ROVER_DISCONNECTED_MS) {
    liveDot.classList.add("live");
    liveLabel.textContent = "Live";
  } else {
    liveDot.classList.add("offline");
    liveLabel.textContent = "Disconnected";
  }
}

function roverSummaryFromDevice(device, role, snapshot) {
  const telemetry = device?.telemetry || {};
  const nowMs = snapshotNowMs(snapshot);
  const telemetrySeenMs = deviceTelemetrySeenMs(device);
  return {
    kind: "device",
    role,
    name: displayNameForDevice(device),
    id: device?.device_id || "",
    lastSeenMs: telemetrySeenMs,
    lastPositionSeenMs: device?.last_position_seen_ms || 0,
    telemetry,
    profile: device?.profile || null,
    deviceType: device?.device_type || "device",
    fix: telemetry.fix_mode_label || fixLabels[telemetry.fix_mode] || "UNKNOWN",
    accuracy: numeric(telemetry.local_accuracy_m, 3, " m"),
    source: device?.source_host || "",
    age: telemetrySeenMs ? ageLabel(telemetrySeenMs, nowMs) : "",
  };
}

function roverSummaryFromPeer(peer, role, snapshot, selectedTelemetry) {
  const accuracy = firstValue(peer, ["local_accuracy_m", "accuracy_m", "horizontal_accuracy_m"]);
  const nowMs = snapshotNowMs(snapshot);
  return {
    kind: "peer",
    role,
    name: displayNameForPeer(peer),
    id: peer?.device_id || "",
    lastSeenMs: peer?.last_seen_ms || 0,
    lastPositionSeenMs: peer?.last_position_seen_ms || 0,
    telemetry: peer || {},
    fix:
      peer?.fix_label ||
      peer?.fix_mode_label ||
      fixLabels[peer?.fix_mode] ||
      fixLabels[selectedTelemetry?.nearest_peer_fix_mode] ||
      "UNKNOWN",
    accuracy: numeric(accuracy ?? selectedTelemetry?.nearest_peer_accuracy_m, 3, " m"),
    source: peer?.source_host || "",
    age: peer?.last_seen_ms ? ageLabel(peer.last_seen_ms, nowMs) : "",
  };
}

function roverSummaryFromNearestTelemetry(peerId, telemetry, role) {
  return {
    kind: "nearest",
    role,
    name: String(peerId || "Waiting"),
    id: String(peerId || ""),
    lastSeenMs: 0,
    lastPositionSeenMs: 0,
    telemetry: {},
    fix: fixLabels[telemetry?.nearest_peer_fix_mode] || "UNKNOWN",
    accuracy: numeric(telemetry?.nearest_peer_accuracy_m, 3, " m"),
    source: "",
    age: "",
  };
}

function placeholderRoverSummary(role) {
  return {
    kind: "placeholder",
    role,
    name: "Waiting",
    id: "",
    lastSeenMs: 0,
    lastPositionSeenMs: 0,
    telemetry: {},
    fix: "UNKNOWN",
    accuracy: "-",
    source: "",
    age: "",
  };
}

function buildSafetyRovers(device, snapshot) {
  if (!device) return [];

  const telemetry = device.telemetry || {};
  const peerId = telemetry.nearest_peer_id;
  const rovers = [roverSummaryFromDevice(device, "Rover 1", snapshot)];

  const peerDevice = findDeviceByIdentifier(snapshot, peerId, device.device_id);
  if (peerDevice) {
    rovers.push(roverSummaryFromDevice(peerDevice, "Rover 2", snapshot));
    return rovers;
  }

  const peer = findPeerByIdentifier(snapshot, peerId);
  const fallbackDevice = fallbackDeviceForPair(snapshot, device, peerId);
  if (peer) {
    rovers.push(roverSummaryFromPeer(peer, "Rover 2", snapshot, telemetry));
  } else if (fallbackDevice) {
    rovers.push(roverSummaryFromDevice(fallbackDevice, "Rover 2", snapshot));
  } else if (peerId) {
    rovers.push(roverSummaryFromNearestTelemetry(peerId, telemetry, "Rover 2"));
  } else {
    rovers.push(placeholderRoverSummary("Rover 2"));
  }

  return rovers;
}

function hasPayloadData(device) {
  return Object.keys(device?.telemetry || {}).length > 0;
}

function nextAlphabeticalDevice(snapshot, device, excludeDevice = null) {
  const devices = sortedDevices(snapshot);
  if (!devices.length) return null;

  const selectedIndex = Math.max(
    0,
    devices.findIndex((candidate) => candidate.device_id === device?.device_id)
  );
  const ordered = devices.slice(selectedIndex + 1).concat(devices.slice(0, selectedIndex));

  return (
    ordered.find(
      (candidate) =>
        candidate.device_id !== device?.device_id &&
        candidate.device_id !== excludeDevice?.device_id &&
        hasPayloadData(candidate)
    ) || null
  );
}

function buildPayloadRovers(device, snapshot) {
  if (!device) return [];

  const telemetry = device.telemetry || {};
  const closestDevice = findDeviceByIdentifier(snapshot, telemetry.nearest_peer_id, device.device_id);
  const fallbackDevice = hasPayloadData(closestDevice) ? null : nextAlphabeticalDevice(snapshot, device, closestDevice);
  const rovers = [roverSummaryFromDevice(device, "", snapshot)];

  if (hasPayloadData(closestDevice)) {
    rovers.push(roverSummaryFromDevice(closestDevice, "", snapshot));
  } else if (fallbackDevice) {
    rovers.push(roverSummaryFromDevice(fallbackDevice, "", snapshot));
  } else {
    rovers.push({
      kind: "placeholder",
      role: "",
      name: telemetry.nearest_peer_id || "Waiting",
      telemetry: {},
    });
  }

  return rovers;
}

function ntripForRover(rover) {
  const telemetry = rover?.telemetry || {};
  return telemetry.ntrip_status_label || ntripLabels[telemetry.ntrip_status] || "-";
}

function batteryForRover(rover) {
  const telemetry = rover?.telemetry || {};
  if (telemetry.battery_percent !== null && telemetry.battery_percent !== undefined) {
    return numeric(telemetry.battery_percent, 1, "%");
  }
  return numeric(telemetry.battery_voltage_v, 2, " V");
}

function telemetryRowsForRover(rover) {
  const telemetry = rover?.telemetry || {};
  const profileMetrics = rover?.kind === "device" ? metricDefinitions({ profile: rover.profile }) : [];
  if (profileMetrics.length && !rover?.profile?.supports_peer_safety) {
    return profileMetrics.map((metric) => {
      const value = telemetry[metric.key];
      const formatted = typeof value === "number"
        ? numeric(value, Number(metric.digits ?? 1), metric.unit || "")
        : valueOrDash(value, metric.unit || "");
      return [metric.label || metric.key, formatted];
    });
  }
  return [
    ["Fix", rover?.fix || "-"],
    ["NTRIP", ntripForRover(rover)],
    ["Satellites", valueOrDash(telemetry.satellites)],
    ["HDOP", numeric(telemetry.hdop, 2)],
    ["Battery", batteryForRover(rover)],
    ["RTCM Age", numeric(telemetry.rtcm_age_sec, 1, " s")],
    ["Accuracy", rover?.accuracy || "-"],
    ["Last Seen", rover?.age ? `${rover.age} ago` : "-"],
  ];
}

function renderTelemetryCompare(rovers) {
  const panel = byId("telemetry-compare");
  if (!rovers.length) {
    panel.innerHTML = `<div class="empty">Waiting for device telemetry</div>`;
    return;
  }

  panel.innerHTML = rovers
    .map((rover) => {
      const rows = telemetryRowsForRover(rover)
        .map(
          ([label, value]) => `
            <div class="telemetry-row">
              <span>${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </div>
          `
        )
        .join("");
      return `
        <div class="telemetry-card">
          <div class="telemetry-card-head">
            <span class="telemetry-role">${escapeHtml(rover.role)}</span>
            <strong>${escapeHtml(rover.name)}</strong>
          </div>
          <div class="telemetry-rows">${rows}</div>
        </div>
      `;
    })
    .join("");
}

function renderSafetyRovers(rovers) {
  const grid = byId("safety-rover-grid");
  if (!rovers.length) {
    grid.innerHTML = "";
    return;
  }

  grid.innerHTML = rovers
    .map((rover) => {
      const subline = [rover.source, rover.age ? `${rover.age} ago` : ""].filter(Boolean).join(" - ");
      return `
        <div class="safety-rover-card">
          <span class="safety-rover-role">${escapeHtml(rover.role)}</span>
          <strong>${escapeHtml(rover.name)}</strong>
          <div class="safety-rover-meta">
            <span>${escapeHtml(rover.fix)}</span>
            <span>${escapeHtml(rover.accuracy)}</span>
          </div>
          <div class="safety-rover-sub">${escapeHtml(subline || rover.id || "-")}</div>
        </div>
      `;
    })
    .join("");
}

function updateSafety(device, snapshot, safetyRovers = buildSafetyRovers(device, snapshot)) {
  const telemetry = device?.telemetry || {};
  const safetyDisconnected = safetyRovers.slice(0, 2).some((rover) => roverIsDisconnectedForSafety(rover, snapshot));
  const safetyWaitingForPosition =
    !safetyDisconnected && safetyRovers.slice(0, 2).some((rover) => roverIsWaitingForPosition(rover, snapshot));
  const safetyUnavailable = safetyDisconnected || safetyWaitingForPosition;
  const safeDistance = safetyUnavailable ? null : telemetry.nearest_peer_safe_distance_m;
  const stateClass = safetyUnavailable ? "unknown" : safeDistanceClass(safeDistance);
  const roverOne = safetyRovers[0]?.name || "-";
  const roverTwo = safetyRovers[1]?.name || telemetry.nearest_peer_id || "-";

  byId("safety-panel").className = `safety-panel safe-${stateClass}`;
  byId("safety-state").textContent = safetyDisconnected ? "DISCONNECTED" : safeDistanceLabel(safeDistance);
  byId("safety-value").textContent = safeDistanceValue(safeDistance);
  byId("safety-pair").textContent = device ? `${roverOne} to ${roverTwo}` : "No crane pair";
  renderSafetyRovers(safetyRovers);
  byId("safety-raw").textContent = safetyUnavailable ? "-" : numeric(telemetry.nearest_peer_distance_m, 2, " m");
  byId("safety-uncertainty").textContent = safetyUnavailable
    ? "-"
    : numeric(telemetry.nearest_peer_uncertainty_m, 3, " m");
  byId("safety-local-accuracy").textContent = safetyUnavailable ? "-" : numeric(telemetry.local_accuracy_m, 3, " m");
  byId("safety-peer-accuracy").textContent = safetyUnavailable
    ? "-"
    : numeric(telemetry.nearest_peer_accuracy_m, 3, " m");
}

function selectHeaderRover(deviceId) {
  const snapshot = state.data;
  const device = snapshot?.devices?.[deviceId];
  if (!device) return;

  state.selectedId = deviceId;
  render(snapshot);
  if (state.currentView === "statistics") {
    restartStatisticsStream();
  }

  const latLng = getLatLng(device.telemetry || {});
  if (latLng && state.map) state.map.setView(latLng, Math.max(state.map.getZoom(), 16));
}

function ensureHeaderRoverButton(list, deviceId) {
  let row = Array.from(list.querySelectorAll(".rover-tab")).find((button) => button.dataset.deviceId === deviceId);
  if (row) return row;

  row = document.createElement("button");
  row.type = "button";
  row.className = "rover-tab";
  row.dataset.deviceId = deviceId;
  row.addEventListener("click", () => selectHeaderRover(row.dataset.deviceId));

  const name = document.createElement("span");
  name.className = "rover-tab-name";
  const status = document.createElement("span");
  status.className = "rover-tab-status";
  row.append(name, status);

  return row;
}

function renderHeaderRovers(snapshot) {
  const list = byId("header-rover-list");
  const devices = sortedDevices(snapshot);
  if (!devices.length) {
    list.innerHTML = `<div class="header-empty">Waiting for devices</div>`;
    return;
  }

  if (!state.selectedId || !snapshot.devices[state.selectedId]) {
    state.selectedId = devices[0].device_id;
  }

  list.querySelector(".header-empty")?.remove();
  const activeIds = new Set(devices.map((device) => String(device.device_id)));
  Array.from(list.querySelectorAll(".rover-tab")).forEach((row) => {
    if (!activeIds.has(row.dataset.deviceId)) row.remove();
  });

  devices.forEach((device, index) => {
    const deviceId = String(device.device_id);
    const telemetry = device.telemetry || {};
    const displayName = displayNameForDevice(device);
    const statusClass = deviceIsDisconnected(device, snapshot)
      ? ""
      : supportsPeerSafety(device) ? statusClassForFix(telemetry.fix_mode) : "good";

    const row = ensureHeaderRoverButton(list, deviceId);
    const isActive = deviceId === state.selectedId;
    row.className = `rover-tab${isActive ? " active" : ""}`;
    row.title = displayName;
    row.setAttribute("aria-pressed", String(isActive));
    row.querySelector(".rover-tab-name").textContent = displayName;
    row.querySelector(".rover-tab-status").className = `rover-tab-status ${statusClass}`.trim();

    if (list.children[index] !== row) {
      list.insertBefore(row, list.children[index] || null);
    }
  });
}

function updateMarkers(snapshot) {
  if (!state.map) return;

  const seenDevices = new Set();
  Object.values(snapshot.devices).forEach((device) => {
    const telemetry = device.telemetry || {};
    const latLng = getLatLng(telemetry);
    if (!latLng) return;
    seenDevices.add(device.device_id);
    const title = `${displayNameForDevice(device)} - ${supportsPeerSafety(device)
      ? telemetry.fix_mode_label || fixLabels[telemetry.fix_mode] || "UNKNOWN"
      : deviceTypeLabel(device)}`;
    const disconnected = deviceIsDisconnected(device, snapshot);
    const fillColor = disconnected ? "#657080" : "#0f7490";
    const marker = updateRoverMarker(state.deviceMarkers.get(device.device_id), latLng, {
      variant: supportsPeerSafety(device) ? "device" : "generic-device",
      status: disconnected ? "offline" : "live",
      zIndexOffset: 200,
      dotStyle: {
        radius: 9,
        color: "#ffffff",
        weight: 3,
        fillColor,
        fillOpacity: 0.95,
      },
    });
    state.deviceMarkers.set(device.device_id, marker);
    marker.bindPopup(escapeHtml(title));
  });

  for (const [id, marker] of state.deviceMarkers) {
    if (!seenDevices.has(id)) {
      marker.remove();
      state.deviceMarkers.delete(id);
    }
  }

  const seenPeers = new Set();
  Object.values(snapshot.peers).forEach((peer) => {
    const latLng = getLatLng(peer);
    if (!latLng) return;
    seenPeers.add(peer.device_id);
    const marker = updateRoverMarker(state.peerMarkers.get(peer.device_id), latLng, {
      variant: "peer",
      status: peer.stale ? "stale" : "live",
      dotStyle: {
        radius: 7,
        color: "#191b1f",
        weight: 2,
        fillColor: peer.stale ? "#b7791f" : "#0f8b5f",
        fillOpacity: 0.86,
      },
    });
    state.peerMarkers.set(peer.device_id, marker);
    marker.bindPopup(escapeHtml(`${displayNameForPeer(peer)} - ${peer.fix_label || "UNKNOWN"}`));
  });

  for (const [id, marker] of state.peerMarkers) {
    if (!seenPeers.has(id)) {
      marker.remove();
      state.peerMarkers.delete(id);
    }
  }
}

function updateSelectedLabel(device) {
  const telemetry = device?.telemetry || {};
  const latLng = getLatLng(telemetry);
  if (!device) {
    byId("selected-label").textContent = "No device selected";
  } else if (latLng) {
    byId("selected-label").textContent = `${displayNameForDevice(device)} - ${latLng[0].toFixed(7)}, ${latLng[1].toFixed(7)}`;
  } else {
    byId("selected-label").textContent = `${displayNameForDevice(device)} - waiting for coordinates`;
  }
}

function renderRawPayloads(rovers) {
  const raw = byId("raw-fields");
  if (!rovers.length) {
    raw.innerHTML = `<div class="empty">No device payloads yet</div>`;
    return;
  }

  const comparedRovers = rovers.slice(0, 2);
  const payloads = comparedRovers.map((rover) => (rover?.kind === "device" ? rover.telemetry || {} : {}));
  const keys = Array.from(new Set(payloads.flatMap((telemetry) => Object.keys(telemetry)))).sort();

  if (!keys.length) {
    raw.innerHTML = `<div class="empty">No payload fields yet</div>`;
    return;
  }

  const header = `
    <div class="raw-name-row">
      ${comparedRovers.map((rover) => `<strong>${escapeHtml(rover?.name || "Waiting")}</strong>`).join("")}
    </div>
  `;
  const rows = keys
    .map(
      (key) => `
        <div class="field-row">
          <div class="field-name">${escapeHtml(key)}</div>
          <div class="field-values">
            ${payloads.map((telemetry) => `<div class="field-value">${escapeHtml(formatRawValue(telemetry[key]))}</div>`).join("")}
          </div>
        </div>
      `
    )
    .join("");

  raw.innerHTML = `${header}${rows}`;
}

function formatRawValue(value) {
  if (Array.isArray(value)) {
    return `[${value.join(", ")}]`;
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  return valueOrDash(value);
}

function setActiveView(view) {
  state.currentView = view;
  document.querySelectorAll(".view-tab").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  byId("live-view").hidden = view === "statistics";
  byId("statistics-view").hidden = view !== "statistics";
  byId("replay-panel").hidden = view !== "replay";
  if (view === "statistics") {
    stopReplay();
    restartStatisticsStream();
  } else if (view === "replay") {
    closeStatisticsStream();
    initReplay().catch((error) => console.error("Replay init failed", error));
    if (state.map) window.requestAnimationFrame(() => state.map.invalidateSize());
  } else if (state.map) {
    closeStatisticsStream();
    stopReplay();
    fetchLatestState(true).catch((error) => console.error("State refresh failed", error));
    window.requestAnimationFrame(() => state.map.invalidateSize());
  } else {
    closeStatisticsStream();
  }
}

function selectedStatisticsDeviceId() {
  return String(selectedDevice()?.device_id || "").trim().slice(0, 200);
}

function updateStatisticsRangeTabs() {
  document.querySelectorAll(".statistics-range-tab").forEach((button) => {
    const active = button.dataset.range === state.statisticsRange;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${url} returned ${response.status}`);
  }
  return response.json();
}

function updateReplayControls() {
  const range = state.replay.range;
  const hasRange = Boolean(range?.enabled && range.from_ms && range.to_ms);
  const atMs = state.replay.atMs || Number(range?.to_ms) || 0;
  const scrubber = byId("replay-scrubber");
  const timestamp = byId("replay-timestamp");
  byId("replay-play").textContent = state.replay.playing ? "Pause" : "Play";
  byId("replay-speed").value = String(state.replay.speed);
  scrubber.disabled = !hasRange;
  timestamp.disabled = !hasRange;
  byId("replay-play").disabled = !hasRange;
  byId("replay-speed").disabled = !hasRange;

  if (!hasRange) {
    byId("replay-current-label").textContent = "No replay samples";
    byId("replay-start-label").textContent = "-";
    byId("replay-end-label").textContent = "-";
    return;
  }

  const fromMs = Number(range.from_ms);
  const toMs = Number(range.to_ms);
  scrubber.min = "0";
  scrubber.max = String(Math.max(0, Math.round((toMs - fromMs) / 1000)));
  scrubber.step = "1";
  scrubber.value = String(Math.max(0, Math.round((clamp(atMs, fromMs, toMs) - fromMs) / 1000)));
  timestamp.min = inputDateTimeValue(range.from_ms);
  timestamp.max = inputDateTimeValue(range.to_ms);
  timestamp.value = inputDateTimeValue(atMs);
  byId("replay-current-label").textContent = timeLabel(atMs);
  byId("replay-start-label").textContent = dateTimeLabel(range.from_ms);
  byId("replay-end-label").textContent = dateTimeLabel(range.to_ms);
}

async function initReplay() {
  if (!state.replay.range) {
    state.replay.range = await fetchJson("/api/replay/range");
    state.replay.atMs = Number(state.replay.range?.to_ms) || 0;
  }
  updateReplayControls();
  if (state.replay.atMs) {
    await loadReplayAt(state.replay.atMs);
  }
}

async function loadReplayAt(atMs) {
  const range = state.replay.range;
  if (!range?.enabled || !range.from_ms || !range.to_ms) return;
  const nextAtMs = clamp(Number(atMs) || Number(range.to_ms), Number(range.from_ms), Number(range.to_ms));
  const requestedAtMs = nextAtMs;
  state.replay.atMs = nextAtMs;
  updateReplayControls();

  if (state.replay.fetchInFlight) {
    state.replay.pendingAtMs = nextAtMs;
    return;
  }

  state.replay.fetchInFlight = true;
  try {
    const params = new URLSearchParams({ at: String(nextAtMs), lookback: String(5 * 60 * 1000) });
    const snapshot = await fetchJson(`/api/replay/state?${params.toString()}`);
    if (state.currentView === "replay") {
      render(snapshot);
    }
  } finally {
    state.replay.fetchInFlight = false;
    const pending = state.replay.pendingAtMs;
    state.replay.pendingAtMs = null;
    if (pending !== null && pending !== requestedAtMs) {
      loadReplayAt(pending).catch((error) => console.error("Replay refresh failed", error));
    }
  }
}

function stopReplay() {
  state.replay.playing = false;
  if (state.replay.timer) {
    clearInterval(state.replay.timer);
    state.replay.timer = null;
  }
  state.replay.lastTickMs = 0;
  updateReplayControls();
}

function startReplay() {
  const range = state.replay.range;
  if (!range?.enabled) return;
  state.replay.playing = true;
  state.replay.lastTickMs = Date.now();
  state.replay.timer = setInterval(() => {
    const now = Date.now();
    const elapsed = now - state.replay.lastTickMs;
    state.replay.lastTickMs = now;
    const nextAtMs = state.replay.atMs + elapsed * state.replay.speed;
    if (nextAtMs >= Number(range.to_ms)) {
      loadReplayAt(range.to_ms).catch((error) => console.error("Replay refresh failed", error));
      stopReplay();
      return;
    }
    loadReplayAt(nextAtMs).catch((error) => console.error("Replay refresh failed", error));
  }, 500);
  updateReplayControls();
}

function toggleReplayPlayback() {
  if (state.replay.playing) {
    stopReplay();
  } else {
    startReplay();
  }
}

function initReplayPanelInteractions() {
  const panel = byId("replay-panel");
  if (typeof L !== "undefined" && L.DomEvent) {
    L.DomEvent.disableClickPropagation(panel);
    L.DomEvent.disableScrollPropagation(panel);
  }
  ["pointerdown", "pointermove", "pointerup", "mousedown", "mousemove", "mouseup", "touchstart", "touchmove", "touchend"].forEach(
    (eventName) => {
      panel.addEventListener(eventName, (event) => event.stopPropagation());
    }
  );
}

function statisticsSubscription() {
  return {
    range: state.statisticsRange,
    deviceId: selectedStatisticsDeviceId(),
  };
}

function closeStatisticsStream() {
  if (state.statisticsStream) {
    state.statisticsStream.close();
  }
  state.statisticsStream = null;
  state.statisticsStreamKey = "";
  state.statisticsStreamConnected = false;
  state.statisticsStreamVersion = -1;
}

function openStatisticsStream() {
  if (state.currentView !== "statistics") return;
  const subscription = statisticsSubscription();
  const streamKey = JSON.stringify([subscription.range, subscription.deviceId]);
  if (state.statisticsStream && state.statisticsStreamKey === streamKey) return;

  closeStatisticsStream();
  const params = new URLSearchParams({ range: subscription.range });
  if (subscription.deviceId) params.set("device_id", subscription.deviceId);
  const stream = new EventSource(`/api/statistics/stream?${params.toString()}`);
  state.statisticsStream = stream;
  state.statisticsStreamKey = streamKey;

  stream.onopen = () => {
    if (state.statisticsStream === stream) state.statisticsStreamConnected = true;
  };
  stream.addEventListener("snapshot", (event) => {
    if (state.statisticsStream !== stream || state.currentView !== "statistics") return;
    try {
      const payload = JSON.parse(event.data);
      const payloadSubscription = payload?.subscription || {};
      if (
        payloadSubscription.range !== subscription.range ||
        String(payloadSubscription.device_id || "") !== subscription.deviceId
      ) {
        return;
      }
      const version = Number(payload.version);
      if (!Number.isFinite(version) || version <= state.statisticsStreamVersion) return;
      state.statisticsStreamConnected = true;
      state.statisticsStreamVersion = version;
      state.statistics = payload.statistics;
      renderStatistics();
    } catch (error) {
      console.error("Invalid statistics snapshot", error);
    }
  });
  stream.onerror = () => {
    if (state.statisticsStream === stream) state.statisticsStreamConnected = false;
  };
}

function restartStatisticsStream() {
  closeStatisticsStream();
  openStatisticsStream();
}

async function refreshStatistics() {
  if (state.statisticsFetchInFlight) return;
  state.statisticsFetchInFlight = true;
  const button = byId("refresh-statistics");
  button.disabled = true;
  const subscription = statisticsSubscription();
  const params = new URLSearchParams({ range: subscription.range });
  if (subscription.deviceId) params.set("device_id", subscription.deviceId);
  try {
    if (!state.statisticsStream) openStatisticsStream();
    const response = await fetch(`/api/statistics/refresh?${params.toString()}`, {
      method: "POST",
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`/api/statistics/refresh returned ${response.status}`);
    }
    const result = await response.json();
    if (result.status === "no_active_subscription") restartStatisticsStream();
  } finally {
    state.statisticsFetchInFlight = false;
    button.disabled = false;
  }
}

function metricCard(label, value, note = "") {
  return `
    <div class="statistics-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(note)}</small>
    </div>
  `;
}

function renderStatisticsMetrics(summary) {
  const sampleCount = Number(summary?.sample_count) || 0;
  const fixTotal =
    (Number(summary?.fix_fixed_count) || 0) +
    (Number(summary?.fix_float_count) || 0) +
    (Number(summary?.fix_no_count) || 0);
  const closest = summary?.closest;
  const closestValue = closest?.closest_safe_distance_m ?? closest?.closest_raw_distance_m;

  byId("statistics-metric-grid").innerHTML = [
    metricCard("Samples", sampleCount.toLocaleString(), `Last ${dateTimeLabel(summary?.last_sample_ms)}`),
    metricCard("Closest Distance", numeric(closestValue, 2, " m"), closest ? dateTimeLabel(closest.closest_at_ms) : "-"),
    metricCard("RTK Fixed", percent(summary?.fix_fixed_count, fixTotal), `${summary?.fix_fixed_count || 0} fixed samples`),
    metricCard("Device Connected", numeric(summary?.connection_percent, 0, "%"), "Average over selected range"),
  ].join("");
}

function groupHourlyDeviceRows(rows) {
  const grouped = new Map();
  rows.forEach((row) => {
    const hour = Number(row.hour_ms);
    if (!Number.isFinite(hour)) return;
    const item = grouped.get(hour) || {
      hour_ms: hour,
      sample_count: 0,
      fix_fixed_count: 0,
      fix_float_count: 0,
      fix_no_count: 0,
      ntrip_connected_count: 0,
      ntrip_disconnected_count: 0,
      connection_percent_total: 0,
      connection_percent_count: 0,
    };
    item.sample_count += Number(row.sample_count) || 0;
    item.fix_fixed_count += Number(row.fix_fixed_count) || 0;
    item.fix_float_count += Number(row.fix_float_count) || 0;
    item.fix_no_count += Number(row.fix_no_count) || 0;
    item.ntrip_connected_count += Number(row.ntrip_connected_count) || 0;
    item.ntrip_disconnected_count += Number(row.ntrip_disconnected_count) || 0;
    const connectionPercent = Number(row.connection_percent);
    if (Number.isFinite(connectionPercent)) {
      item.connection_percent_total += connectionPercent;
      item.connection_percent_count += 1;
      item.connection_percent = item.connection_percent_total / item.connection_percent_count;
    }
    grouped.set(hour, item);
  });
  return Array.from(grouped.values()).sort((a, b) => a.hour_ms - b.hour_ms);
}

function buildLinePoints(rows, valueGetter) {
  return rows
    .map((row) => {
      const value = valueGetter(row);
      const time = Number(row.at_ms ?? row.hour_ms ?? row.closest_at_ms);
      if (!Number.isFinite(time) || !Number.isFinite(value)) return null;
      return { row, time, value };
    })
    .filter(Boolean)
    .sort((a, b) => a.time - b.time);
}

function linePath(points, width, height, padding, minValue, maxValue, mode = "linear") {
  const xMin = points[0].time;
  const xMax = points[points.length - 1].time;
  const xSpan = Math.max(1, xMax - xMin);
  const ySpan = Math.max(1, maxValue - minValue);
  points.forEach((point) => {
    point.x = padding.left + ((point.time - xMin) / xSpan) * (width - padding.left - padding.right);
    point.y = padding.top + (1 - (point.value - minValue) / ySpan) * (height - padding.top - padding.bottom);
  });
  if (mode === "step") {
    return points
      .map((point, index) => {
        if (index === 0) return `M ${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
        return `H ${point.x.toFixed(2)} V ${point.y.toFixed(2)}`;
      })
      .join(" ");
  }
  return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
}

function compactTimeLabel(ms) {
  const date = new Date(ms);
  if (state.statisticsRange === "live") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" });
}

function chartAxisLabels(points, width, height, padding, minValue, maxValue, suffix, digits, yAxisLabels = null) {
  const valueMid = (minValue + maxValue) / 2;
  const yItems =
    Array.isArray(yAxisLabels) && yAxisLabels.length
      ? yAxisLabels.map((item) => {
          const value = Number(item.value);
          const y =
            padding.top +
            (1 - (value - minValue) / Math.max(1, maxValue - minValue)) * (height - padding.top - padding.bottom);
          return [item.label, y];
        })
      : [
          [Number(maxValue).toFixed(digits) + suffix, padding.top],
          [Number(valueMid).toFixed(digits) + suffix, padding.top + (height - padding.top - padding.bottom) / 2],
          [Number(minValue).toFixed(digits) + suffix, height - padding.bottom],
        ];
  const yLabels = yItems
    .map(
      ([label, y]) => `
        <text class="chart-axis-label y-axis-label" x="${padding.left - 12}" y="${Number(y).toFixed(1)}">${escapeHtml(label)}</text>
      `
    )
    .join("");

  const first = points[0];
  const last = points[points.length - 1];
  const xLabels = `
    <text class="chart-axis-label x-axis-label" x="${padding.left}" y="${height - 8}">${escapeHtml(compactTimeLabel(first.time))}</text>
    <text class="chart-axis-label x-axis-label end" x="${width - padding.right}" y="${height - 8}">${escapeHtml(compactTimeLabel(last.time))}</text>
  `;
  return `${yLabels}${xLabels}`;
}

function renderLineChart(
  el,
  rows,
  valueGetter,
  {
    suffix = "",
    color = "#0f7490",
    digits = 1,
    minScale = null,
    maxScale = null,
    mode = "linear",
    yAxisLabels = null,
    valueFormatter = null,
    leftPadding = 64,
  } = {}
) {
  const points = buildLinePoints(rows, valueGetter);
  if (!points.length) {
    el.innerHTML = `<div class="chart-empty">No matching values yet</div>`;
    return;
  }
  const width = Math.max(360, Math.round(el.getBoundingClientRect().width || el.clientWidth || 520));
  const height = 236;
  const padding = { top: 18, right: 22, bottom: 42, left: leftPadding };
  const values = points.map((point) => point.value);
  let minValue = Math.min(...values);
  let maxValue = Math.max(...values);
  if (Number.isFinite(minScale)) {
    minValue = Number(minScale);
  }
  if (Number.isFinite(maxScale)) {
    maxValue = Number(maxScale);
  }
  if (minValue === maxValue) {
    minValue = Math.max(0, minValue - 1);
    maxValue += 1;
  } else if (!Number.isFinite(minScale) || !Number.isFinite(maxScale)) {
    const pad = (maxValue - minValue) * 0.12;
    if (!Number.isFinite(minScale)) {
      minValue = Math.max(0, minValue - pad);
    }
    if (!Number.isFinite(maxScale)) {
      maxValue += pad;
    }
  }
  const path = linePath(points, width, height, padding, minValue, maxValue, mode);
  const fillPath = `${path} L ${points[points.length - 1].x.toFixed(2)} ${height - padding.bottom} L ${points[0].x.toFixed(2)} ${height - padding.bottom} Z`;
  const axisLabels = chartAxisLabels(points, width, height, padding, minValue, maxValue, suffix, digits, yAxisLabels);

  el.innerHTML = `
    <svg class="line-chart-svg" viewBox="0 0 ${width} ${height}" role="img" style="--chart-color: ${color}">
      <line class="chart-grid-line" x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}"></line>
      <line class="chart-grid-line" x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"></line>
      ${axisLabels}
      <path class="chart-area" d="${fillPath}"></path>
      <path class="chart-line" d="${path}"></path>
      <line class="chart-hover-line" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" hidden></line>
      <rect class="chart-hit-area" x="${padding.left}" y="${padding.top}" width="${width - padding.left - padding.right}" height="${height - padding.top - padding.bottom}"></rect>
    </svg>
    <div class="chart-tooltip" hidden></div>
  `;

  const svg = el.querySelector(".line-chart-svg");
  const tooltip = el.querySelector(".chart-tooltip");
  const hoverLine = el.querySelector(".chart-hover-line");
  const nearestPoint = (x) =>
    points.reduce((best, point) => (Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best), points[0]);
  const hideTooltip = () => {
    tooltip.hidden = true;
    hoverLine.hidden = true;
  };
  const showTooltip = (point) => {
    tooltip.hidden = false;
    hoverLine.hidden = false;
    const displayValue =
      typeof valueFormatter === "function" ? valueFormatter(point.value, point.row) : Number(point.value).toFixed(digits) + suffix;
    tooltip.innerHTML = `
      <strong>${escapeHtml(displayValue)}</strong>
      <span>${escapeHtml(timeLabel(point.time))}</span>
    `;
    tooltip.style.left = `${(point.x / width) * 100}%`;
    tooltip.style.top = `${(point.y / height) * 100}%`;
    hoverLine.setAttribute("x1", point.x.toFixed(2));
    hoverLine.setAttribute("x2", point.x.toFixed(2));
  };
  svg.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * width;
    showTooltip(nearestPoint(Math.max(padding.left, Math.min(width - padding.right, x))));
  });
  svg.addEventListener("pointerleave", hideTooltip);
  svg.addEventListener("pointercancel", hideTooltip);
  el.addEventListener("mouseleave", hideTooltip);
}

function renderStatisticsCharts(hourly) {
  const selected = selectedDevice();
  if (selected && !supportsPeerSafety(selected)) {
    renderProfileStatisticsCharts(selected, hourly);
    return;
  }
  if (state.statisticsRange === "live") {
    renderLiveSampleCharts(state.statistics?.samples);
    return;
  }

  byId("distance-chart-title").textContent = "Closest Distance By Hour";
  byId("rtk-chart-title").textContent = "Avg. Hourly RTK Fixed Rate";
  byId("ntrip-chart-title").textContent = "Avg. Hourly Device Connection Rate";

  const deviceRows = groupHourlyDeviceRows(hourly?.device_metrics || []);
  const pairRows = (hourly?.pair_metrics || []).filter((row) => row.closest_safe_distance_m !== null || row.closest_raw_distance_m !== null);
  renderLineChart(
    byId("distance-chart"),
    pairRows,
    (row) => Number(row.closest_safe_distance_m ?? row.closest_raw_distance_m),
    { suffix: " m", color: "#c2410c", digits: 2 }
  );
  renderLineChart(
    byId("rtk-chart"),
    deviceRows,
    (row) => {
      const total = Number(row.sample_count) || 0;
      return total > 0 ? ((Number(row.fix_fixed_count) || 0) / total) * 100 : NaN;
    },
    { suffix: "%", color: "#0f8b5f", digits: 0, minScale: 0, maxScale: 100 }
  );
  renderLineChart(
    byId("ntrip-chart"),
    deviceRows,
    (row) => Number(row.connection_percent),
    { suffix: "%", color: "#0f7490", digits: 0, minScale: 0, maxScale: 100 }
  );
}

function renderProfileStatisticsCharts(device, hourly) {
  const definitions = metricDefinitions(device).slice(0, 3);
  const slots = [
    ["distance-chart-title", "distance-chart", "#c2410c"],
    ["rtk-chart-title", "rtk-chart", "#0f8b5f"],
    ["ntrip-chart-title", "ntrip-chart", "#0f7490"],
  ];
  const isLive = state.statisticsRange === "live";
  const liveRows = (state.statistics?.samples?.samples || []).map((row) => {
    try {
      return { ...row, ...JSON.parse(row.payload_json || "{}") };
    } catch (_error) {
      return row;
    }
  });
  const hourlyRows = hourly?.numeric_metrics || [];

  slots.forEach(([titleId, chartId, color], index) => {
    const definition = definitions[index];
    if (!definition) {
      byId(titleId).textContent = "Additional Profile Metric";
      byId(chartId).innerHTML = `<div class="chart-empty">No additional metric configured</div>`;
      return;
    }
    byId(titleId).textContent = `${definition.label} ${isLive ? "Live" : "By Hour"}`;
    const rows = isLive
      ? liveRows
      : hourlyRows.filter((row) => row.metric_key === definition.key);
    renderLineChart(
      byId(chartId),
      rows,
      (row) => Number(isLive ? row[definition.key] : row.avg_value),
      {
        suffix: definition.unit || "",
        color,
        digits: Number(definition.digits ?? 1),
      }
    );
  });
}

function renderLiveSampleCharts(samplesPayload) {
  const samples = samplesPayload?.samples || [];
  const statusRows = buildDeviceStatusRows(samples, state.statistics?.events?.events || []);
  byId("distance-chart-title").textContent = "Live Distance";
  byId("rtk-chart-title").textContent = "Live RTK Fixed";
  byId("ntrip-chart-title").textContent = "Live Device Status";
  renderLineChart(
    byId("distance-chart"),
    samples,
    (row) => Number(row.safe_distance_m ?? row.raw_distance_m),
    { suffix: " m", color: "#c2410c", digits: 2 }
  );
  renderLineChart(
    byId("rtk-chart"),
    samples,
    (row) => (Number(row.fix_mode) === 4 ? 100 : 0),
    {
      color: "#0f8b5f",
      minScale: 0,
      maxScale: 100,
      mode: "step",
      leftPadding: 96,
      yAxisLabels: [
        { value: 100, label: "Fixed" },
        { value: 0, label: "Not Fixed" },
      ],
      valueFormatter: (value) => (Number(value) >= 50 ? "Fixed" : "Not Fixed"),
    }
  );
  renderLineChart(
    byId("ntrip-chart"),
    statusRows,
    (row) => Number(row.connected_percent),
    {
      color: "#0f7490",
      minScale: 0,
      maxScale: 100,
      mode: "step",
      leftPadding: 108,
      yAxisLabels: [
        { value: 100, label: "Connected" },
        { value: 0, label: "Disconnected" },
      ],
      valueFormatter: (value) => (Number(value) >= 50 ? "Connected" : "Disconnected"),
    }
  );
}

function buildDeviceStatusRows(samples, events) {
  const selected = selectedDevice();
  const deviceId = selected?.device_id || selectedStatisticsDeviceId();
  const rows = [];

  samples
    .filter((sample) => !deviceId || sample.device_id === deviceId)
    .forEach((sample) => {
      const atMs = Number(sample.at_ms);
      if (Number.isFinite(atMs)) {
        rows.push({ at_ms: atMs, connected_percent: 100 });
      }
    });

  events
    .filter((event) => !deviceId || event.device_id === deviceId)
    .forEach((event) => {
      const atMs = Number(event.at_ms);
      if (!Number.isFinite(atMs)) return;
      if (event.event_type === "device_disconnected") {
        rows.push({ at_ms: atMs, connected_percent: 0 });
      } else if (event.event_type === "device_reconnected") {
        rows.push({ at_ms: atMs, connected_percent: 100 });
      }
    });

  const latestSampleMs = Math.max(0, ...rows.filter((row) => row.connected_percent === 100).map((row) => row.at_ms));
  if (latestSampleMs > 0 && Date.now() - latestSampleMs > ROVER_DISCONNECTED_MS) {
    rows.push({ at_ms: latestSampleMs + ROVER_DISCONNECTED_MS, connected_percent: 0 });
  }

  if (selected) {
    rows.push({
      at_ms: Date.now(),
      connected_percent: deviceIsDisconnected(selected, state.data) ? 0 : 100,
    });
  }

  return rows.sort((a, b) => a.at_ms - b.at_ms);
}

function renderStatisticsEvents(eventsPayload) {
  const events = eventsPayload?.events || [];
  const panel = byId("statistics-events");
  if (!events.length) {
    panel.innerHTML = `<div class="statistics-event-empty">No events for this range</div>`;
    return;
  }
  panel.innerHTML = events
    .map(
      (event) => `
        <div class="statistics-event">
          <div class="statistics-event-time">${escapeHtml(dateTimeLabel(event.at_ms))}</div>
          <div class="statistics-event-message">
            <strong>${escapeHtml(event.message || event.event_type)}</strong>
            <div class="statistics-event-device">${escapeHtml(event.device_id || "Dashboard")}</div>
          </div>
          <div class="statistics-event-type">${escapeHtml(event.event_type || "event")}</div>
        </div>
      `
    )
    .join("");
}

function renderStatistics() {
  const statistics = state.statistics;
  const selected = selectedDevice();
  byId("statistics-heading").textContent = selected ? `${displayNameForDevice(selected)} Operational History` : "Operational History";
  if (!statistics?.summary?.enabled) {
    byId("statistics-metric-grid").innerHTML = metricCard("Logging", "Disabled", "Enable logging in config.yaml");
    byId("distance-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("rtk-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("ntrip-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("statistics-events").innerHTML = `<div class="statistics-event-empty">Logging is disabled</div>`;
    return;
  }
  renderStatisticsMetrics(statistics.summary);
  renderStatisticsCharts(statistics.hourly);
  renderStatisticsEvents(statistics.events);
}

function render(snapshot) {
  const previousSelectedId = state.selectedId;
  state.lastVersion = Math.max(state.lastVersion, snapshotVersion(snapshot));
  state.data = snapshot;
  if (!snapshot.server?.replay) {
    state.liveSnapshot = snapshot;
  }
  state.snapshotServerNowMs = Number(snapshot?.server?.now_ms) || Date.now();
  state.snapshotLocalReceivedMs = Date.now();
  ensureSelectedDevice(snapshot);
  if (state.currentView === "statistics" && state.selectedId !== previousSelectedId) {
    restartStatisticsStream();
  }
  const selected = selectedDevice();
  const hasPeerSafety = supportsPeerSafety(selected);
  const safetyRovers = hasPeerSafety
    ? buildSafetyRovers(selected, snapshot)
    : selected ? [roverSummaryFromDevice(selected, deviceTypeLabel(selected), snapshot)] : [];
  const payloadRovers = hasPeerSafety
    ? buildPayloadRovers(selected, snapshot)
    : selected ? [roverSummaryFromDevice(selected, "", snapshot)] : [];
  updateHeader(snapshot);
  byId("safety-panel").hidden = !hasPeerSafety;
  updateSafety(hasPeerSafety ? selected : null, snapshot, hasPeerSafety ? safetyRovers : []);
  renderTelemetryCompare(safetyRovers);
  renderHeaderRovers(snapshot);
  updateMarkers(snapshot);
  updateSelectedLabel(selected);
  renderRawPayloads(payloadRovers);
}

function refreshAgeSensitiveUi() {
  const snapshot = state.data;
  if (!snapshot) return;

  const selected = selectedDevice();
  const hasPeerSafety = supportsPeerSafety(selected);
  const safetyRovers = hasPeerSafety
    ? buildSafetyRovers(selected, snapshot)
    : selected ? [roverSummaryFromDevice(selected, deviceTypeLabel(selected), snapshot)] : [];
  if (state.eventStreamConnected) {
    updateHeader(snapshot);
  }
  byId("safety-panel").hidden = !hasPeerSafety;
  updateSafety(hasPeerSafety ? selected : null, snapshot, hasPeerSafety ? safetyRovers : []);
  renderTelemetryCompare(safetyRovers);
  renderHeaderRovers(snapshot);
  updateMarkers(snapshot);
}

async function fetchLatestState(newerOnly = false) {
  if (state.stateFetchInFlight) return null;
  state.stateFetchInFlight = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const snapshot = await response.json();
    state.liveSnapshot = snapshot;
    if (state.currentView !== "replay" && (!newerOnly || snapshotVersion(snapshot) > state.lastVersion)) {
      render(snapshot);
    }
    return snapshot;
  } finally {
    state.stateFetchInFlight = false;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function boot() {
  const snapshot = await fetchLatestState();
  initMap(snapshot.server);

  document.querySelectorAll(".view-tab").forEach((button) => {
    button.addEventListener("click", () => setActiveView(button.dataset.view || "live"));
  });
  byId("refresh-statistics").addEventListener("click", () => refreshStatistics().catch((error) => console.error(error)));
  initReplayPanelInteractions();
  byId("replay-play").addEventListener("click", toggleReplayPlayback);
  byId("replay-speed").addEventListener("change", () => {
    state.replay.speed = Number(byId("replay-speed").value) || 1;
    state.replay.lastTickMs = Date.now();
    updateReplayControls();
  });
  byId("replay-scrubber").addEventListener("input", () => {
    const sliderSeconds = Number(byId("replay-scrubber").value);
    stopReplay();
    const range = state.replay.range;
    const fromMs = Number(range?.from_ms);
    if (Number.isFinite(fromMs)) {
      loadReplayAt(fromMs + sliderSeconds * 1000).catch((error) =>
        console.error("Replay refresh failed", error)
      );
    }
  });
  byId("replay-timestamp").addEventListener("change", () => {
    const atMs = msFromInputDateTime(byId("replay-timestamp").value);
    if (atMs !== null) {
      stopReplay();
      loadReplayAt(atMs).catch((error) => console.error("Replay refresh failed", error));
    }
  });
  updateStatisticsRangeTabs();
  document.querySelectorAll(".statistics-range-tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.statisticsRange = button.dataset.range || "24h";
      updateStatisticsRangeTabs();
      restartStatisticsStream();
    });
  });

  const events = new EventSource("/events");
  events.onopen = () => {
    state.eventStreamConnected = true;
    fetchLatestState(true).catch((error) => {
      console.error("State refresh after SSE reconnect failed", error);
    });
  };
  events.addEventListener("state", (event) => {
    state.eventStreamConnected = true;
    const snapshot = JSON.parse(event.data);
    state.liveSnapshot = snapshot;
    if (state.currentView !== "replay") {
      render(snapshot);
    }
  });
  events.onerror = () => {
    state.eventStreamConnected = false;
    if (state.currentView !== "replay") {
      byId("live-label").textContent = "Reconnecting";
      byId("live-dot").className = "status-dot offline";
    }
  };
  setInterval(refreshAgeSensitiveUi, 500);
  setInterval(() => {
    if (state.eventStreamConnected) return;
    fetchLatestState(true).catch((error) => {
      console.error("State refresh failed", error);
    });
  }, state.fallbackPollMs);
}

boot().catch((error) => {
  console.error(error);
  byId("live-label").textContent = "Error";
  byId("live-dot").className = "status-dot offline";
});
