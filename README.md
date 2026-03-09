# Home Lighting Programmer

To optimize spatial user experience, home lighting systems require distinct programming logic:

- Decoration Lighting: Uses static color and intensity for a consistent visual baseline.

- Utility Lighting: Requires low-latency sensor interoperability for immediate functional responsiveness.

- Ambient Lighting: Uses dynamic color temperature and intensity shifts to simulate natural light, aligning with human circadian rhythms.

## Configuration

* Example:
  ```json
  {
    "id": "dev_kitchen_sink",
    "note": "Sink area light, always active upon kitchen occupancy",
    "schedule": [
        { "time": "01:00", "level": 0, "kelvin": 2700 },
        { "time": "06:00", "level": 0, "kelvin": 2700 },
        { "time": "06:30", "level": 50, "kelvin": 4000 },
        { "time": "08:30", "level": 100, "kelvin": 4000 },
        { "time": "12:30", "level": 100, "kelvin": 4000 },
        { "time": "18:30", "level": 100, "kelvin": 3000 },
        { "time": "21:30", "level": 100, "kelvin": 2700 }
    ],
    "sensor": [
        { "id": "kitchen_motion", "timeout": 5 },
        { "id": "kitchen_presence", "timeout": 0 }
    ]
  }
  ```

* Descripion:
  
  * Sensor Logic
  
    - Continuously monitors occupancy status from configured sensors.

    - Records the timestamp upon detecting occupancy (occupancy = 1).

    - Executes an automatic shut-off if the elapsed time since the last trigger exceeds the timeout duration (e.g., 5 minutes for motion, 0 minutes for presence).

    - Maintains the scheduled active state while within the timeout window.

    - Utilizes event-driven status updates to guarantee low latency.
  
  * Schedule and Interpolation Logic
  
    - During active states, the system applies linear interpolation to calculate precise brightness and color temperature based on the two nearest scheduled time markers.

    - Example: At 07:30, halfway between 06:30 (level 50, 4000K) and 08:30 (level 100, 4000K), the interpolated level is 75 at 4000K.

    - Ensures seamless lighting transitions, optimizing circadian rhythm alignment without abrupt visual shifts.

    - Brightness and color temperature values update at one-minute intervals.
  
