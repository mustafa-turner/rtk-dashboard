/** Device-profile helpers shared by the live dashboard UI. */

const FALLBACK_PROFILE = {
  key: "device",
  label: "Generic Device",
  category: "sensor",
  supports_map: true,
  supports_peer_safety: false,
  metrics: [],
};

export function profileForDevice(device) {
  return device?.profile && typeof device.profile === "object"
    ? { ...FALLBACK_PROFILE, ...device.profile }
    : { ...FALLBACK_PROFILE, key: String(device?.device_type || "device") };
}

export function deviceTypeLabel(device) {
  return profileForDevice(device).label;
}

export function supportsPeerSafety(device) {
  return Boolean(profileForDevice(device).supports_peer_safety);
}

export function metricDefinitions(device) {
  const metrics = profileForDevice(device).metrics;
  return Array.isArray(metrics) ? metrics : [];
}

export function countLabel(count) {
  return `${count} ${count === 1 ? "device" : "devices"}`;
}
