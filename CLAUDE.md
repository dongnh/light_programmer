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
    programmer.py              # Automation engine: CommandDispatcher, light schedules,
                                 occupancy gating, main 1Hz loop. Honors mode flags
                                 (auto/kill) loaded each tick from --mode-state JSON.
                                 AC control was removed in v0.7.0; AC/climate APIs of
                                 matter_webcontrol are still wrapped by matter_lib and
                                 exposed via the MCP server, but the schedule loop
                                 never reads climate sensors or writes /api/ac.
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
- **AC entries removed (v0.7.0)**: `"type": "ac"` entries in config are ignored by the schedule
  loop. AC/climate control now lives entirely in matter_webcontrol + the HomeKit bridges. The
  MCP server still exposes read/write tools for AC and climate as a thin proxy to matter_webcontrol.
- **Sensor logic**: Two modes for light entries:
  - Simple: `"sensor": [{id, timeout}]` — any sensor active = device enabled.
  - Advanced: `"sensor_condition"` — AST with `AND`, `OR`, `NOT`, `sensor`, `time_window` nodes.
- **Rain override** (optional `"rain"` block on a light entry): while a rain sensor is active
  AND the light is on, overlay rain-time values onto the scheduled state. Does not turn the
  light on/off — only recolors/dims an already-on device (e.g. an artificial skylight going
  overcast). Shape: `"rain": {sensor|sensor_condition, kelvin?, level?|level_scale?}`.
  - Brightness precedence: `intensity_scale` (multiply the SCHEDULED level by a per-intensity
    factor, e.g. `{"light":0.85,"moderate":0.65,"heavy":0.45,"violent":0.25}` — blends rain
    with the circadian schedule, recommended) → `intensity_level` (per-intensity absolute,
    ignores schedule) → `level` (single absolute) → `level_scale` (single multiplier).
    Color temp: `intensity_kelvin` map → `kelvin`.
    With no sensor it never triggers. The rain sensor (from `matter-weather-sensor`) uses the
    dedicated `rain_state: "rain"` key (Matter Rain Sensor 0x0044) and streams `rain_intensity`
    in its SSE; the callback reads `rain`/`occupancy` (binary) and stores the latest intensity,
    so the override dims an artificial skylight in step with how hard it's raining.
  - **Colour-flow effect (v0.12.0)**: a rain entry with `"effect": "flow"` offloads an
    on-device flicker animation to the Yeelight bridge instead of the 1Hz static-level control.
    While raining + on, the loop POSTs `/api/flow {id, base, peak, kelvin, lightning}` to the
    Yeelight bridge (`--yeelight-server`); `base` = scheduled level × `intensity_scale`, `peak`
    = full scheduled level, `lightning` true when `intensity` ∈ `flash_levels` (default
    `["violent"]`). While flow is active the bulb is owned by the bridge — static level/temp
    control is skipped and the state cache is poisoned (`level=-1`) so it re-applies on stop.
    When rain clears / light turns off / effect is removed, the loop POSTs `/api/flow/stop`.
    Flow is best-effort: bridge errors are logged and never block the loop. See
    `_flow_post`/`_flow_changed` in [programmer.py](light_programmer/programmer.py:31).
- **CommandDispatcher**: Queues commands to avoid flooding the controller. Rate-limited background thread.
- **State caching**: Only sends commands when target differs from cached state (brightness ±2,
  color temp >50K threshold).
- **Mode flags** (v0.6.0+): two booleans persisted in `--mode-state` JSON file:
  - `auto` (default `true`): when `false`, schedule loop is paused and devices left as-is.
  - `kill` (default `false`): when `true`, every configured device is forced OFF once on
    transition, then loop stays paused. Clearing kill (or re-enabling auto) clears the
    state cache so the next tick reapplies fresh schedule values. Kill only acts on lights
    (and other non-AC devices); it does not touch AC/IR units.
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

`light-programmer` flags (see [programmer.py](light_programmer/programmer.py:489)):
- `--server IP:PORT` (required), `--config PATH` (required), `--api-key` / `MATTER_SRV_KEY`.
- `--mode-state PATH` / `LP_MODE_STATE` — enables the auto/kill flags + `/mode` HTTP endpoint.
  Without it, mode flags and HomeKit-bridge integration are off.
- `--mode-http-host` (default `127.0.0.1`; use `0.0.0.0` for LAN) / `--mode-http-port` (7870).
- `--yeelight-server IP:PORT` / `LP_YEELIGHT_SERVER` and `--yeelight-api-key` / `LP_YEELIGHT_KEY`
  — only needed when a config uses rain `"effect": "flow"`. Optional.

## Dependencies

Pure Python 3 stdlib (no pip packages). Requires a running `matter_webcontrol` instance
(https://github.com/dongnh/matter_webcontrol).

## Code Conventions

- Device IDs: `dev_<name>` format (Matter node IDs from controller).
- Time format: `HH:MM` (24-hour).
- Brightness: 0-100 in config (mapped to 0-254 Matter scale internally).
- Color temperature: Kelvin in config (converted to mireds internally).
- Logging via Python `logging` module at INFO level.

## Development & Release

- **Version source of truth**: [`light_programmer/__init__.py`](light_programmer/__init__.py)
  (`__version__`). Bump it on every user-facing change. ⚠️ `pyproject.toml`'s `version`
  currently lags (`0.7.0` while `__init__` is past `0.12.0`) — keep both in sync when releasing.
- **Commit convention**: one commit per release, subject `vX.Y.Z: <imperative summary>` (e.g.
  `v0.12.0: rain "effect: flow" drives Yeelight on-device colour-flow animation`). MINOR for
  new behaviour, PATCH for fixes. Match the existing `git log` style.
- **No automated test suite.** `tests_local/` holds ad-hoc scripts (`identify_sensors.py`) +
  fixture data (`home.json`), not pytest. The `.venv-matter` / `.venv-test` / `.venv-yee`
  dirs are scratch environments for poking the live controller — don't treat them as the
  project venv and don't commit them (they're gitignored). Validate changes by reasoning +
  running `light-programmer` against a real/local `matter_webcontrol`.
- **Config has no formal schema**: `load_config`/`run_automation` parse JSON directly; a typo'd
  key is silently ignored rather than rejected. Cross-check new config keys against the actual
  reader in [programmer.py](light_programmer/programmer.py) before assuming they take effect.

## Deployment & Ecosystem

- **Production host**: runs as a launchd service on `panda@192.168.1.220` (key-based SSH set up).
  See the auto-memory entries (`light_programmer_deployment`, `homekit_bridge_deployment`) for
  venv path, launchd labels, and config location before touching the live install.
- **Required runtime peer**: a live [`matter_webcontrol`](https://github.com/dongnh/matter_webcontrol)
  instance. This package only talks to its REST/SSE API; it never speaks Matter directly.
- **Surrounding stack** (separate repos, push to `github.com/dongnh`): `matter-weather-sensor`
  (rain/illuminance sensors feeding the rain override), the Yeelight bridge (colour-flow target),
  `homekit-bridge` (consumes `/mode` for LP Auto / LP Kill switches), and the
  `matter-homekit-bridge` AC/heater stack. Light_programmer is the schedule brain; these are
  its sensors and actuators.
