# Home Lighting Programmer

Ecosystem-agnostic smart home lighting automation using the Matter protocol. Replaces physical switches with sensor-driven control and supports circadian rhythm alignment through dynamic color temperature and brightness scheduling.

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

## Quick Start

```bash
# Step 1: Auto-generate config from your hardware
python3 genconfig.py --ip 192.168.1.220 --port 8080 --out config.json

# Step 2: Edit config.json to customize schedules and sensor logic

# Step 3: Run
python3 programmer.py --server 192.168.1.220:8080 --config config.json
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

## Files

| File | Purpose |
|------|---------|
| `programmer.py` | Main automation controller - runs the 1Hz loop |
| `matter_lib.py` | Device abstraction layer (lights, sensors, controller) |
| `genconfig.py` | Generates config JSON from hardware discovery |
| `sample.json` | Example configuration with 11 devices |

## License

MIT
