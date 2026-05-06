# Rover-Side Dashboard Settings Integration

Use this in the `crane-rover` repo to connect the rover's existing USB serial
settings menu to the dashboard UI in this repo.

## Goal

The dashboard now has a per-rover settings screen. It must stay safe:

- The rover remains the source of truth for which settings exist.
- The dashboard only queues requested changes.
- The rover validates every request against the same options and setters used by
  the USB serial settings menu.
- Secrets are never echoed back in logs or telemetry.

## Source Of Truth

Do not create a second settings list by hand. Find the current USB serial menu
implementation in the rover repo and refactor the menu entries into shared
definitions, for example:

```python
SERIAL_SETTINGS = [
    {
        "title": "NTRIP",
        "fields": [
            {
                "path": "ntrip.host",
                "label": "NTRIP Caster Host",
                "type": "text",
                "getter": lambda cfg: cfg["ntrip"]["host"],
                "setter": set_ntrip_host,
            },
        ],
    },
]
```

Then have both the USB serial menu and dashboard settings handler use those same
definitions. If the current serial menu has different sections, labels, or
options, keep the serial menu exact and publish those exact sections to the
dashboard.

## Dashboard Protocol

The rover should use its `device_id` from the normal telemetry payload. These
HTTP endpoints already exist in the dashboard:

```text
GET  /api/rovers/<device_id>/settings/pending
POST /api/rovers/<device_id>/settings/ack
POST /api/rovers/<device_id>/settings/logs
```

If `settings.writeToken` is set in the dashboard config, the rover must send one
of these on every request:

```text
Authorization: Bearer <writeToken>
X-Dashboard-Settings-Token: <writeToken>
```

## Advertise Support

Publish this in `info/mcu` or in the normal telemetry payload. `info/mcu` is
better because the schema is not high-rate telemetry.

```json
{
  "device_id": "rover-alpha",
  "settings_status": {
    "supported": true,
    "schema_version": "serial-menu-v1",
    "config_version": 12
  },
  "settings_schema": [
    {
      "title": "NTRIP",
      "fields": [
        {
          "path": "ntrip.host",
          "label": "NTRIP Caster Host",
          "type": "text"
        }
      ]
    }
  ],
  "settings": {
    "ntrip": {
      "host": "caster.example.com"
    }
  }
}
```

Supported field types are `text`, `number`, `password`, `checkbox`, and
`select`. For `select`, include an `options` array. For secrets, send
`"secret": true` and omit or redact the current value.

## Poll And Apply

Poll every 2-5 seconds when dashboard settings are enabled:

```text
GET http://<dashboard-host>:8080/api/rovers/<device_id>/settings/pending
```

The response contains pending requests:

```json
{
  "device_id": "rover-alpha",
  "pending": [
    {
      "id": 7,
      "changes": [
        {
          "path": "blynk.broker",
          "label": "MQTT Broker",
          "type": "text",
          "value": "192.168.1.50"
        }
      ]
    }
  ]
}
```

For each change:

1. Confirm `path` exists in the shared serial settings definitions.
2. Coerce `value` using the same rules as the USB serial menu.
3. Run the same validation and setter as the USB serial menu.
4. Write `config.yaml` atomically.
5. Restart or reconnect only the affected subsystem.
6. Post an ack.

Ack success:

```text
POST /api/rovers/<device_id>/settings/ack
```

```json
{
  "request_id": 7,
  "status": "applied",
  "message": "Applied MQTT Broker"
}
```

Ack rejection/failure:

```json
{
  "request_id": 7,
  "status": "rejected",
  "message": "Unknown setting path: blynk.broker"
}
```

Valid statuses are `applied`, `rejected`, and `failed`.

## Serial-Style Logs

Send the same important lines the USB serial menu would show:

```text
POST /api/rovers/<device_id>/settings/logs
```

```json
{
  "level": "info",
  "message": "NTRIP Caster Host changed"
}
```

Use `info`, `warning`, `error`, `applied`, `rejected`, or the existing serial
log levels if the rover already has them.

## Safety Checklist

- Reject any path not present in the shared serial menu definitions.
- Reject nested objects and lists as setting values.
- Keep password/token values out of telemetry, logs, and ack messages.
- Apply one queued request at a time under a lock.
- Do not execute shell commands from dashboard values.
- Keep a backup of the previous config before writing.
- If a subsystem cannot hot-reload safely, write the config and ask for a manual
  restart in the ack message instead of forcing a risky restart.

## Existing Rover Config Note

This dashboard already supports local plain MQTT on port `1883`. The rover repo
also needs `blynk.useTls` exposed in its public config template so local MQTT can
disable TLS:

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

Then a local dashboard profile can set:

```yaml
blynk:
  broker: 192.168.1.50
  port: 1883
  useTls: false
```
