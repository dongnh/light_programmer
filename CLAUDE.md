# Home Lighting Programmer

## Project Overview

Smart home lighting automation system using Matter protocol. Controls lights based on time schedules and motion/presence sensors. Supports circadian rhythm-aware color temperature and brightness.

Distributed as PyPI package `light-programmer`.

## Architecture

```
light_programmer/              # Python package
    __init__.py                # version
    matter_lib.py              # MatterClient (HTTP+X-API-Key), MatterDevice, LightDevice, SensorDevice, MatterController.
                                 Talks REST to matter_webcontrol: /api/level, /api/mired, /api/toggle, /api/subscribe (SSE).
                                 Device list fetched from /api/metadata (server_address=) or loaded from disk (json_path=).
                                 Devices accept an optional dispatcher (callable queue) for serialized execution.
    programmer.py              # Automation engine: CommandDispatcher (callable queue), scheduling, sensor logic, main 1Hz loop.
                                 Imports device classes from matter_lib.
    genconfig.py               # Auto-generates config JSON from /api/metadata.
pyproject.toml                 # Package config, CLI entry points
sample.json                    # Real-world config example with 11 devices
```

## Key Concepts

- **Schedule**: Array of `{time, level, kelvin}` points. Values are linearly interpolated between points. Cross-midnight supported.
- **Sensor logic**: Two modes:
  - Simple: `"sensor": [{id, timeout}]` — any sensor active = light on.
  - Advanced: `"sensor_condition"` — AST with `AND`, `OR`, `NOT`, `sensor`, `time_window` nodes.
- **CommandDispatcher**: Queues commands to avoid flooding the controller. Rate-limited background thread.
- **State caching**: Only sends commands when target differs from cached state (brightness ±2, color temp >50K threshold).

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
