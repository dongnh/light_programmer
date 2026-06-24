# Light Programmer

Sensor-driven home lighting over Matter. Built for home use, not commercial deployment.

## What it does

One daemon keeps your lights matching how each room should feel across the day. You describe brightness, colour temperature, and which sensors matter; a 1 Hz loop interpolates each light's circadian schedule, gates it on occupancy, and sends a command only when the target meaningfully changes. It talks to [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) over HTTP/SSE — never Matter directly.

## Features

- **Circadian schedules** — `{time, level, kelvin}` points, linearly interpolated, cross-midnight aware.
- **Occupancy gating** — a flat `sensor` list, or a boolean `sensor_condition` tree (`AND`/`OR`/`NOT`/time windows).
- **Moonlight** — a level between 0 and 1 drives the bulb's warm night-light channel via the Yeelight bridge. Opt-in: only a deliberate sub-one setpoint triggers it, never the ramp up to daylight.
- **`unoccupied` fallback** — an away-state window and level instead of plain off (e.g. an evening glow, dark overnight).
- **Rain override** — weather scales an already-on light's brightness and colour; `effect: "flow"` hands the bulb to the Yeelight bridge for an on-device flicker until it clears.
- **Mode flags + `/lights`** — `auto` (pause) and `kill` (force off) over a small HTTP API, with optional `X-API-Key`. `/lights` reports per-light reachability for the HomeKit bridge.
- **MCP server** (`mcp` extra) — agents enumerate devices, edit the config with validation, drive lights, and toggle modes. A non-loopback bind requires a bearer token; destructive tools are opt-in.

AC and climate are out of scope here — handled by matter_webcontrol and the HomeKit bridges. The MCP server only proxies their read/write APIs.

## Install

```
pip install light-programmer        # add the [mcp] extra for the MCP server
```

Requires Python 3.8+ and a reachable matter_webcontrol. `light-genconfig` scaffolds a starter config from your controller's `/api/metadata`.

## Configure

A JSON array, one entry per light: an `id`, an optional `name` (the label the HomeKit bridge shows), a `schedule`, and either a `sensor` list or a `sensor_condition`. `rain` and `unoccupied` are optional. See [`sample.json`](sample.json) for a multi-room example and [`programmer.py`](light_programmer/programmer.py) for the authoritative reader — the schema is informal and unknown keys are ignored.

## Related projects

Part of a Matter home under [github.com/dongnh](https://github.com/dongnh):

- **matter_webcontrol** — required peer; speaks Matter so this daemon doesn't.
- **matter-weather-sensor** — rain, illuminance, and climate as Matter sensors; feeds the rain override.
- **yeelight_webcontrol** — target for the `flow` effect and the moonlight channel.
- **light-programmer-homekit** — turns `/lights` into per-light Apple Home reachability sensors.
- **matter-homekit-bridge** — brings HomeKit-only Aqara AC and heaters into the fabric.

## License

MIT
