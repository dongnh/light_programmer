import json
import threading
import time
import argparse
import logging
import sys
import subprocess
import queue
from datetime import datetime, timedelta

from matter_lib import MatterController, SensorDevice

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)

# Global sensor state and event trigger
sensor_registry = {}
state_changed_event = threading.Event()


class CommandDispatcher:
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


# --- Scheduling ---

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


# --- Sensor Logic ---

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

            if state_mutated:
                state_changed_event.set()
        except json.JSONDecodeError:
            pass

    return callback

def evaluate_condition(node: dict, registry: dict, current_time: datetime) -> bool:
    if not node:
        return True

    node_type = node.get('type')

    if node_type == 'sensor':
        s_id = node.get('id')
        timeout_mins = node.get('timeout', 5)
        sensor_data = registry.get(s_id, {"is_occupied": False, "last_cleared": datetime.min})

        if sensor_data["is_occupied"]:
            return True
        if (current_time - sensor_data["last_cleared"]) <= timedelta(minutes=timeout_mins):
            return True
        return False

    if node_type == 'time_window':
        start_str = node.get('start', '00:00')
        end_str = node.get('end', '23:59')
        current_mins = current_time.hour * 60 + current_time.minute
        start_mins = time_to_minutes(start_str)
        end_mins = time_to_minutes(end_str)

        if start_mins <= end_mins:
            return start_mins <= current_mins < end_mins
        else:
            return current_mins >= start_mins or current_mins < end_mins

    operator = node.get('operator', '').upper()
    operands = node.get('operands', [])

    if operator == 'AND':
        return all(evaluate_condition(child, registry, current_time) for child in operands)
    elif operator == 'OR':
        return any(evaluate_condition(child, registry, current_time) for child in operands)
    elif operator == 'NOT':
        if operands:
            return not evaluate_condition(operands[0], registry, current_time)
        return True

    return False


# --- Main Loop ---

def run_automation(server: str, config_path: str):
    dispatcher = CommandDispatcher(rate_limit_delay=0.0)
    controller = MatterController(server_address=server)

    # Inject dispatcher so device commands go through the queue
    for dev in controller.devices.values():
        dev.dispatcher = dispatcher

    with open(config_path, 'r', encoding='utf-8') as f:
        lighting_configs = json.load(f)

    device_map = {dev.node_id: dev for dev in controller.devices.values()}

    for dev in controller.devices.values():
        if isinstance(dev, SensorDevice):
            dev.subscribe_occupancy(create_sensor_callback(dev.node_id))

    state_cache = {}
    logging.info("System initialized. Entering main loop.")

    try:
        while True:
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

                # Evaluate occupancy
                sensor_cond = config.get('sensor_condition')
                legacy_sensors = config.get('sensor', [])

                if sensor_cond:
                    is_occupied = evaluate_condition(sensor_cond, sensor_registry, now)
                elif legacy_sensors:
                    is_occupied = any(
                        evaluate_condition({"type": "sensor", **s}, sensor_registry, now)
                        for s in legacy_sensors
                    )
                else:
                    is_occupied = True

                target_level = int(target_state.get('level', 0)) if is_occupied else 0
                target_on = target_level > 0

                prev = state_cache.get(device_id, {'state': None, 'level': -1, 'kelvin': -1})

                if target_on:
                    matter_level = int((target_level / 100.0) * 254)

                    if prev['state'] != 'ON':
                        logging.info("[" + device.name + "] TURN ON")
                        device.turn_on()
                        prev['state'] = 'ON'

                    if abs(prev['level'] - matter_level) >= 2:
                        logging.info("[" + device.name + "] Level: " + str(matter_level))
                        device.set_level(matter_level)
                        prev['level'] = matter_level

                    if 'kelvin' in target_state:
                        kelvin = int(target_state['kelvin'])
                        mireds = int(1000000 / kelvin) if kelvin > 0 else 250

                        if abs(prev['kelvin'] - kelvin) > 50 and hasattr(device, 'set_color_temperature'):
                            logging.info("[" + device.name + "] Temp: " + str(kelvin) + "K")
                            device.set_color_temperature(mireds)
                            prev['kelvin'] = kelvin
                else:
                    if prev['state'] != 'OFF':
                        logging.info("[" + device.name + "] TURN OFF")
                        device.turn_off()
                        prev['state'] = 'OFF'
                        prev['level'] = 0

                state_cache[device_id] = prev

    except KeyboardInterrupt:
        logging.info("Terminated by user.")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Matter Lighting Automation")
    parser.add_argument("--server", required=True, help="Server IP:PORT")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    args = parser.parse_args()

    try:
        run_automation(args.server, args.config)
    except KeyboardInterrupt:
        logging.info("System halted.")
        sys.exit(0)
