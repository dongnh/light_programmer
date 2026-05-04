import json
import os
import threading
import time
import argparse
import logging
import sys
import queue
from datetime import datetime, timedelta

from .matter_lib import (
    MatterController,
    SensorDevice,
    ClimateSensorDevice,
    LightDevice,
    ACDevice,
    parse_ac_mode,
    AC_MODE_OFF,
)

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
    """Background queue that serializes calls to avoid flooding the controller."""

    def __init__(self, rate_limit_delay: float = 0.0):
        self.cmd_queue = queue.Queue()
        self.rate_limit_delay = rate_limit_delay
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()

    def _process_queue(self):
        while True:
            fn = self.cmd_queue.get()
            if fn is None:
                break
            try:
                fn()
            except Exception as e:
                logging.error("Command execution failed: " + str(e))
            finally:
                self.cmd_queue.task_done()

            if self.rate_limit_delay > 0:
                time.sleep(self.rate_limit_delay)

    def enqueue(self, fn):
        self.cmd_queue.put(fn)


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
        if not raw_data.startswith("data:"):
            return

        json_str = raw_data[5:].lstrip()
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


# --- Climate / time-window helpers ---

def _within_window(window: dict, now: datetime) -> bool:
    if not window:
        return True
    start_mins = time_to_minutes(window.get("start", "00:00"))
    end_mins = time_to_minutes(window.get("end", "23:59"))
    cur = now.hour * 60 + now.minute
    if start_mins <= end_mins:
        return start_mins <= cur < end_mins
    return cur >= start_mins or cur < end_mins


def decide_ac_on(config: dict, current_temp: float, prev_on: bool) -> bool:
    """Hysteresis decision based on ambient temperature.

    Cool mode: ON when temp >= on_above, OFF when temp <= off_below, stay otherwise.
    Heat mode: ON when temp <= on_below, OFF when temp >= off_above, stay otherwise.
    If only one threshold given, the other defaults to it (no hysteresis band).
    """
    if current_temp is None:
        return prev_on  # no reading -> hold last decision

    mode = (config.get("mode") or "cool").lower()

    if mode in ("cool", "dry", "fan", "fan_only", "auto"):
        on_above = config.get("on_above")
        off_below = config.get("off_below", on_above)
        if on_above is None:
            return prev_on
        if current_temp >= on_above:
            return True
        if off_below is not None and current_temp <= off_below:
            return False
        return prev_on

    if mode == "heat":
        on_below = config.get("on_below")
        off_above = config.get("off_above", on_below)
        if on_below is None:
            return prev_on
        if current_temp <= on_below:
            return True
        if off_above is not None and current_temp >= off_above:
            return False
        return prev_on

    return False


# --- Main Loop ---

def _apply_light(config, device, now, current_minutes, state_cache):
    schedule = config.get('schedule', [])
    target_state = calculate_current_state(schedule, current_minutes)

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

    prev = state_cache.get(config['id'], {'state': None, 'level': -1, 'kelvin': -1})

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

    state_cache[config['id']] = prev


def _apply_ac(config, device, now, climate_devices, state_cache):
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

    in_window = _within_window(config.get('active_window'), now)

    sensor_id = config.get('climate_sensor')
    climate_dev = climate_devices.get(sensor_id) if sensor_id else None
    current_temp = None
    if climate_dev is not None:
        try:
            current_temp = (climate_dev.read_climate() or {}).get('temperature')
        except Exception as e:
            logging.warning(f"Climate read failed for {sensor_id}: {e}")

    prev = state_cache.get(config['id'],
                           {'on': False, 'mode': None, 'setpoint': None, 'occupied_since': None})

    # Continuous-occupancy debounce: AC only turns on after motion has been satisfied
    # for `on_delay_minutes` (default 5) without interruption.
    on_delay = float(config.get('on_delay_minutes', 5))
    if is_occupied:
        if prev.get('occupied_since') is None:
            prev['occupied_since'] = now
        occupied_for = (now - prev['occupied_since']).total_seconds() / 60.0
    else:
        prev['occupied_since'] = None
        occupied_for = 0.0
    occupancy_ready = is_occupied and (prev['on'] or occupied_for >= on_delay)

    temp_says_on = decide_ac_on(config, current_temp, prev.get('on', False))
    effective_on = temp_says_on and occupancy_ready and in_window

    target_mode_name = (config.get('mode') or 'cool').lower()
    target_mode = parse_ac_mode(target_mode_name)
    target_setpoint = config.get('setpoint')
    if target_setpoint is not None:
        target_setpoint = float(target_setpoint)

    if effective_on:
        if not prev['on'] or prev['mode'] != target_mode:
            temp_log = f" (temp={current_temp:.1f}°C)" if current_temp is not None else ""
            sp_log = f", setpoint={target_setpoint:.1f}" if target_setpoint is not None else ""
            logging.info(f"[{device.name}] AC ON mode={target_mode_name}{sp_log}{temp_log}")
            device.set_state(on=True, mode=target_mode, setpoint=target_setpoint)
            prev['on'] = True
            prev['mode'] = target_mode
            prev['setpoint'] = target_setpoint
        elif (target_setpoint is not None and
              (prev['setpoint'] is None or abs(prev['setpoint'] - target_setpoint) >= 0.5)):
            logging.info(f"[{device.name}] AC setpoint -> {target_setpoint:.1f}")
            device.set_setpoint(target_setpoint)
            prev['setpoint'] = target_setpoint
    else:
        if prev['on']:
            temp_log = f" (temp={current_temp:.1f}°C)" if current_temp is not None else ""
            reason = ("temp" if not temp_says_on
                      else "occupancy" if not is_occupied
                      else "warmup" if not occupancy_ready
                      else "window")
            logging.info(f"[{device.name}] AC OFF [{reason}]{temp_log}")
            device.turn_off()
            prev['on'] = False
            prev['mode'] = AC_MODE_OFF
            prev['setpoint'] = None

    state_cache[config['id']] = prev


def run_automation(server: str, config_path: str, api_key: str = None):
    dispatcher = CommandDispatcher(rate_limit_delay=0.0)
    controller = MatterController(server_address=server, api_key=api_key)

    for dev in controller.devices.values():
        dev.dispatcher = dispatcher

    with open(config_path, 'r', encoding='utf-8') as f:
        configs = json.load(f)

    device_map = {dev.id: dev for dev in controller.devices.values()}
    climate_devices = {dev.id: dev for dev in controller.devices.values()
                       if isinstance(dev, ClimateSensorDevice)}

    for dev in controller.devices.values():
        if isinstance(dev, SensorDevice):
            dev.subscribe_occupancy(create_sensor_callback(dev.id))

    state_cache = {}
    logging.info("System initialized. Entering main loop.")

    try:
        while True:
            state_changed_event.wait(timeout=1.0)
            state_changed_event.clear()

            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute

            for config in configs:
                device_id = config['id']
                device = device_map.get(device_id)
                if device is None:
                    continue

                cfg_type = (config.get('type') or '').lower()
                is_ac = cfg_type == 'ac' or isinstance(device, ACDevice)

                if is_ac and isinstance(device, ACDevice):
                    _apply_ac(config, device, now, climate_devices, state_cache)
                elif isinstance(device, LightDevice):
                    _apply_light(config, device, now, current_minutes, state_cache)

    except KeyboardInterrupt:
        logging.info("Terminated by user.")
        sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Matter Lighting & AC Automation")
    parser.add_argument("--server", required=True, help="Server IP:PORT")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--api-key", default=os.environ.get("MATTER_SRV_KEY"),
                        help="X-API-Key for the matter_webcontrol server (or set MATTER_SRV_KEY)")
    args = parser.parse_args()

    try:
        run_automation(args.server, args.config, api_key=args.api_key)
    except KeyboardInterrupt:
        logging.info("System halted.")
        sys.exit(0)

if __name__ == "__main__":
    main()
