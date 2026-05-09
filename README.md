# RTK Dashboard

Local MQTT replacement for Blynk plus a web dashboard for `crane-rover`
telemetry.

The rover can keep publishing the same `batch_ds` JSON payload. Point it at
this machine instead of Blynk and the dashboard will accept the existing data
shape.

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

dashboard:
  title: Crane Rover Dashboard
  roverAntennaOffset:
    x: 0
    y: 0
  roverNames:
    # 192.168.1.21: rover-alpha
  defaultCenter:
    latitude: -2.5489
    longitude: 118.0149
    zoom: 5
```

### Common Settings

- `mqtt.host` / `mqtt.port`: where the local MQTT listener binds
- `http.host` / `http.port`: where the dashboard web server binds
- `udpPeers.enabled`: enable or disable peer discovery traffic
- `dashboard.title`: title shown in the browser
- `dashboard.defaultCenter`: default map center and zoom
- `dashboard.roverNames`: manual display names for device IDs, client IDs,
  usernames, or source IPs

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

## Accepted Telemetry Fields

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

It also accepts optional `device_id` or `deviceId` in the payload, plus
`batch_ds/<device_id>` topic variants if you later move each rover to its own
topic.
