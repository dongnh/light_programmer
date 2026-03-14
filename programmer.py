import json
import subprocess
import threading
import time
import argparse
import logging
import sys
import urllib.request
import queue
from datetime import datetime, timedelta

# Configure logging interface
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)

# Global registry for sensor telemetry and state tracking
sensor_registry = {}
# Event trigger for instant state evaluation
state_changed_event = threading.Event()

class CommandDispatcher:
    # Changed default rate limit to 0.0 for instant execution
    def __init__(self, rate_limit_delay: float = 0.0):
        self.cmd_queue = queue.Queue()
        self.rate_limit_delay = rate_limit_delay
        
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()

    def _process_queue(self):
        while True:
            cmd = self.cmd_queue.get()
            if cmd is None:
                break
            try:
                subprocess.run(cmd, capture_output=True, text=True)
            except Exception as e:
                logging.error("Command execution failed: " + str(e))
            finally:
                self.cmd_queue.task_done()
            
            if self.rate_limit_delay > 0:
                time.sleep(self.rate_limit_delay)

    def enqueue_command(self, cmd: list):
        self.cmd_queue.put(cmd)

dispatcher = CommandDispatcher(rate_limit_delay=0.0)

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
        
        dispatcher.enqueue_command(cmd)
        return "Command queued"

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
            
            process = subprocess.Popen(["python3", "-u", "-c", script], stdout=subprocess.PIPE, text=True)
            
            for line in process.stdout:
                if line.strip():
                    callback_function(line.strip())
                    
        listener_thread = threading.Thread(target=run_stream, daemon=True)
        listener_thread.start()
        return listener_thread

class MatterController:
    def __init__(self, server_address: str):
        url = f"http://{server_address}/api/metadata"
        logging.info("Fetching hardware metadata from: " + url)
        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode('utf-8'))
        except Exception as e:
            logging.error("Failed to retrieve metadata: " + str(e))
            sys.exit(1)

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
    def callback(data_line):
        raw_data = str(data_line).strip()
        
        if not raw_data.startswith("data: "):
            return
            
        json_str = raw_data[6:].strip()
        
        try:
            payload = json.loads(json_str)
            occupancy = payload.get("occupancy", 0)
            
            logging.info("Sensor Stream [" + sensor_id + "]: " + str(payload))
            
            current_state = sensor_registry.get(sensor_id, {"is_occupied": False, "last_cleared": datetime.min})
            
            state_mutated = False
            if occupancy == 1 and not current_state["is_occupied"]:
                current_state["is_occupied"] = True
                state_mutated = True
            elif occupancy == 0 and current_state["is_occupied"]:
                current_state["is_occupied"] = False
                current_state["last_cleared"] = datetime.now()
                state_mutated = True
            elif occupancy == 0 and sensor_id not in sensor_registry:
                current_state["is_occupied"] = False
                current_state["last_cleared"] = datetime.min
                
            sensor_registry[sensor_id] = current_state
            
            # Trigger immediate main loop execution if state changes
            if state_mutated:
                state_changed_event.set()
                
        except json.JSONDecodeError:
            pass
            
    return callback

def run_automation(server: str, config_path: str):
    controller = MatterController(server)
    with open(config_path, 'r', encoding='utf-8') as f:
        lighting_configs = json.load(f)

    device_map = {dev.node_id: dev for dev in controller.devices.values()}
    
    for dev in controller.devices.values():
        if isinstance(dev, SensorDevice):
            dev.subscribe_occupancy(create_sensor_callback(dev.node_id))

    state_cache = {}
    logging.info("Core system initialized. Entering event-driven telemetry loop.")

    try:
        while True:
            # Wait up to 1 second, or execute immediately if event is triggered
            state_changed_event.wait(timeout=1.0)
            state_changed_event.clear()

            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute
            
            for config in lighting_configs:
                device_id = config['id']
                device = device_map.get(device_id)
                
                if not device or not hasattr(device, 'set_level'):
                    continue
                    
                schedule = config.get('schedule', [])
                target_state = calculate_current_state(schedule, current_minutes)
                
                sensors = config.get('sensor', [])
                is_occupied = False
                
                if not sensors:
                    is_occupied = True
                else:
                    for s in sensors:
                        s_id = s.get('id')
                        timeout_mins = s.get('timeout', 5)
                        
                        sensor_data = sensor_registry.get(s_id, {"is_occupied": False, "last_cleared": datetime.min})
                        
                        if sensor_data["is_occupied"]:
                            is_occupied = True
                            break
                        else:
                            last_cleared = sensor_data["last_cleared"]
                            if (now - last_cleared) <= timedelta(minutes=timeout_mins):
                                is_occupied = True
                                break

                target_level = int(target_state.get('level', 0)) if is_occupied else 0
                target_on = target_level > 0
                
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
            
    except KeyboardInterrupt:
        logging.info("Execution terminated by user (SIGINT).")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Matter Automation Controller")
    parser.add_argument("--server", required=True, help="Server IP and port (e.g., 192.168.1.220:8080)")
    parser.add_argument("--config", required=True, help="Path to schedule JSON")
    args = parser.parse_args()
    
    try:
        run_automation(args.server, args.config)
    except KeyboardInterrupt:
        logging.info("System safely halted.")
        sys.exit(0)