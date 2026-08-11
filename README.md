# RTK Dashboard

Local MQTT ingestion, history, and web visualization for multiple IoT device
types. It includes a compatibility profile for `crane-rover`, plus built-in
profiles for tide sensors, weather stations, and trucks.

Existing rovers can keep publishing the same `batch_ds` JSON payload. New
devices can publish ordinary JSON without changing the MQTT or storage layers.

## What This Runs

- MQTT listener on `0.0.0.0:1883`
- Web dashboard on `0.0.0.0:8080`
- Optional UDP peer listener on `0.0.0.0:5005`
- Server-sent events for live browser updates

No Node, npm, Mosquitto, or other external services are required.

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/mustafa-turner/rtk-dashboard.git
cd rtk-dashboard
```

### 2. Create `config.yaml`

Start from the example file:

```bash
cp config.example.yaml config.yaml
```

You can run without a config file, but creating one is the easiest way to make
changes explicit and repeatable.

### 3. Start the server

```bash
python3 server.py --config config.yaml
```

### 4. Open the dashboard

On the same machine:

```text
http://127.0.0.1:8080
```

From another device on the LAN:

```text
http://<this-machine-ip>:8080
```

### 5. Point a rover at this machine

Set the rover's MQTT broker to this machine's IP on port `1883`.

If you want to verify the dashboard before touching a rover, use the sample
publisher:

```bash
python3 tools/publish_sample.py
```

Publish all built-in device examples with:

```bash
python3 tools/publish_sample.py --type all
```

## First Install Checklist

For a new install, confirm these basics first:

- `python3` is available
- Port `1883` is open for rover MQTT traffic
- Port `8080` is open for the web UI
- `config.yaml` exists if you want non-default settings
- The rover is using plain MQTT on `1883`, not TLS

## Configuration

The server reads `config.yaml` by default. You can also pass a custom path with
`--config`.

Example:

```yaml
mqtt:
  host: 0.0.0.0
  port: 1883

http:
  host: 0.0.0.0
  port: 8080

udpPeers:
  enabled: true
  host: 0.0.0.0
  port: 5005
  maxAgeSec: 5

logging:
  enabled: true
  databasePath: data/rtk-dashboard.sqlite
  rawRetentionDays: 30
  summaryRetentionDays: 0
  sampleMinIntervalSec: 2
  rollupIntervalSec: 300

dashboard:
  title: IoT Device Dashboard
  roverAntennaOffset:
    x: 0
    y: 0
  deviceNames:
    # 192.168.1.21: rover-alpha
  defaultCenter:
    latitude: -2.5489
    longitude: 118.0149
    zoom: 5

devices:
  disconnectedAfterSec: 5
  types: {}
```

### Common Settings

- `mqtt.host` / `mqtt.port`: where the local MQTT listener binds
- `mqtt.advertisedHost`: address shown in the dashboard for devices to use;
  `auto` selects the IP on the machine's default network route
- `mqtt.username` / `mqtt.passwordEnv`: optional MQTT authentication; the
  password is read from an environment variable and is never returned by the API
- `http.host` / `http.port`: where the dashboard web server binds
- `udpPeers.enabled`: enable or disable peer discovery traffic
- `logging.enabled`: enable or disable the SQLite history database
- `logging.rawRetentionDays`: cap detailed sample storage; hourly summaries are
  retained indefinitely when `summaryRetentionDays` is `0`
- `dashboard.title`: title shown in the browser
- `dashboard.defaultCenter`: default map center and zoom
- `dashboard.deviceNames`: manual display names for device IDs, client IDs,
  usernames, or source IPs
- `devices.types`: optional profile definitions for new device types

### MQTT Address And Credentials

The top-left header shows a connection address such as
`MQTT mqtt://192.168.1.50:1883`. Binding to `0.0.0.0` lets the server listen on
all interfaces, but devices must connect to the advertised LAN address—not to
`0.0.0.0`. If `auto` chooses the wrong interface on a machine with Ethernet,
Wi-Fi, or a VPN, set the address explicitly:

```yaml
mqtt:
  host: 0.0.0.0
  port: 1883
  advertisedHost: 192.168.1.50
  username: device
  passwordEnv: RTK_DASHBOARD_MQTT_PASSWORD
```

The IP address, port, topic, username, and device ID are configuration—not
secrets. The MQTT password and Wi-Fi password are secrets. Keep them out of
Git, screenshots, logs, and telemetry payloads.

For an interactive launch:

```bash
export RTK_DASHBOARD_MQTT_PASSWORD='replace-with-a-long-random-password'
python3 server.py --config config.yaml
```

For systemd, create `/etc/rtk-dashboard.env`:

```text
RTK_DASHBOARD_MQTT_PASSWORD=replace-with-a-long-random-password
```

Then protect and restart it:

```bash
sudo chown root:root /etc/rtk-dashboard.env
sudo chmod 600 /etc/rtk-dashboard.env
sudo systemctl daemon-reload
sudo systemctl restart rtk-dashboard
```

This built-in broker uses plain MQTT on port 1883. Restrict it to a trusted LAN
or VPN and do not expose it directly to the public internet. Authentication
prevents accidental clients from publishing but does not encrypt traffic.

### Tide Logger Payload

The built-in `tide_sensor` profile matches the ESP32 tide logger and accepts its
water level, ultrasonic distance, power measurements, SHT40 readings, quality
diagnostics, and offline-queue counters. Publish JSON to:

```text
telemetry/tide_sensor/tide-station-01
```

Use `measured_at_ms` for the original UTC measurement time. Delayed LittleFS
records are stored and charted at that time rather than their later upload time.
See `docs/tide-logger-codex-prompt.md` for the complete firmware integration
contract.

## Verify A New Install

### Option 1: Use sample data

In one shell:

```bash
python3 server.py --config config.yaml
```

In another shell:

```bash
python3 tools/publish_sample.py
```

You should see a rover appear in the dashboard.

### Option 2: Use a real rover

Start the dashboard, then update the rover MQTT settings to use this machine as
the broker on port `1883`. Once telemetry starts publishing to `batch_ds`, the
dashboard should update automatically.

## Statistics History

When `logging.enabled` is true, the dashboard writes telemetry history to a
local SQLite database. The default `data/rtk-dashboard.sqlite` path is relative
to this repository.

The logger keeps raw payload samples for `rawRetentionDays` and keeps hourly
summary rows indefinitely when `summaryRetentionDays` is `0`. With the current
rover publish interval of about 2 seconds, expect roughly `40-120 MB` per day
per device for raw samples, depending on payload size. The default 30 day
retention is comfortable on a 1 TB SSD, and hourly summaries are tiny.

The Statistics tab uses a shared server-sent event stream. Browsers with the
same range and device subscription share one server-side calculation schedule,
and each new subscriber receives the group's latest cached snapshot:

- `/api/statistics/stream?range=live|24h|7d|30d&device_id=...`
- `POST /api/statistics/refresh?range=...&device_id=...` for manual refresh

The underlying local logging API endpoints remain available for diagnostics and
other consumers:

- `/api/logs/summary?range=24h|7d|30d&device_id=...`
- `/api/logs/hourly?from=...&to=...&device_id=...`
- `/api/logs/events?from=...&to=...&device_id=...`
- `/api/logs/samples?from=...&to=...&device_id=...&limit=500`

While the Statistics tab is open, the server pushes shared snapshots at an
interval appropriate for the selected range. Use the top device buttons to choose
which device to plot, then switch between `30 days`, `7 days`, `24 hours`, and
`Live`. The live chart plots recent raw samples at the device publish cadence.
Moving across a chart shows the nearest timestamp and value.

The raw sample table stores the full JSON payload, so unprofiled devices are
logged without schema migrations. The logger also records telemetry disconnect and
reconnect events so spotty network periods can be reviewed later.

## Adding Another Device Type

All JSON objects are accepted. Unknown explicit types use the generic device
card and keep every payload field. A device can identify itself in the payload:

```json
{
  "device_id": "soil-17",
  "device_type": "soil_probe",
  "moisture_percent": 43.2,
  "battery_percent": 88
}
```

Recommended MQTT topics are:

- `telemetry/<device-id>` when the payload contains `device_type`
- `telemetry/<device-type>/<device-id>` when the type belongs in the topic
- `devices/<device-id>/telemetry` for a conventional device-first hierarchy

The existing `batch_ds`, `batch_ds/<device-id>`, `ds/<field>`, and `info/mcu`
routes remain supported.

To give a new type aliases and a curated telemetry card, declare it under
`devices.types`. No Python or JavaScript changes are required:

```yaml
devices:
  types:
    soil_probe:
      label: Soil Probe
      category: environment
      supportsMap: true
      matchFields: [moisture_percent, soil_temperature_c]
      aliases:
        battery_percent: [battery_pct]
      metrics:
        moisture_percent:
          label: Moisture
          unit: "%"
          digits: 1
        soil_temperature_c:
          label: Soil Temperature
          unit: " °C"
          digits: 1
```

Built-in profiles live in `rtk_dashboard/device_profiles.py`. Add code there
only when a type needs behavior beyond configuration, such as crane peer-safety
semantics.

## Code Layout

- `server.py`: small compatibility entry point
- `rtk_dashboard/device_profiles.py`: type inference, aliases, and UI metadata
- `rtk_dashboard/state.py`: live device state and ingestion orchestration
- `rtk_dashboard/storage.py`: SQLite samples, rollups, queries, and replay
- `rtk_dashboard/statistics.py`: shared statistics subscriptions
- `rtk_dashboard/mqtt.py` and `peer_udp.py`: transport adapters
- `rtk_dashboard/http_server.py` and `tiles.py`: HTTP/SSE and maps
- `static/device-ui.js`: browser-side device profile rendering
- `static/app.js`: dashboard interaction, maps, replay, and charts

## Rover Config

On each rover using `crane-rover`, update the `blynk` section in its
`config.yaml`.

### Local Pi Dashboard

```yaml
blynk:
  enabled: true
  broker: 192.168.1.50
  port: 1883
  username: device
  authToken: your_blynk_device_auth_token
  templateId: TMPLxxxxxxx
  firmwareVersion: 0.1.0
  publishIntervalSec: 2
```

Replace `192.168.1.50` with this machine's LAN or ZeroTier IP.

Important changes:

- `broker` points to this machine
- `port` is `1883`

The local dashboard does not validate the Blynk `authToken`, so you can reuse
the same `username` and `authToken` from your Blynk config if that makes
switching easier.

### Blynk Cloud

```yaml
blynk:
  enabled: true
  broker: blynk.cloud
  port: 8883
  username: device
  authToken: your-blynk-auth-token
  templateId: TMPLxxxxxxx
  firmwareVersion: 0.1.0
  publishIntervalSec: 2
```

When switching back to Blynk:

- `broker` points to Blynk
- `port` is usually `8883`
- TLS must be enabled in the rover code, which is the current default

Do not commit real Blynk auth tokens to this repository.

## TLS Warning For Rover MQTT

This dashboard listens for plain MQTT on `1883`.

Current `crane-rover` code defaults Blynk MQTT to TLS in `rover/blynk.py`. If
the rover still tries TLS while pointed at this dashboard, the connection will
fail even if the broker and port are correct.

The rover code needs TLS disabled for local MQTT:

```python
"blynk": {
    "enabled": True,
    "broker": "blynk.cloud",
    "port": 8883,
    "username": "device",
    "authToken": "",
    "templateId": "",
    "firmwareVersion": "0.1.0",
    "useTls": True,
}
```

For the local dashboard profile, the rover should end up with:

```yaml
blynk:
  enabled: true
  broker: 10.33.240.3
  port: 1883
  username: device
  authToken: your_blynk_device_auth_token
  templateId: TMPLxxxxxxx
  firmwareVersion: 0.1.0
  useTls: false
```

If `useTls` is not exposed in the rover's public config template, it may need
to be added there first.

### `Connection reset by peer` on the rover

If the rover logs:

```text
Blynk MQTT error: [Errno 104] Connection reset by peer
```

the most likely cause is a TLS mismatch:

- the rover is trying TLS
- this dashboard expects plain MQTT on `1883`

Restart the rover after changing its config.

## Rover Names

The dashboard list is sorted alphabetically by display name. Display names are
read from common payload fields such as `rover_name`, `device_name`,
`hostname`, `Name`, or `name`.

If a rover still only identifies itself by source IP, set a local display name
in `config.yaml`:

```yaml
dashboard:
  roverAntennaOffset:
    x: 0
    y: 0
  roverNames:
    192.168.1.21: rover-alpha
    sample-rover: sample-rover
```

Keys can match `device_id`, MQTT client ID, username, or source IP.

`roverAntennaOffset` is measured in the original `CC.png` image pixels. `x`
and `y` are relative to the icon itself, and the dashboard rotates that offset
with the icon before placing it on the map.

## Install As A Service

The included service file is written for a Raspberry Pi install at
`/home/pi/rtk-dashboard` and runs as user `pi`.

If your install path or user is different, edit
[systemd/rtk-dashboard.service](/Users/mustafa/Documents/GitHub/rtk-dashboard/systemd/rtk-dashboard.service)
before copying it into `/etc/systemd/system/`.

```bash
sudo cp systemd/rtk-dashboard.service /etc/systemd/system/rtk-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable rtk-dashboard.service
sudo systemctl start rtk-dashboard.service
```

Check logs:

```bash
journalctl -u rtk-dashboard.service -f
```

Restart after changes:

```bash
sudo systemctl restart rtk-dashboard.service
```

## Crane Rover Telemetry Fields

The dashboard displays these existing Blynk-style telemetry fields:

- `latitude`
- `longitude`
- `altitude_m`
- `position`
- `satellites`
- `hdop`
- `rtcm_age_sec`
- `fix_mode`
- `ntrip_status`
- `battery_percent`
- `battery_voltage_v`
- `battery_current_a`
- `battery_power_w`
- `battery_status`
- `battery_present`
- `local_accuracy_m`
- `nearest_peer_distance_m`
- `nearest_peer_safe_distance_m`
- `nearest_peer_uncertainty_m`
- `nearest_peer_combined_accuracy_m`
- `nearest_peer_accuracy_m`
- `nearest_peer_fix_mode`
- `nearest_peer_id`
- optional uptime fields such as `uptime_sec`, `app_uptime_sec`, or
  `device_uptime_sec`

Every device type may include optional `device_id` / `deviceId`, `device_type`
/ `deviceType`, latitude/longitude (including `lat`, `lon`, or `lng` aliases),
battery values, and arbitrary type-specific fields. Crane rovers additionally
accept the fields above.
