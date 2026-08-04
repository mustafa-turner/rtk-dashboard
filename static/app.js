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
  logs: null,
  logsFetchInFlight: false,
  logChartMode: "hourly",
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

function percent(numerator, denominator, digits = 0) {
  const top = Number(numerator);
  const bottom = Number(denominator);
  if (!Number.isFinite(top) || !Number.isFinite(bottom) || bottom <= 0) {
    return "-";
  }
  return `${((top / bottom) * 100).toFixed(digits)}%`;
}

function snapshotNowMs(snapshot) {
  const serverNowMs = Number(snapshot?.server?.now_ms);
  if (!Number.isFinite(serverNowMs)) {
    return Date.now();
  }
  return serverNowMs;
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
    Number(device?.last_seen_ms) ||
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
  const ageMs = ageMsFromLastSeen(device?.last_seen_ms, snapshot);
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
  const nowMs = snapshotNowMs(snapshot);
  byId("dashboard-title").textContent = dashboard.title || "Crane Rover Dashboard";
  byId("mqtt-address").textContent = `MQTT ${snapshot.server.mqtt.host}:${snapshot.server.mqtt.port}`;
  byId("device-count").textContent = `${Object.keys(snapshot.devices).length} rovers`;
  byId("peer-count").textContent = `${Object.keys(snapshot.peers).length} peers`;

  const selected = selectedDevice();
  const liveDot = byId("live-dot");
  const liveLabel = byId("live-label");
  liveDot.className = "status-dot";
  if (!selected) {
    liveLabel.textContent = "Waiting";
    return;
  }
  const ageMs = nowMs - selected.last_seen_ms;
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
    panel.innerHTML = `<div class="empty">Waiting for rover telemetry</div>`;
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
    list.innerHTML = `<div class="header-empty">Waiting for rovers</div>`;
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
    const statusClass = deviceIsDisconnected(device, snapshot) ? "" : statusClassForFix(telemetry.fix_mode);

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
    const title = `${displayNameForDevice(device)} - ${telemetry.fix_mode_label || fixLabels[telemetry.fix_mode] || "UNKNOWN"}`;
    const disconnected = deviceIsDisconnected(device, snapshot);
    const fillColor = disconnected ? "#657080" : "#0f7490";
    const marker = updateRoverMarker(state.deviceMarkers.get(device.device_id), latLng, {
      variant: "device",
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
    byId("selected-label").textContent = "No rover selected";
  } else if (latLng) {
    byId("selected-label").textContent = `${displayNameForDevice(device)} - ${latLng[0].toFixed(7)}, ${latLng[1].toFixed(7)}`;
  } else {
    byId("selected-label").textContent = `${displayNameForDevice(device)} - waiting for coordinates`;
  }
}

function renderRawPayloads(rovers) {
  const raw = byId("raw-fields");
  if (!rovers.length) {
    raw.innerHTML = `<div class="empty">No crane payloads yet</div>`;
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
  byId("live-view").hidden = view !== "live";
  byId("logging-view").hidden = view !== "logging";
  if (view === "logging") {
    refreshLogs().catch((error) => console.error("Log refresh failed", error));
  } else if (state.map) {
    window.requestAnimationFrame(() => state.map.invalidateSize());
  }
}

function logRangeMs() {
  return parseRangeToMs(byId("log-range")?.value || "24h");
}

function parseRangeToMs(value) {
  const text = String(value || "24h").trim().toLowerCase();
  const amount = Number.parseFloat(text);
  if (!Number.isFinite(amount) || amount <= 0) return 24 * 60 * 60 * 1000;
  if (text.endsWith("d")) return amount * 24 * 60 * 60 * 1000;
  if (text.endsWith("w")) return amount * 7 * 24 * 60 * 60 * 1000;
  return amount * 60 * 60 * 1000;
}

function updateLogDeviceOptions(snapshot) {
  const select = byId("log-device");
  if (!select) return;
  const previous = select.value;
  const devices = sortedDevices(snapshot);
  select.innerHTML = `<option value="">All devices</option>`;
  devices.forEach((device) => {
    const option = document.createElement("option");
    option.value = device.device_id;
    option.textContent = displayNameForDevice(device);
    select.append(option);
  });
  select.value = devices.some((device) => device.device_id === previous) ? previous : "";
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${url} returned ${response.status}`);
  }
  return response.json();
}

async function refreshLogs() {
  if (state.logsFetchInFlight) return;
  state.logsFetchInFlight = true;
  const range = byId("log-range")?.value || "24h";
  const rangeMs = logRangeMs();
  const to = Date.now();
  const from = to - rangeMs;
  const deviceId = byId("log-device")?.value || "";
  const params = new URLSearchParams({ range, from: String(from), to: String(to) });
  if (deviceId) params.set("device_id", deviceId);
  const sampleParams = new URLSearchParams({ from: String(to - 10 * 60 * 1000), to: String(to), limit: "500" });
  if (deviceId) sampleParams.set("device_id", deviceId);
  try {
    const requests = [
      fetchJson(`/api/logs/summary?${params.toString()}`),
      fetchJson(`/api/logs/hourly?${params.toString()}`),
      fetchJson(`/api/logs/events?${params.toString()}&limit=80`),
    ];
    if (state.logChartMode === "live") {
      requests.push(fetchJson(`/api/logs/samples?${sampleParams.toString()}`));
    }
    const [summary, hourly, events, samples] = await Promise.all(requests);
    state.logs = { summary, hourly, events, samples: samples || state.logs?.samples || null };
    renderLogs();
  } finally {
    state.logsFetchInFlight = false;
  }
}

function metricCard(label, value, note = "") {
  return `
    <div class="log-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(note)}</small>
    </div>
  `;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "-";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (hours >= 24) return `${(hours / 24).toFixed(1)} d`;
  if (hours > 0) return `${hours} h ${minutes} m`;
  return `${minutes} m`;
}

function renderLogMetrics(summary) {
  const sampleCount = Number(summary?.sample_count) || 0;
  const fixTotal =
    (Number(summary?.fix_fixed_count) || 0) +
    (Number(summary?.fix_float_count) || 0) +
    (Number(summary?.fix_no_count) || 0);
  const ntripTotal =
    (Number(summary?.ntrip_connected_count) || 0) + (Number(summary?.ntrip_disconnected_count) || 0);
  const closest = summary?.closest;
  const closestValue = closest?.closest_safe_distance_m ?? closest?.closest_raw_distance_m;

  byId("log-metric-grid").innerHTML = [
    metricCard("Samples", sampleCount.toLocaleString(), `Last ${dateTimeLabel(summary?.last_sample_ms)}`),
    metricCard("Closest Distance", numeric(closestValue, 2, " m"), closest ? dateTimeLabel(closest.closest_at_ms) : "-"),
    metricCard("RTK Fixed", percent(summary?.fix_fixed_count, fixTotal), `${summary?.fix_fixed_count || 0} fixed samples`),
    metricCard("NTRIP Connected", percent(summary?.ntrip_connected_count, ntripTotal), `${summary?.ntrip_connected_count || 0} connected samples`),
    metricCard("Resets", String(summary?.reset_count || 0), `Max uptime ${formatDuration(summary?.uptime_max_sec)}`),
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
    };
    item.sample_count += Number(row.sample_count) || 0;
    item.fix_fixed_count += Number(row.fix_fixed_count) || 0;
    item.fix_float_count += Number(row.fix_float_count) || 0;
    item.fix_no_count += Number(row.fix_no_count) || 0;
    item.ntrip_connected_count += Number(row.ntrip_connected_count) || 0;
    item.ntrip_disconnected_count += Number(row.ntrip_disconnected_count) || 0;
    grouped.set(hour, item);
  });
  return Array.from(grouped.values()).sort((a, b) => a.hour_ms - b.hour_ms);
}

function buildLinePoints(rows, valueGetter) {
  return rows
    .map((row) => {
      const value = valueGetter(row);
      const time = Number(row.at_ms ?? row.closest_at_ms ?? row.hour_ms);
      if (!Number.isFinite(time) || !Number.isFinite(value)) return null;
      return { row, time, value };
    })
    .filter(Boolean)
    .sort((a, b) => a.time - b.time);
}

function linePath(points, width, height, padding, minValue, maxValue) {
  const xMin = points[0].time;
  const xMax = points[points.length - 1].time;
  const xSpan = Math.max(1, xMax - xMin);
  const ySpan = Math.max(1, maxValue - minValue);
  return points
    .map((point, index) => {
      const x = padding.left + ((point.time - xMin) / xSpan) * (width - padding.left - padding.right);
      const y = padding.top + (1 - (point.value - minValue) / ySpan) * (height - padding.top - padding.bottom);
      point.x = x;
      point.y = y;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}

function renderLineChart(el, rows, valueGetter, { suffix = "", color = "#0f7490", digits = 1 } = {}) {
  const points = buildLinePoints(rows, valueGetter);
  if (!points.length) {
    el.innerHTML = `<div class="chart-empty">No matching values yet</div>`;
    return;
  }
  const width = 520;
  const height = 210;
  const padding = { top: 18, right: 18, bottom: 28, left: 42 };
  const values = points.map((point) => point.value);
  let minValue = Math.min(...values);
  let maxValue = Math.max(...values);
  if (minValue === maxValue) {
    minValue = Math.max(0, minValue - 1);
    maxValue += 1;
  } else {
    const pad = (maxValue - minValue) * 0.12;
    minValue = Math.max(0, minValue - pad);
    maxValue += pad;
  }
  const path = linePath(points, width, height, padding, minValue, maxValue);
  const fillPath = `${path} L ${points[points.length - 1].x.toFixed(2)} ${height - padding.bottom} L ${points[0].x.toFixed(2)} ${height - padding.bottom} Z`;
  const pointDots = points
    .map(
      (point, index) => `
        <circle
          class="chart-point"
          cx="${point.x.toFixed(2)}"
          cy="${point.y.toFixed(2)}"
          r="8"
          data-index="${index}"
          tabindex="0"
        ></circle>
      `
    )
    .join("");

  el.innerHTML = `
    <svg class="line-chart-svg" viewBox="0 0 ${width} ${height}" role="img" style="--chart-color: ${color}">
      <line class="chart-grid-line" x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}"></line>
      <line class="chart-grid-line" x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"></line>
      <path class="chart-area" d="${fillPath}"></path>
      <path class="chart-line" d="${path}"></path>
      ${pointDots}
    </svg>
    <div class="chart-tooltip" hidden></div>
  `;

  const tooltip = el.querySelector(".chart-tooltip");
  const showTooltip = (point) => {
    tooltip.hidden = false;
    tooltip.innerHTML = `
      <strong>${escapeHtml(Number(point.value).toFixed(digits) + suffix)}</strong>
      <span>${escapeHtml(timeLabel(point.time))}</span>
    `;
    tooltip.style.left = `${(point.x / width) * 100}%`;
    tooltip.style.top = `${(point.y / height) * 100}%`;
  };
  el.querySelectorAll(".chart-point").forEach((dot) => {
    const point = points[Number(dot.dataset.index)];
    dot.addEventListener("mouseenter", () => showTooltip(point));
    dot.addEventListener("focus", () => showTooltip(point));
  });
  el.addEventListener("mouseleave", () => {
    tooltip.hidden = true;
  });
}

function renderLogCharts(hourly) {
  if (state.logChartMode === "live") {
    renderLiveSampleCharts(state.logs?.samples);
    return;
  }

  byId("distance-chart-title").textContent = "Closest Distance By Hour";
  byId("rtk-chart-title").textContent = "RTK Fixed Rate";
  byId("ntrip-chart-title").textContent = "NTRIP Connected Rate";

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
    { suffix: "%", color: "#0f8b5f", digits: 0 }
  );
  renderLineChart(
    byId("ntrip-chart"),
    deviceRows,
    (row) => {
      const total = (Number(row.ntrip_connected_count) || 0) + (Number(row.ntrip_disconnected_count) || 0);
      return total > 0 ? ((Number(row.ntrip_connected_count) || 0) / total) * 100 : NaN;
    },
    { suffix: "%", color: "#0f7490", digits: 0 }
  );
}

function renderLiveSampleCharts(samplesPayload) {
  const samples = samplesPayload?.samples || [];
  byId("distance-chart-title").textContent = "Live Distance";
  byId("rtk-chart-title").textContent = "Live RTK Fixed";
  byId("ntrip-chart-title").textContent = "Live NTRIP Connected";
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
    { suffix: "%", color: "#0f8b5f", digits: 0 }
  );
  renderLineChart(
    byId("ntrip-chart"),
    samples,
    (row) => (Number(row.ntrip_status) === 1 ? 100 : 0),
    { suffix: "%", color: "#0f7490", digits: 0 }
  );
}

function renderLogEvents(eventsPayload) {
  const events = eventsPayload?.events || [];
  const panel = byId("log-events");
  if (!events.length) {
    panel.innerHTML = `<div class="log-event-empty">No events for this range</div>`;
    return;
  }
  panel.innerHTML = events
    .map(
      (event) => `
        <div class="log-event">
          <div class="log-event-time">${escapeHtml(dateTimeLabel(event.at_ms))}</div>
          <div class="log-event-message">
            <strong>${escapeHtml(event.message || event.event_type)}</strong>
            <div class="log-event-device">${escapeHtml(event.device_id || "Dashboard")}</div>
          </div>
          <div class="log-event-type">${escapeHtml(event.event_type || "event")}</div>
        </div>
      `
    )
    .join("");
}

function renderLogs() {
  const logs = state.logs;
  if (!logs?.summary?.enabled) {
    byId("log-metric-grid").innerHTML = metricCard("Logging", "Disabled", "Enable logging in config.yaml");
    byId("distance-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("rtk-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("ntrip-chart").innerHTML = `<div class="chart-empty">Logging is disabled</div>`;
    byId("log-events").innerHTML = `<div class="log-event-empty">Logging is disabled</div>`;
    return;
  }
  renderLogMetrics(logs.summary);
  renderLogCharts(logs.hourly);
  renderLogEvents(logs.events);
}

function render(snapshot) {
  state.lastVersion = Math.max(state.lastVersion, snapshotVersion(snapshot));
  state.data = snapshot;
  ensureSelectedDevice(snapshot);
  const selected = selectedDevice();
  const safetyRovers = buildSafetyRovers(selected, snapshot);
  const payloadRovers = buildPayloadRovers(selected, snapshot);
  updateHeader(snapshot);
  updateSafety(selected, snapshot, safetyRovers);
  renderTelemetryCompare(safetyRovers);
  renderHeaderRovers(snapshot);
  updateLogDeviceOptions(snapshot);
  updateMarkers(snapshot);
  updateSelectedLabel(selected);
  renderRawPayloads(payloadRovers);
}

function refreshAgeSensitiveUi() {
  const snapshot = state.data;
  if (!snapshot) return;

  const selected = selectedDevice();
  const safetyRovers = buildSafetyRovers(selected, snapshot);
  if (state.eventStreamConnected) {
    updateHeader(snapshot);
  }
  updateSafety(selected, snapshot, safetyRovers);
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
    if (!newerOnly || snapshotVersion(snapshot) > state.lastVersion) {
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
  byId("log-range").addEventListener("change", () => refreshLogs().catch((error) => console.error(error)));
  byId("log-device").addEventListener("change", () => refreshLogs().catch((error) => console.error(error)));
  byId("refresh-logs").addEventListener("click", () => refreshLogs().catch((error) => console.error(error)));
  byId("log-chart-mode").addEventListener("change", (event) => {
    state.logChartMode = event.target.value === "live" ? "live" : "hourly";
    refreshLogs().catch((error) => console.error(error));
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
    render(JSON.parse(event.data));
  });
  events.onerror = () => {
    state.eventStreamConnected = false;
    byId("live-label").textContent = "Reconnecting";
    byId("live-dot").className = "status-dot offline";
  };
  setInterval(refreshAgeSensitiveUi, 500);
  setInterval(() => {
    if (state.eventStreamConnected) return;
    fetchLatestState(true).catch((error) => {
      console.error("State refresh failed", error);
    });
  }, state.fallbackPollMs);
  setInterval(() => {
    if (state.currentView === "logging") {
      refreshLogs().catch((error) => console.error("Log refresh failed", error));
    }
  }, 2000);
}

boot().catch((error) => {
  console.error(error);
  byId("live-label").textContent = "Error";
  byId("live-dot").className = "status-dot offline";
});
