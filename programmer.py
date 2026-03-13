import json
import subprocess
import threading
import time
import argparse
import logging
import sys
from datetime import datetime, timedelta

# Configure logging interface
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)

# Global registry for sensor telemetry
sensor_last_occupied = {}

class MatterDevice:
    def __init__(self, config):
        self.node_id = config.get('node_id')
        self.name = config.get('name')
        self.hardware_type = config.get('hardware_type')
        self.events = config.get('events', {})

    def _run_script(self, event_name, *args):
        if event_name not in self.events:
            raise ValueError("Event is not defined for this device.")
        script = self.events[event_name]['script']
        cmd = ["python3", "-c", script] + [str(a) for a in args]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()

class LightDevice(MatterDevice):
    def turn_on(self): return self._run_script("turn_on")
    def turn_off(self): return self._run_script("turn_off")
    def set_level(self, level: int): return self._run_script("set_level", level)
    def read_level(self): return self._run_script("read_level")
    def set_color_temperature(self, mireds: int): return self._run_script("set_color_temperature", mireds)
    def read_color_temperature(self): return self._run_script("read_color_temperature")

class SensorDevice(MatterDevice):
    def read_occupancy(self):
        return self._run_script("read_occupancy")

    def subscribe_occupancy(self, callback_function):
        def run_stream():
            script = self.events.get("subscribe_occupancy", {}).get("script", "")
            if not script: return
            
            # Use '-u' flag to force unbuffered standard output
            process = subprocess.Popen(["python3", "-u", "-c", script], stdout=subprocess.PIPE, text=True)
            
            for line in process.stdout:
                if line.strip():
                    callback_function(line.strip())
                    
        listener_thread = threading.Thread(target=run_stream, daemon=True)
        listener_thread.start()
        return listener_thread

class MatterController:
    def __init__(self, json_path: str):
        with open(json_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
        self.devices = {}
        for dev_config in data.get('devices', []):
            hw_type = dev_config.get('hardware_type', '')
            if 'light' in hw_type:
                self.devices[dev_config['name']] = LightDevice(dev_config)
            elif 'sensor' in hw_type:
                self.devices[dev_config['name']] = SensorDevice(dev_config)

def time_to_minutes(time_str: str) -> int:
    hours, minutes = map(int, time_str.split(':'))
    return hours * 60 + minutes

def interpolate_value(current_time: int, t1: int, t2: int, v1: float, v2: float) -> float:
    if t1 == t2: return v1
    if t2 < t1:
        t2 += 1440
        if current_time < t1: current_time += 1440
    ratio = (current_time - t1) / (t2 - t1)
    return v1 + (v2 - v1) * ratio

def calculate_current_state(schedule: list, current_minutes: int) -> dict:
    sorted_sched = sorted(schedule, key=lambda x: time_to_minutes(x['time']))
    if not sorted_sched: return {"level": 0}
    if len(sorted_sched) == 1: return sorted_sched[0]

    prev_point = sorted_sched[-1]
    next_point = sorted_sched[0]
    for i in range(len(sorted_sched)):
        if time_to_minutes(sorted_sched[i]['time']) > current_minutes:
            next_point = sorted_sched[i]
            prev_point = sorted_sched[i-1] if i > 0 else sorted_sched[-1]
            break

    t1 = time_to_minutes(prev_point['time'])
    t2 = time_to_minutes(next_point['time'])

    target_state = {}
    target_state['level'] = interpolate_value(current_minutes, t1, t2, prev_point.get('level', 0), next_point.get('level', 0))
    if 'kelvin' in prev_point and 'kelvin' in next_point:
        target_state['kelvin'] = interpolate_value(current_minutes, t1, t2, prev_point['kelvin'], next_point['kelvin'])
    return target_state

def create_sensor_callback(sensor_id: str):
    # Closure to maintain sensor context and parse SSE JSON payload
    def callback(data_line):
        raw_data = str(data_line).strip()
        
        # Isolate payload by verifying and stripping the SSE prefix
        if not raw_data.startswith("data: "):
            return
            
        json_str = raw_data[6:].strip()
        
        try:
            # Parse the telemetry payload
            payload = json.loads(json_str)
            occupancy = payload.get("occupancy", 0)
            
            logging.info("Sensor Stream [" + sensor_id + "]: " + str(payload))
            
            # Update temporal state if motion is detected
            if occupancy == 1:
                sensor_last_occupied[sensor_id] = datetime.now()
                
        except json.JSONDecodeError:
            pass # Discard malformed packets silently
            
    return callback

def run_automation(data_path: str, config_path: str):
    controller = MatterController(data_path)
    with open(config_path, 'r', encoding='utf-8') as f:
        lighting_configs = json.load(f)

    device_map = {dev.node_id: dev for dev in controller.devices.values()}
    
    # Initialize asynchronous sensor subscriptions
    for dev in controller.devices.values():
        if isinstance(dev, SensorDevice):
            dev.subscribe_occupancy(create_sensor_callback(dev.node_id))

    # State cache to prevent redundant API calls
    state_cache = {}

    logging.info("Core system initialized. Entering 1Hz telemetry loop.")

    try:
        while True:
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute
            
            for config in lighting_configs:
                device_id = config['id']
                device = device_map.get(device_id)
                
                if not device or not hasattr(device, 'set_level'):
                    continue
                    
                # Base Schedule Evaluation
                schedule = config.get('schedule', [])
                target_state = calculate_current_state(schedule, current_minutes)
                
                # Sensor Override Evaluation
                sensors = config.get('sensor', [])
                is_occupied = False
                
                if not sensors:
                    is_occupied = True
                else:
                    for s in sensors:
                        s_id = s.get('id')
                        timeout_mins = s.get('timeout', 5)
                        last_trigger = sensor_last_occupied.get(s_id, datetime.min)
                        
                        if (now - last_trigger) <= timedelta(minutes=timeout_mins):
                            is_occupied = True
                            break

                # Final Command Resolution
                target_level = int(target_state.get('level', 0)) if is_occupied else 0
                target_on = target_level > 0
                
                # State Synchronization
                prev = state_cache.get(device_id, {'state': None, 'level': -1, 'kelvin': -1})
                
                if target_on:
                    matter_level = int((target_level / 100.0) * 254)
                    
                    if prev['state'] != 'ON':
                        logging.info("[" + device.name + "] Actuating: TURN ON")
                        device.turn_on()
                        prev['state'] = 'ON'
                        
                    if abs(prev['level'] - matter_level) >= 2:
                        logging.info("[" + device.name + "] Adjusting Level: " + str(matter_level))
                        device.set_level(matter_level)
                        prev['level'] = matter_level
                        
                    if 'kelvin' in target_state:
                        kelvin = int(target_state['kelvin'])
                        mireds = int(1000000 / kelvin) if kelvin > 0 else 250
                        
                        if abs(prev['kelvin'] - kelvin) > 50 and hasattr(device, 'set_color_temperature'):
                            logging.info("[" + device.name + "] Adjusting Temperature: " + str(kelvin) + "K")
                            device.set_color_temperature(mireds)
                            prev['kelvin'] = kelvin
                else:
                    if prev['state'] != 'OFF':
                        logging.info("[" + device.name + "] Actuating: TURN OFF")
                        device.turn_off()
                        prev['state'] = 'OFF'
                        prev['level'] = 0
                        
                state_cache[device_id] = prev
                
            time.sleep(1)
            
    except KeyboardInterrupt:
        logging.info("Execution terminated by user (SIGINT).")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Matter Automation Controller")
    parser.add_argument("--data", required=True, help="Path to logical bridge JSON")
    parser.add_argument("--config", required=True, help="Path to schedule JSON")
    args = parser.parse_args()
    
    try:
        run_automation(args.data, args.config)
    except KeyboardInterrupt:
        logging.info("System safely halted.")
        sys.exit(0)