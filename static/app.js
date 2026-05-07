const state = {
  data: null,
  selectedId: null,
  map: null,
  deviceMarkers: new Map(),
  peerMarkers: new Map(),
  eventStreamConnected: true,
};

const SAFETY_STALE_MS = 2000;

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

function snapshotNowMs(snapshot) {
  const serverNowMs = Number(snapshot?.server?.now_ms);
  const receivedAtMs = Number(snapshot?.client_received_at_ms);
  if (!Number.isFinite(serverNowMs)) {
    return Date.now();
  }
  if (!Number.isFinite(receivedAtMs)) {
    return serverNowMs;
  }
  return serverNowMs + Math.max(0, Date.now() - receivedAtMs);
}

function roverAgeMs(rover, snapshot) {
  const lastSeenMs = Number(rover?.lastSeenMs);
  if (!Number.isFinite(lastSeenMs) || lastSeenMs <= 0) {
    return null;
  }
  return Math.max(0, snapshotNowMs(snapshot) - lastSeenMs);
}

function roverIsStaleForSafety(rover, snapshot) {
  const ageMs = roverAgeMs(rover, snapshot);
  return ageMs !== null && ageMs > SAFETY_STALE_MS;
}

function statusClassForFix(fixMode) {
  if (fixMode === 4) return "good";
  if (fixMode === 3 || fixMode === 2) return "warn";
  return "bad";
}

function safeDistanceClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "unknown";
  if (number < 25) return "bad";
  // if (number < 25) return "warn";
  return "good";
}

function safeDistanceLabel(value) {
  const status = safeDistanceClass(value);
  if (status === "good") return "CLEAR";
  if (status === "warn") return "CAUTION";
  if (status === "bad") return "DANGER";
  return "WAITING";
}

function safeDistanceValue(value) {
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

function initMap(config) {
  if (typeof L === "undefined") {
    const mapEl = byId("map");
    mapEl.classList.add("map-fallback");
    mapEl.textContent = "Map unavailable";
    byId("center-map").disabled = true;
    return;
  }

  const center = config?.dashboard?.defaultCenter || {};
  state.map = L.map("map", { zoomControl: true }).setView(
    [Number(center.latitude) || -2.5489, Number(center.longitude) || 118.0149],
    Number(center.zoom) || 5
  );
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);

  byId("center-map").addEventListener("click", () => {
    if (!state.map) return;
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
  if (ageMs < 6000) {
    liveDot.classList.add("live");
    liveLabel.textContent = "Live";
  } else {
    liveDot.classList.add("offline");
    liveLabel.textContent = "Stale";
  }
}

function roverSummaryFromDevice(device, role, snapshot) {
  const telemetry = device?.telemetry || {};
  const nowMs = snapshotNowMs(snapshot);
  return {
    kind: "device",
    role,
    name: displayNameForDevice(device),
    id: device?.device_id || "",
    lastSeenMs: device?.last_seen_ms || 0,
    telemetry,
    fix: telemetry.fix_mode_label || fixLabels[telemetry.fix_mode] || "UNKNOWN",
    accuracy: numeric(telemetry.local_accuracy_m, 3, " m"),
    source: device?.source_host || "",
    age: device?.last_seen_ms ? ageLabel(device.last_seen_ms, nowMs) : "",
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
  const safetyIsStale = safetyRovers.slice(0, 2).some((rover) => roverIsStaleForSafety(rover, snapshot));
  const safeDistance = safetyIsStale ? null : telemetry.nearest_peer_safe_distance_m;
  const stateClass = safeDistanceClass(safeDistance);
  const roverOne = safetyRovers[0]?.name || "-";
  const roverTwo = safetyRovers[1]?.name || telemetry.nearest_peer_id || "-";

  byId("safety-panel").className = `safety-panel safe-${stateClass}`;
  byId("safety-state").textContent = safeDistanceLabel(safeDistance);
  byId("safety-value").textContent = safeDistanceValue(safeDistance);
  byId("safety-pair").textContent = device ? `${roverOne} to ${roverTwo}` : "No crane pair";
  renderSafetyRovers(safetyRovers);
  byId("safety-raw").textContent = safetyIsStale ? "-" : numeric(telemetry.nearest_peer_distance_m, 2, " m");
  byId("safety-uncertainty").textContent = safetyIsStale
    ? "-"
    : numeric(telemetry.nearest_peer_uncertainty_m, 3, " m");
  byId("safety-local-accuracy").textContent = safetyIsStale ? "-" : numeric(telemetry.local_accuracy_m, 3, " m");
  byId("safety-peer-accuracy").textContent = safetyIsStale
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

    const row = ensureHeaderRoverButton(list, deviceId);
    const isActive = deviceId === state.selectedId;
    row.className = `rover-tab${isActive ? " active" : ""}`;
    row.title = displayName;
    row.setAttribute("aria-pressed", String(isActive));
    row.querySelector(".rover-tab-name").textContent = displayName;
    row.querySelector(".rover-tab-status").className = `rover-tab-status ${statusClassForFix(telemetry.fix_mode)}`;

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
    let marker = state.deviceMarkers.get(device.device_id);
    if (!marker) {
      marker = L.circleMarker(latLng, {
        radius: 9,
        color: "#ffffff",
        weight: 3,
        fillColor: "#0f7490",
        fillOpacity: 0.95,
      }).addTo(state.map);
      state.deviceMarkers.set(device.device_id, marker);
    } else {
      marker.setLatLng(latLng);
    }
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
    let marker = state.peerMarkers.get(peer.device_id);
    if (!marker) {
      marker = L.circleMarker(latLng, {
        radius: 7,
        color: "#191b1f",
        weight: 2,
        fillColor: peer.stale ? "#b7791f" : "#0f8b5f",
        fillOpacity: 0.86,
      }).addTo(state.map);
      state.peerMarkers.set(peer.device_id, marker);
    } else {
      marker.setLatLng(latLng);
      marker.setStyle({ fillColor: peer.stale ? "#b7791f" : "#0f8b5f" });
    }
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

function render(snapshot) {
  if (!snapshot.client_received_at_ms) {
    snapshot.client_received_at_ms = Date.now();
  }
  state.data = snapshot;
  ensureSelectedDevice(snapshot);
  const selected = selectedDevice();
  const safetyRovers = buildSafetyRovers(selected, snapshot);
  const payloadRovers = buildPayloadRovers(selected, snapshot);
  updateHeader(snapshot);
  updateSafety(selected, snapshot, safetyRovers);
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
  const safetyRovers = buildSafetyRovers(selected, snapshot);
  if (state.eventStreamConnected) {
    updateHeader(snapshot);
  }
  updateSafety(selected, snapshot, safetyRovers);
  renderTelemetryCompare(safetyRovers);
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
  const response = await fetch("/api/state");
  const snapshot = await response.json();
  initMap(snapshot.server);
  render(snapshot);

  const events = new EventSource("/events");
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
}

boot().catch((error) => {
  console.error(error);
  byId("live-label").textContent = "Error";
  byId("live-dot").className = "status-dot offline";
});
