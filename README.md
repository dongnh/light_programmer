# Home Lighting Programmer

Ecosystem-agnostic smart home automation using the Matter protocol. Replaces physical switches with sensor-driven control of **lights** (circadian-aware brightness + color temperature schedules) and **air conditioners** (temperature-driven hysteresis with occupancy gating).

> **Note:** For domestic use only. Not hardened for production/commercial deployment.

## How It Works

The system runs a 1Hz loop that:
1. Reads real-time sensor data (motion/presence) via Server-Sent Events
2. Interpolates the configured schedule to determine target brightness and color temperature
3. Sends commands only when the target state changes

Three lighting modes are supported:
- **Decoration** - Static color and intensity
- **Utility** - Sensor-triggered, low-latency response
- **Ambient** - Time-based color temperature and brightness that follows natural daylight

## Requirements

- Python 3
- A running [`matter-web-controller`](https://github.com/dongnh/matter_webcontrol) instance
- Matter-compatible lights and sensors

## Installation

```bash
pip install light-programmer
```

## Quick Start

```bash
# Step 1: Auto-generate config from your hardware
light-genconfig --ip 192.168.1.220 --port 8080 --out config.json

# Step 2: Edit config.json to customize schedules and sensor logic

# Step 3: Run
light-programmer --server 192.168.1.220:8080 --config config.json
```

## Configuration

Each device entry in the config JSON has:

```jsonc
{
    "id": "dev_kitchen_sink",       // Matter node ID
    "note": "Sink area light",      // Human-readable description
    "schedule": [                   // Time-based control points
        { "time": "06:30", "level": 50,  "kelvin": 4000 },
        { "time": "12:00", "level": 100, "kelvin": 4000 },
        { "time": "21:30", "level": 100, "kelvin": 2700 }
    ],
    "sensor": [                     // Simple sensor trigger
        { "id": "kitchen_motion", "timeout": 5 }
    ]
}
```

- `level`: Brightness 0-100%
- `kelvin`: Color temperature 2700-6500K (omit for non-color lights)
- `timeout`: Seconds to keep light on after sensor clears
- Values between schedule points are linearly interpolated

See [`sample.json`](sample.json) for a full working example.

## Advanced Sensor Logic

For complex scenarios, use `sensor_condition` instead of `sensor`. It supports a tree of boolean operators:

| Node Type     | Description |
|---------------|-------------|
| `sensor`      | `true` if occupied or within timeout |
| `time_window` | `true` if current time is between `start` and `end` (cross-midnight supported) |
| `AND`         | All operands must be `true` |
| `OR`          | At least one operand must be `true` |
| `NOT`         | Inverts its operand |

### Example: Light on only when at desk AND not in bed

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

### Example: Follow schedule during day, sensor-only at night

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

During 06:00-22:00 the light follows its schedule regardless of sensors. Outside that window, it only turns on when the sensor detects motion.

## Air Conditioner Control

AC entries are temperature-driven (no time schedule). They use a Matter thermostat (`/api/ac`) plus a separate climate sensor (`/api/climate`) for the ambient reading.

```jsonc
{
    "id": "dev_ac_livingroom",
    "type": "ac",                          // marks this entry as an AC
    "climate_sensor": "dev_temp_livingroom",
    "mode": "cool",                        // cool / heat / dry / fan / auto
    "setpoint": 26.0,                      // °C sent to the thermostat
    "on_above": 29.0,                      // turn on when ambient ≥ 29 °C
    "off_below": 26.5,                     // turn off when ambient ≤ 26.5 °C
    "on_delay_minutes": 5,                 // require continuous occupancy ≥ 5 min before turning on
    "active_window": {"start": "10:00", "end": "23:30"},
    "sensor": [
        {"id": "dev_occ_livingroom", "timeout": 15}
    ]
}
```

**Bring-up rule** (off → on): ambient ≥ `on_above` **AND** occupancy continuously satisfied for `on_delay_minutes` **AND** time within `active_window`.

**Bring-down rule** (on → off): ambient ≤ `off_below`, **OR** occupancy fails (after each sensor's `timeout` hold), **OR** outside `active_window`.

In the dead band between `off_below` and `on_above` the AC holds its previous state — this is the hysteresis. For `mode: "heat"`, swap to `on_below` / `off_above` (turn on when cold enough, off when warm enough).

The same `sensor` / `sensor_condition` AST used by lights applies — combine multiple occupancy sensors with `AND`/`OR`/`NOT` as needed. If the climate sensor is unreachable, the AC holds its last decision rather than flapping.

## Project Structure

| File | Purpose |
|------|---------|
| `light_programmer/programmer.py` | Main automation controller — runs the 1Hz loop for lights and ACs |
| `light_programmer/matter_lib.py` | Device abstractions: `LightDevice`, `SensorDevice`, `ClimateSensorDevice`, `ACDevice` |
| `light_programmer/genconfig.py` | Generates config JSON from hardware discovery |
| `sample.json` | Example configuration with 11 devices |
| `pyproject.toml` | Package configuration and CLI entry points |

## License

MIT
