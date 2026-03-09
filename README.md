# Home Lighting Programmer

To optimize the spatial user experience, home lighting system requires distinct programming logic for each lighting component. 
* **Decoration Lighting** employs static color and intensity parameters to establish a consistent visual baseline. 
* **Utility Lighting** demands flawless, low latency interoperability with motion and presence sensors to ensure immediate functional responsiveness. 
* **Ambient Lighting** utilizes dynamic color temperature and intensity shifts to simulate natural daylight and warm evening firelight, fostering a deeper connection to human circadian rhythms.

## Configuration

* Example:
  ```json
  {
    "id": "dev_kitchen_sink",
    "note": "Sink area light, always active upon kitchen occupancy",
    "schedule": [
      {
        "time": "01:00",
        "level": 0,
        "kelvin": 2700
      },
      {
        "time": "06:00",
        "level": 0,
        "kelvin": 2700
      },
      {
        "time": "06:30",
        "level": 50,
        "kelvin": 4000
      },
      {
        "time": "08:30",
        "level": 100,
        "kelvin": 4000
      },
      {
        "time": "12:30",
        "level": 100,
        "kelvin": 4000
      },
      {
        "time": "18:30",
        "level": 100,
        "kelvin": 3000
      },
      {
        "time": "21:30",
        "level": 100,
        "kelvin": 2700
      }
    ],
    "sensor": [
      {
        "id": "kitchen_motion",
        "timeout": 300
      },
      {
        "id": "kitchen_presence",
        "timeout": 600
      }
    ]
  }
  ```

* Descripion:
  
1. Sensor Logic

- The system continuously monitors the occupancy status from the sensors (motion_sensor_01 and presence_sensor_01).

- Upon detecting motion or presence (occupancy = 1), the system records the last trigger time.

- Based on the timeout field (300 seconds for motion or 600 seconds for presence), if the elapsed time since the last occupancy = 1 instance exceeds the timeout duration, the lights will execute an automatic shut-off.

- If the elapsed time remains within the timeout window, the system maintains the active lighting state based on the schedule.
  
- Sensor devices utilize an event-driven mechanism for status updates to guarantee low latency.

2. Schedule and Interpolation Logic

- When the lights are in an active state, the system applies linear interpolation to accurately calculate the brightness level and color temperature at the current exact time, based on the two nearest time markers in the schedule.

- Example: Between the 06:30 (level 50, kelvin 4000) and 08:30 (level 100, kelvin 4000) markers. If the sensor is triggered at 07:30 (the exact midpoint of the duration), the system will interpolate a level value of 75 and a kelvin value of 4000.

- This mechanism ensures a seamless transition in lighting, optimizing for the user's circadian rhythm rather than executing an abrupt transition.
  
- Brightness levels and color temperature will be updated at one-minute intervals.
  
