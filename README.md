# Home Lighting Programmer
## Project Goals
This project provides ecosystem-agnostic control of smart home lighting. It eradicates the reliance on static physical switches, utilizing motion and presence sensors to achieve complete automation. The system dynamically supports human circadian rhythms to ensure optimal health and restorative sleep cycles for residents. Deployment is strictly restricted to indoor environments due to inherent security vulnerabilities.

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
### Configuration Structure
* Example:

```JSON
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
```

### Sensor Logic
- Continuously monitors occupancy status from defined sensor arrays.
- Records chronological timestamps upon occupancy detection.
- Executes automatic shut-off protocols if elapsed time exceeds the defined timeout threshold.
- Utilizes event-driven status updates to guarantee immediate functional responsiveness.

### Schedule and Interpolation Logic
- Applies linear interpolation during active states to calculate precise brightness and color temperature between the nearest scheduled time markers.
- Ensures seamless lighting transitions to optimize circadian rhythm alignment without abrupt visual shifts.
- Updates state values at strict one-minute intervals.