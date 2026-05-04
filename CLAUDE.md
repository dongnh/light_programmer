# Home Lighting Programmer

## Project Overview

Smart home lighting automation system using Matter protocol. Controls lights based on time schedules and motion/presence sensors. Supports circadian rhythm-aware color temperature and brightness.

Distributed as PyPI package `light-programmer`.

## Architecture

```
light_programmer/              # Python package
    __init__.py                # version
    matter_lib.py              # MatterClient (HTTP+X-API-Key), MatterDevice, LightDevice, SensorDevice,
                                 ClimateSensorDevice, ACDevice, MatterController.
                                 REST endpoints used: /api/level, /api/mired, /api/toggle, /api/ac,
                                 /api/climate, /api/subscribe (SSE).
                                 Device list fetched from /api/metadata or loaded from disk (json_path=).
                                 Devices accept an optional dispatcher (callable queue) for serialized execution.
    programmer.py              # Automation engine: CommandDispatcher, light + AC schedules,
                                 occupancy/climate gating, main 1Hz loop. Honors mode flags
                                 (auto/kill) loaded each tick from --mode-state JSON.
    mode_state.py              # Atomic JSON store for {auto, kill} flags shared across processes.
    mode_http.py               # Tiny stdlib HTTP server: GET/POST /mode, POST /kill.
                                 Consumed by the homekit-bridge (separate repo).
    genconfig.py               # Auto-generates config JSON from /api/metadata.
    mcp_server.py              # Optional MCP server (FastMCP). Tools for device discovery,
                                 climate/AC reads, config CRUD with validation, direct control,
                                 and mode-flag get/set.
                                 Installed via `pip install light-programmer[mcp]`,
                                 launched as `light-programmer-mcp` (stdio).
pyproject.toml                 # Package config, CLI entry points
sample.json                    # Real-world config example with 11 devices
```

## Key Concepts

- **Light schedule**: Array of `{time, level, kelvin}` points. Linearly interpolated. Cross-midnight supported.
- **AC entry**: `"type": "ac"` with fields:
  - `climate_sensor` (one ID) or `climate_sensors` (list). Omit both to use the AC's own
    thermostat (`local_temperature`). Multi-sensor uses any-trigger semantics: max temp /
    max humidity across sensors.
  - `mode` (`cool`/`heat`/`dry`/`fan`/`auto`), `setpoint` (°C).
  - Temperature hysteresis: cool/dry/fan/auto use `on_above` / `off_below`; heat uses
    `on_below` / `off_above`.
  - Optional humidity hysteresis: `humidity_above` / `humidity_below` (only fires when a
    configured sensor reports humidity).
  - Combined: ON if temperature OR humidity dim says on; OFF only when every configured
    dimension says off; otherwise holds previous state.
  - Optional `active_window: {start, end}` restricts operating hours; `sensor` /
    `sensor_condition` gate by occupancy. AC is forced OFF when occupancy fails or
    outside window. Missing climate reading on a dimension holds previous decision.
- **AC on-delay**: `on_delay_minutes` (default 5) — occupancy must be continuously satisfied for
  this many minutes before the AC is allowed to turn on. Resets if occupancy lapses. Once on,
  the AC stays on regardless of this delay (it only gates the transition off→on).
- **Sensor logic**: Two modes (apply to both lights and ACs):
  - Simple: `"sensor": [{id, timeout}]` — any sensor active = device enabled.
  - Advanced: `"sensor_condition"` — AST with `AND`, `OR`, `NOT`, `sensor`, `time_window` nodes.
- **CommandDispatcher**: Queues commands to avoid flooding the controller. Rate-limited background thread.
- **State caching**: Only sends commands when target differs from cached state (brightness ±2,
  color temp >50K threshold, AC setpoint ±0.5°C, AC mode change).
- **Mode flags** (v0.6.0+): two booleans persisted in `--mode-state` JSON file:
  - `auto` (default `true`): when `false`, schedule loop is paused and devices left as-is.
  - `kill` (default `false`): when `true`, every configured device is forced OFF once on
    transition, then loop stays paused. Clearing kill (or re-enabling auto) clears the
    state cache so the next tick reapplies fresh schedule values.
    While kill stays engaged, AC entries are re-asserted OFF every hour
    (`KILL_AC_REASSERT_INTERVAL`) to defend against IR-remote / external pokes.
  - HTTP API on `--mode-http-host:--mode-http-port` (default `127.0.0.1:7870`):
    `GET /mode`, `POST /mode {auto?, kill?}`, `POST /kill {kill}`.
  - MCP tools: `get_mode(state_path?)`, `set_mode(auto?, kill?, state_path?)`.

## Running

```bash
# Install
pip install -e .

# CLI commands (set MATTER_SRV_KEY env var or pass --api-key if the server requires auth)
light-genconfig --ip <IP> --port <PORT> --out config.json [--api-key <KEY>]
light-programmer --server <IP:PORT> --config config.json [--api-key <KEY>]
```

## Dependencies

Pure Python 3 stdlib (no pip packages). Requires a running `matter_webcontrol` instance
(https://github.com/dongnh/matter_webcontrol).

## Code Conventions

- Device IDs: `dev_<name>` format (Matter node IDs from controller).
- Time format: `HH:MM` (24-hour).
- Brightness: 0-100 in config (mapped to 0-254 Matter scale internally).
- Color temperature: Kelvin in config (converted to mireds internally).
- Logging via Python `logging` module at INFO level.
