# Light Programmer

Sensor-driven home lighting over Matter. One small daemon reads your motion and presence sensors, follows a schedule you write once, and sends only the commands that actually change something.

> For home use. Not hardened for commercial deployment.

## What it does

Light Programmer replaces wall switches with intent. You describe how each light should behave across the day — brightness, color temperature, which sensors matter — and the daemon keeps the room matching that description.

- **Circadian schedules.** Brightness and color temperature interpolate smoothly between the points you set.
- **Presence-aware.** Lights respond to motion and presence sensors, with per-sensor timeouts and boolean conditions when you need them.
- **Quiet on the wire.** Commands are sent only when the target differs from the current state, so the Matter fabric stays calm.
- **Mode flags.** An `auto` switch pauses the schedule; a `kill` switch turns everything off. Both are toggleable over HTTP for HomeKit integration.

Air-conditioning and climate logic now live in [matter\_webcontrol](https://github.com/dongnh/matter_webcontrol) and the HomeKit bridges. Light Programmer focuses on lights.

## Requirements

- Python 3.8 or later
- A reachable [matter\_webcontrol](https://github.com/dongnh/matter_webcontrol) instance
- Matter-compatible lights and occupancy sensors

## Install

```bash
pip install light-programmer
```

## Get started

```bash
# 1. Generate a starter config from your hardware.
light-genconfig --ip 192.168.1.220 --port 8080 --out config.json

# 2. Edit config.json to match how you want each room to behave.

# 3. Run.
light-programmer --server 192.168.1.220:8080 --config config.json
```

Set `MATTER_SRV_KEY` in the environment, or pass `--api-key`, if your matter\_webcontrol requires authentication.

## Configuration

A configuration is a JSON array. Each entry describes one light.

```jsonc
{
    "id": "dev_kitchen_sink",
    "note": "Sink area",
    "schedule": [
        { "time": "06:30", "level": 50,  "kelvin": 4000 },
        { "time": "12:00", "level": 100, "kelvin": 4000 },
        { "time": "21:30", "level": 100, "kelvin": 2700 }
    ],
    "sensor": [
        { "id": "kitchen_motion", "timeout": 5 }
    ]
}
```

- `level` is brightness, 0 to 100.
- `kelvin` is color temperature. Omit it for fixed-white lights.
- `timeout` is how many minutes to hold the light on after the sensor clears.
- Values between schedule points are linearly interpolated. Schedules wrap across midnight.

See [`sample.json`](sample.json) for a complete example.

## Sensor logic

The simple `sensor` array is enough for most rooms: any listed sensor being active enables the light. For everything else, use `sensor_condition` — a small expression tree.

| Node | Meaning |
|---|---|
| `sensor` | True while occupied, or within the configured `timeout` after clearing. |
| `time_window` | True when the current time falls between `start` and `end`. Cross-midnight is supported. |
| `AND`, `OR`, `NOT` | Boolean operators over child nodes. |

### At the desk, but not in bed

```json
{
    "sensor_condition": {
        "operator": "AND",
        "operands": [
            { "type": "sensor", "id": "desk_presence", "timeout": 15 },
            {
                "operator": "NOT",
                "operands": [
                    { "type": "sensor", "id": "bed_presence", "timeout": 5 }
                ]
            }
        ]
    }
}
```

### Schedule during the day, motion at night

```json
{
    "sensor_condition": {
        "operator": "OR",
        "operands": [
            { "type": "time_window", "start": "06:00", "end": "22:00" },
            { "type": "sensor", "id": "room_motion", "timeout": 5 }
        ]
    }
}
```

During the window, the light follows the schedule. Outside it, only motion brings it on.

## Mode flags

Two booleans control the daemon at runtime, stored together in a small JSON file passed via `--mode-state`:

- `auto` — when `false`, the schedule is paused and devices are left as they are.
- `kill` — when `true`, every configured device is turned off, then the loop stays paused until `kill` is cleared.

When `--mode-state` is set, an HTTP endpoint is exposed on `--mode-http-host:--mode-http-port` (default `127.0.0.1:7870`):

```
GET  /mode                 → { "auto": true, "kill": false }
POST /mode  { "auto": false }
POST /kill  { "kill": true }
```

This is the same surface the HomeKit bridge uses to expose Auto and Kill as Apple Home switches.

## MCP server

A bundled MCP server lets Claude and other agents discover your devices and edit the configuration directly.

```bash
pip install "light-programmer[mcp]"
light-programmer-mcp
```

Example entry for `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "light-programmer": {
      "command": "light-programmer-mcp",
      "env": { "MATTER_SRV_KEY": "your-api-key" }
    }
  }
}
```

Available tools:

| Tool | Purpose |
|---|---|
| `list_devices` | Enumerate lights, sensors, climate, and AC from `/api/metadata`. |
| `read_climate`, `read_ac_state`, `read_status` | Live readings from matter\_webcontrol. |
| `read_config`, `write_config`, `validate_config` | Whole-file edits with schema validation. |
| `upsert_entry`, `remove_entry` | Per-entry edits keyed by device id. |
| `set_light`, `set_ac` | Direct control for quick tests. |
| `get_mode`, `set_mode` | Read and write the auto/kill flags. |

The climate and AC tools are proxies to matter\_webcontrol. Light Programmer itself does not automate AC.

## Project layout

| File | Purpose |
|---|---|
| `light_programmer/programmer.py` | The 1 Hz automation loop. |
| `light_programmer/matter_lib.py` | Device wrappers and the matter\_webcontrol HTTP client. |
| `light_programmer/genconfig.py` | Generates a starter configuration from `/api/metadata`. |
| `light_programmer/mode_state.py`, `mode_http.py` | Auto/kill flags and the small HTTP API. |
| `light_programmer/mcp_server.py` | MCP tools for agent-driven configuration. |
| `sample.json` | A working multi-room example. |

## License

MIT
