# Home Lighting Programmer
## Project Goals
- This project provides ecosystem-agnostic control of smart home lighting. 
- It eradicates the reliance on static physical switches, utilizing motion and presence sensors to achieve complete automation. 
- The system dynamically supports human circadian rhythms to ensure optimal health and restorative sleep cycles for residents. 
- Deployment is strictly restricted to domestic environments due to inherent security vulnerabilities.

## Lighting Logic
To optimize spatial user experience, the system applies distinct programming logic:
* Decoration Lighting: Maintains static color and intensity for a consistent visual baseline.
* Utility Lighting: Requires low-latency sensor interoperability for immediate functional responsiveness.
* Ambient Lighting: Employs dynamic color temperature and intensity shifts to simulate natural daylight progression.

## System Requirements
Implementation necessitates the installation and active operation of the `matter-web-controller`. Consequently, the operational scripts require command-line parameters directing them to the active server instance of the controller.

## System Components
### 1. genconfig.py (Configuration Generator)
Retrieves hardware metadata from the controller API to synthesize a JSON configuration file. It systematically identifies network nodes and evaluates color control clusters to allocate appropriate color temperature parameters.
* Usage:
```Bash
python3 genconfig.py --ip <SERVER_IP> --port <PORT> --out <OUTPUT_FILE>
```
### 2. programmer.py (Automation Controller)
The central processor maintaining a 1Hz telemetry loop. It subscribes to Server-Sent Events from the matter-web-controller for real-time sensor processing and utilizes linear interpolation to calculate device states, ensuring minimal latency and seamless environmental transitions.
* Usage:
```Bash
python3 programmer.py --server <IP:PORT> --config <CONFIG_FILE>
```

### Sensor Logic
- Continuously monitors occupancy status from defined sensor arrays.
- Records chronological timestamps upon occupancy detection.
- Executes automatic shut-off protocols if elapsed time exceeds the defined timeout threshold.
- Utilizes event-driven status updates to guarantee immediate functional responsiveness.

* Example:
```JSON
[
    {
        "id": "dev_kitchen_sink",
        "note": "Sink area light, active upon occupancy",
        "schedule": [
            { "time": "06:30", "level": 50, "kelvin": 4000 },
            { "time": "12:30", "level": 100, "kelvin": 4000 },
            { "time": "21:30", "level": 100, "kelvin": 2700 }
        ],
        "sensor": [
            { "id": "kitchen_motion", "timeout": 5 }
        ]
    }
]
```

### Optional Advanced Sensor Logic
* To support complex automation topologies, the system implements an Abstract Syntax Tree (AST) evaluation model via the `sensor_condition` object. This architecture supersedes the legacy flat `sensor` array, allowing for recursive boolean logic combinations and temporal constraints.

* Supported node types and logical operators:
    - `AND` (Logical Conjunction): Evaluates to true if all operand nodes evaluate to true.
    - `OR` (Logical Disjunction): Evaluates to true if at least one operand node evaluates to true.
    - `NOT` (Logical Negation): Inverts the boolean state of its primary operand node.
    - `sensor`: Evaluates physical hardware telemetry based on real-time occupancy state and timeout duration.
    - `time_window`: Evaluates whether the current system time falls within a strictly defined `start` and `end` interval (supports cross-midnight intervals).

* Example:
    - This configuration activates the workspace light only when the desk sensor detects occupancy AND the bed sensor does NOT detect occupancy.

    ```JSON
    [
        {
        "id": "dev_workspace_light",
        "note": "Workspace light with strict conditional constraints",
        "schedule": [
            { "time": "08:00", "level": 100, "kelvin": 5000 },
            { "time": "22:00", "level": 50, "kelvin": 3000 }
        ],
        "sensor_condition": {
            "operator": "AND",
            "operands": [
                {"type": "sensor", "id": "desk_presence", "timeout": 15},
                {
                    "operator": "NOT",
                    "operands": [
                        {"type": "sensor", "id": "bed_presence", "timeout": 5}
                    ]
                }
            ]
        }
        }
    ]
    ```
    - This configuration strictly enforces the schedule from 06:00 to 22:00. Outside of this designated time window (from 22:00 to 06:00), the device relies exclusively on logical disjunction with the physical sensor to trigger activation.

    ```json
    {
        "id": "dev_workspace_light",
        "note": "Maintains schedule during day, sensor-driven at night",
        "schedule": [
            { "time": "06:00", "level": 100, "kelvin": 4000 },
            { "time": "22:00", "level": 10, "kelvin": 2700 }
        ],
        "sensor_condition": {
            "operator": "OR",
            "operands": [
                {
                    "type": "time_window",
                    "start": "06:00",
                    "end": "22:00"
                },
                {
                    "type": "sensor",
                    "id": "dev_1_3",
                    "timeout": 5
                }
            ]
        }
    }
    ```

### Schedule and Interpolation Logic
- Applies linear interpolation during active states to calculate precise brightness and color temperature between the nearest scheduled time markers.
- Ensures seamless lighting transitions to optimize circadian rhythm alignment without abrupt visual shifts.
- Updates state values at strict one-minute intervals.