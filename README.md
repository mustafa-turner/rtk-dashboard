# RTK Dashboard

Local MQTT replacement for Blynk plus a web dashboard for the `crane-rover`
telemetry payload.

This service is designed to accept the same MQTT publish shape used by:

https://github.com/mustafa-turner/crane-rover

The rover currently publishes JSON telemetry to `batch_ds`. This dashboard keeps
that input intact so the rover can be pointed at this Pi instead of Blynk.

## What Runs Here

- MQTT listener on `0.0.0.0:1883`
- Web dashboard on `0.0.0.0:8080`
- Optional UDP peer listener on `0.0.0.0:5005`
- Server-sent events for live browser updates
- Per-rover settings UI with queued dashboard-to-rover requests

No Node, npm, pip, Mosquitto, or external Python packages are required.

## Rover Config

On each rover using `crane-rover`, update the `blynk` section in `config.yaml`.
To switch between the local Pi dashboard and Blynk cloud, keep two versions of
this block and swap the broker settings.

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

Replace `192.168.1.50` with this Pi's LAN or ZeroTier IP.

The important changes are:

- `broker` points to this Pi
- `port` is `1883`

The local dashboard does not validate the Blynk `authToken`, so you can reuse
the same `username` and `authToken` from your Blynk config if that makes
switching easier.

Important: current `crane-rover` code reads a hidden `useTls` setting in
`rover/blynk.py`, but `rover/config.py` does not include `useTls` in the public
config template. That means adding `useTls: false` to `config.yaml` may not take
effect unless the rover config template is also updated to preserve it. For
plain local MQTT on port `1883`, the rover code needs TLS disabled somehow:

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

After that rover-side change, this local dashboard profile should include:

```yaml
  useTls: false
```

### Blynk Cloud

Use your normal Blynk MQTT host, port, and TLS settings:

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

If your Blynk project uses a different MQTT host, keep using that host. The
important changes when switching back to Blynk are:

- `broker` points to Blynk
- `port` is usually `8883`
- TLS must be enabled in the rover code, which is the current default

Do not commit real Blynk auth tokens to this repository. Keep real credentials
only in the rover's private `config.yaml`.

### MQTT Port Reference

MQTT uses `1883` by default for plain local MQTT. MQTT over TLS commonly uses
`8883`, which is why Blynk cloud uses it for encrypted internet traffic.

### `Connection reset by peer` On The Rover

If the rover logs this while pointed at the Pi:

```text
Blynk MQTT error: [Errno 104] Connection reset by peer
```

the most likely cause is a TLS mismatch. Current `crane-rover` defaults Blynk
MQTT to TLS in `rover/blynk.py`, even when the config uses port `1883`. This
local dashboard listens for plain MQTT on `1883`, so it closes the connection
when a TLS client hello arrives instead of a plain MQTT connect packet.

Fix it in the rover repo by exposing `useTls` in `rover/config.py`:

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

Then set this in the rover's local Pi profile:

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

Restart the rover after changing the config.

The rover can keep publishing to `batch_ds`; the dashboard accepts that Blynk
style payload directly.

## Dashboard Settings UI

The gear button in the top-right header opens a per-rover settings screen. Rover
selection still uses the rover buttons in the header.

Current rover code does not apply dashboard settings yet, so the UI stays in a
safe pending state until a rover advertises settings support. The dashboard
already provides the queue, pending, ack, and log endpoints for that future rover
integration. See [howto.md](howto.md) for the rover-side implementation guide.

## Run

```bash
python3 server.py
```

Open:

```text
http://<this-pi-ip>:8080
```

## Rover Names

The dashboard list is sorted alphabetically by display name. Display names are
read from common payload fields such as `rover_name`, `device_name`, `hostname`,
`Name`, or `name`.

If a rover still only identifies itself by source IP, set a local display name
in `config.yaml`:

```yaml
dashboard:
  roverNames:
    192.168.1.21: rover-alpha
    sample-rover: sample-rover
```

Keys can match `device_id`, MQTT client ID, username, or source IP.

## Test With Sample Data

In another shell:

```bash
python3 tools/publish_sample.py
```

## Install As A Service

The service unit runs `/home/pi/rtk-dashboard/server.py` as user `pi` and starts
after the network is online.

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

Restart after dashboard changes:

```bash
sudo systemctl restart rtk-dashboard.service
```

## Accepted Telemetry Fields

The dashboard displays the existing Blynk payload fields:

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

It also accepts optional `device_id` or `deviceId` in the payload, and
`batch_ds/<device_id>` topic variants if you later make each rover publish to a
separate topic. For display names, it accepts common name fields such as
`rover_name`, `device_name`, `hostname`, `Name`, and `name`.
