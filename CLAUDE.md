# Home Lighting Programmer

## Project Overview

Smart home lighting automation system using Matter protocol. Controls lights based on time schedules and motion/presence sensors. Supports circadian rhythm-aware color temperature and brightness.

## Architecture

```
programmer.py  - Main automation loop (1Hz). Subscribes to sensor events (SSE), interpolates schedules, dispatches commands.
matter_lib.py  - Device abstraction: LightDevice, SensorDevice, MatterController. Talks to matter-web-controller API.
genconfig.py   - Auto-generates config JSON from hardware metadata at http://<IP>:<PORT>/api/metadata.
sample.json    - Real-world config example with 11 devices.
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
# 1. Generate config from hardware
python3 genconfig.py --ip <IP> --port <PORT> --out config.json

# 2. Run automation
python3 programmer.py --server <IP:PORT> --config config.json
```

## Dependencies

Pure Python 3 stdlib (no pip packages). Requires a running `matter-web-controller` instance.

## Code Conventions

- Device IDs: `dev_<name>` format (Matter node IDs from controller).
- Time format: `HH:MM` (24-hour).
- Brightness: 0-100 in config (mapped to 0-254 Matter scale internally).
- Color temperature: Kelvin in config (converted to mireds internally).
- Logging via Python `logging` module at INFO level.
