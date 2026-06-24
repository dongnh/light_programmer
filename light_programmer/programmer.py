from __future__ import annotations

import json
import os
import threading
import time
import argparse
import logging
import sys
import queue
import urllib.request
from datetime import datetime, timedelta

from .matter_lib import (
    MatterController,
    SensorDevice,
    LightDevice,
    MatterClient,
)
from . import mode_state, mode_http

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)

# Global sensor state and event trigger
sensor_registry = {}

# Optional Yeelight colour-flow endpoint (for rain "effect": "flow" lights).
_flow_cfg = {"server": None, "api_key": None}


def _flow_post(path: str, body: dict) -> bool:
    """POST a command to the Yeelight bridge (best-effort). True on success."""
    server = _flow_cfg.get("server")
    if not server:
        return False
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _flow_cfg.get("api_key"):
        headers["X-API-Key"] = _flow_cfg["api_key"]
    req = urllib.request.Request("http://" + server + path, data=data,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            r.read()
        return True
    except Exception as e:  # noqa: BLE001 - flow is best-effort, never block the loop
        logging.warning("flow %s failed: %s", path, e)
        return False


def _flow_changed(prev: dict, base: int, peak: int, kelvin: int, lightning: bool) -> bool:
    """True if the flow needs (re)sending: not active, or params drifted enough."""
    f = prev.get("flow")
    if not prev.get("flow_active") or not f:
        return True
    return (abs(f["base"] - base) >= 3 or abs(f["peak"] - peak) >= 3
            or f["kelvin"] != kelvin or f["lightning"] != lightning)
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

def _is_moon_level(level) -> bool:
    """A setpoint in the open (0, 1) band is an explicit moonlight request."""
    try:
        return 0.0 < float(level) < 1.0
    except (TypeError, ValueError):
        return False


def _unoccupied_level(unocc_cfg, current_minutes: int):
    """Brightness to hold while a light is UNOCCUPIED. Default 0 (off).

    Without an `unoccupied` block a light just turns off when its sensor_condition
    is false — the original behaviour. An `unoccupied` block is a list of
    {start, end, level} windows (HH:MM, cross-midnight ok, same form as
    time_window); the first window containing `current_minutes` wins and its
    `level` follows the schedule convention (0 = off, 0<level<1 = moonlight,
    level>=1 = daylight). Outside every window -> 0 (off). This lets an
    unoccupied light fall back to a dim moonlight glow in one time band and to
    full off in another (e.g. evening ambient -> off overnight)."""
    if not unocc_cfg:
        return 0
    for w in unocc_cfg:
        start = time_to_minutes(w.get('start', '00:00'))
        end = time_to_minutes(w.get('end', '23:59'))
        inside = (start <= current_minutes < end) if start <= end \
            else (current_minutes >= start or current_minutes < end)
        if inside:
            return w.get('level', 0)
    return 0


def calculate_current_state(schedule: list, current_minutes: int) -> dict:
    sorted_sched = sorted(schedule, key=lambda x: time_to_minutes(x['time']))
    if not sorted_sched: return {"level": 0, "_moon": False}
    if len(sorted_sched) == 1:
        only = sorted_sched[0]
        st = {"level": only.get('level', 0), "_moon": _is_moon_level(only.get('level', 0))}
        if 'kelvin' in only:
            st['kelvin'] = only['kelvin']
        return st

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
    # Moonlight is OPT-IN: the segment counts as moonlight only when a bracketing
    # setpoint is an explicit sub-1 level. Interpolation ramps between 0 and a
    # daylight level (or rain-scaled fractions) must NOT trigger mode 5.
    target_state['_moon'] = (_is_moon_level(prev_point.get('level', 0))
                             or _is_moon_level(next_point.get('level', 0)))
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
            # A dedicated rain sensor streams a `rain` key; presence sensors a
            # `occupancy` key. Treat either as the binary active signal so the
            # same registry/condition machinery works for both.
            occupancy = payload.get("occupancy")
            if occupancy is None:
                occupancy = payload.get("rain", 0)
            logging.info("Sensor Stream [" + sensor_id + "]: " + str(payload))

            current_state = sensor_registry.get(sensor_id, {"is_occupied": False, "last_cleared": datetime.min})
            state_mutated = False

            # Remember the latest real rain intensity (light/moderate/heavy/violent)
            # so a rain override can scale brightness by it. Skip "none"/null so the
            # last real value is retained through a brief lull within the timeout.
            intensity = payload.get("rain_intensity")
            if intensity and intensity != "none":
                current_state["rain_intensity"] = intensity

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

def _rain_active(rain_cfg: dict, now: datetime) -> bool:
    """True if a light entry's optional `rain` override should apply right now.

    Accepts the same sensor shapes as a light's gating: a `sensor_condition` AST
    or a legacy `sensor` list (any active -> True). With no sensor configured the
    override never triggers (returns False) — rain must be observed, not assumed.
    """
    cond = rain_cfg.get('sensor_condition')
    if cond:
        return evaluate_condition(cond, sensor_registry, now)
    sensors = rain_cfg.get('sensor', [])
    if sensors:
        return any(
            evaluate_condition({"type": "sensor", **s}, sensor_registry, now)
            for s in sensors
        )
    return False


_INTENSITY_RANK = {"light": 1, "moderate": 2, "heavy": 3, "violent": 4}


def _collect_sensor_ids(node: dict, out: set) -> None:
    if not node:
        return
    if node.get('type') == 'sensor' and node.get('id'):
        out.add(node['id'])
    for child in node.get('operands', []):
        _collect_sensor_ids(child, out)


def _rain_intensity(rain_cfg: dict) -> str | None:
    """Heaviest current rain intensity across the override's referenced sensors.

    Reads the `rain_intensity` each rain sensor last streamed (light/moderate/
    heavy/violent), returning the strongest, or None if none reported one.
    """
    ids = {s['id'] for s in rain_cfg.get('sensor', []) if s.get('id')}
    _collect_sensor_ids(rain_cfg.get('sensor_condition'), ids)
    best, best_rank = None, 0
    for sid in ids:
        inten = sensor_registry.get(sid, {}).get('rain_intensity')
        rank = _INTENSITY_RANK.get(inten, 0)
        if rank > best_rank:
            best, best_rank = inten, rank
    return best


def _apply_rain_override(target_state: dict, rain_cfg: dict, intensity: str | None) -> None:
    """Overlay rain-time color temperature / brightness onto the scheduled state.

    Brightness, in precedence order:
    - `intensity_scale` : multiply the SCHEDULED brightness by a per-intensity
                          factor, e.g. {"light":0.85,"moderate":0.65,"heavy":0.45,
                          "violent":0.25}. Blends rain with the circadian schedule
                          so the skylight still tracks time-of-day, just dimmer.
    - `intensity_level` : map rain intensity -> absolute brightness (ignores schedule);
    - `level`           : a single absolute brightness (0-100); OR
    - `level_scale`     : multiply the scheduled brightness by a single factor.
    Color temperature: `intensity_kelvin` map (per intensity), else `kelvin`.
    Lets an artificial skylight dim in step with how hard it's actually raining.
    """
    ik = rain_cfg.get('intensity_kelvin')
    if ik and intensity in ik:
        target_state['kelvin'] = ik[intensity]
    elif 'kelvin' in rain_cfg:
        target_state['kelvin'] = rain_cfg['kelvin']

    isc = rain_cfg.get('intensity_scale')
    il = rain_cfg.get('intensity_level')
    if isc and intensity in isc:
        target_state['level'] = target_state.get('level', 0) * float(isc[intensity])
    elif il and intensity in il:
        target_state['level'] = il[intensity]
    elif 'level' in rain_cfg:
        target_state['level'] = rain_cfg['level']
    elif 'level_scale' in rain_cfg:
        target_state['level'] = target_state.get('level', 0) * float(rain_cfg['level_scale'])


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

    # Optional rain override: while a rain sensor is active, recolor/dim the
    # (already-on) device — e.g. an artificial skylight going overcast.
    # Rain override must never turn a light ON: gate on the SCHEDULED level
    # being > 0 so an absolute rain form (`level`/`intensity_level`) can't write
    # a non-zero brightness onto a scheduled-OFF light. (Multiplicative forms
    # already collapse to 0; gating here also skips the colour-temp/flow/
    # moonlight branches for a scheduled-off light.) A moonlight setpoint
    # (0<level<1) counts as on, so a moonlit skylight can still be recoloured.
    rain_cfg = config.get('rain')
    sched_level = int(round(float(target_state.get('level', 0))))  # pre-override, for flow base/peak
    raining = (bool(rain_cfg) and is_occupied
               and float(target_state.get('level', 0)) > 0
               and _rain_active(rain_cfg, now))
    intensity = _rain_intensity(rain_cfg) if raining else None
    if raining:
        _apply_rain_override(target_state, rain_cfg, intensity)

    # Brightness is a FLOAT. Moonlight (the night-light channel) is OPT-IN: it
    # engages ONLY when the active schedule segment has an explicit sub-1 setpoint
    # (target_state['_moon']). A sub-1 value that is merely an interpolation ramp
    # (0 -> daylight) or a rain-scaled fraction is NOT moonlight — it collapses to
    # off, exactly as the pre-moonlight int() truncation did.
    seg_is_moon = bool(target_state.get('_moon'))
    if is_occupied:
        level_f = float(target_state.get('level', 0))
    else:
        # Unoccupied: fall back to the configured `unoccupied` windows (default
        # OFF). A sub-1 window level requests moonlight instead of off — e.g. a
        # skylight that glows dim in the evening when no one is at the desk, then
        # goes fully dark overnight. This is the only way to get a non-off
        # unoccupied state; the schedule defines the OCCUPIED behaviour.
        u_level = _unoccupied_level(config.get('unoccupied'), current_minutes)
        level_f = float(u_level)
        seg_is_moon = _is_moon_level(u_level)
    if not seg_is_moon and 0.0 < level_f < 1.0:
        level_f = 0.0  # daylight ramp / rain fraction below 1% -> off (legacy behaviour)
    is_moon = seg_is_moon and 0.0 < level_f < 1.0
    if is_moon and not _flow_cfg.get('server'):
        # Moonlight needs the direct Yeelight channel (--yeelight-server); without
        # it, fall back to the lowest normal level so the light still turns on.
        is_moon = False
        level_f = 1.0
    target_on = level_f > 0
    target_level = int(round(level_f))  # daylight 0-100 (>= 1 only)

    prev = state_cache.get(config['id'], {'state': None, 'level': -1, 'kelvin': -1,
                                          'rain': False, 'flow': None, 'flow_active': False,
                                          'moon': None})
    rain_tag = (intensity or "on") if raining else False
    if prev.get('rain') != rain_tag:
        logging.info("[" + device.name + "] RAIN " + (str(rain_tag) if raining else "off"))
        prev['rain'] = rain_tag

    # Colour-flow effect (Yeelight on-device animation) — opt-in per rain entry.
    # Brightness stays synced to the schedule: base = scheduled x intensity_scale
    # (already in target_level), peak = the full scheduled level.
    use_flow = (raining and target_on and (rain_cfg or {}).get('effect') == 'flow'
                and _flow_cfg.get('server'))
    if use_flow:
        base, peak = target_level, sched_level
        kelvin = int(rain_cfg.get('kelvin', 4500))
        lightning = intensity in rain_cfg.get('flash_levels', ['violent'])
        if _flow_changed(prev, base, peak, kelvin, lightning):
            _flow_post('/api/flow', {'id': config['id'], 'base': base, 'peak': peak,
                                     'kelvin': kelvin, 'lightning': lightning})
            prev['flow'] = {'base': base, 'peak': peak, 'kelvin': kelvin, 'lightning': lightning}
            prev['flow_active'] = True
            prev['state'], prev['level'], prev['kelvin'] = 'ON', -1, -1  # flow owns the bulb
            prev['moon'] = None  # flow took the bulb off the night-light channel
            logging.info("[%s] FLOW %s base=%d peak=%d%s", device.name, intensity,
                         base, peak, " +lightning" if lightning else "")
        state_cache[config['id']] = prev
        return  # flow drives the bulb; skip static level/temp control this tick

    if prev.get('flow_active'):
        # Flow should stop (rain cleared, light off, or effect disabled).
        _flow_post('/api/flow/stop', {'id': config['id']})
        prev['flow_active'], prev['flow'] = False, None
        prev['state'], prev['level'], prev['kelvin'] = None, -1, -1  # force schedule re-apply
        prev['moon'] = None  # force a fresh moonlight re-apply if still in the band
        logging.info("[%s] FLOW stopped", device.name)

    if target_on and is_moon:
        # Sub-1 schedule level -> moonlight channel. Drive the Yeelight bridge
        # directly (like flow); nl_br = level x 100 (0.1 -> 10%, 0.9 -> 90%).
        nl = max(1, min(100, int(round(level_f * 100))))
        if prev.get('moon') != nl:
            if _flow_post('/api/moonlight', {'id': config['id'], 'on': True, 'level': nl}):
                logging.info("[%s] MOONLIGHT %d%%", device.name, nl)
                prev['moon'] = nl
                prev['state'] = 'ON'
                prev['level'] = -1   # force daylight re-apply when leaving moonlight
                prev['kelvin'] = -1
            # else: POST failed -> leave prev['moon'] so the next tick retries
    elif target_on:
        if prev.get('moon'):
            # Leaving moonlight: the bridge's /api/level resets NORMAL mode for
            # night-light-capable bulbs, so just force a level re-apply here.
            prev['moon'] = None
            prev['level'] = -1
            prev['kelvin'] = -1

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
        prev['moon'] = None

    state_cache[config['id']] = prev


def _force_off_all(configs, device_map):
    """Used when entering kill mode — turn every configured device off once."""
    for cfg in configs:
        dev = device_map.get(cfg['id'])
        if dev is None:
            continue
        try:
            dev.turn_off()
            logging.info(f"[{getattr(dev, 'name', cfg['id'])}] KILL → OFF")
        except Exception as e:
            logging.warning(f"KILL turn_off failed for {cfg['id']}: {e}")


ONLINE_POLL_INTERVAL = 10.0  # seconds between matter_webcontrol reachability polls
ONLINE_POLL_TIMEOUT = 3.0    # short per-request timeout for the reachability poll (own client)


def _fetch_online(client) -> dict | None:
    """Map device_id -> online bool from matter_webcontrol's /api/metadata.

    Returns None when the controller itself is unreachable, so callers keep the
    last known per-light state instead of flapping every light to disconnected.
    A configured light that is simply absent from the metadata (e.g. a logical
    bridge that dropped) is reported by its caller as offline.
    """
    try:
        data = client.get("/api/metadata")
    except Exception as e:  # noqa: BLE001 - a poll failure must never kill the loop
        logging.warning("online poll failed: %s", e)
        return None
    out = {}
    for dev in data.get("devices", []):
        did = dev.get("id")
        if did:
            out[did] = bool(dev.get("online", True))
    return out


def run_automation(server: str, config_path: str, api_key: str = None,
                   mode_state_path: str = None,
                   mode_http_host: str = "127.0.0.1",
                   mode_http_port: int = 7870,
                   yeelight_server: str = None,
                   yeelight_api_key: str = None):
    _flow_cfg["server"] = yeelight_server
    _flow_cfg["api_key"] = yeelight_api_key
    if yeelight_server:
        logging.info("Yeelight colour-flow endpoint: %s", yeelight_server)
    dispatcher = CommandDispatcher(rate_limit_delay=0.0)
    controller = MatterController(server_address=server, api_key=api_key)

    # Separate, short-timeout client for the off-loop reachability poll so a
    # stalled matter_webcontrol can never block the 1Hz control loop (which uses
    # controller.client with its 5s timeout). Distinct instance => the poll can't
    # share or mutate the control client's timeout.
    online_client = MatterClient(server, api_key=api_key, timeout=ONLINE_POLL_TIMEOUT)

    for dev in controller.devices.values():
        dev.dispatcher = dispatcher

    with open(config_path, 'r', encoding='utf-8') as f:
        configs = json.load(f)

    device_map = {dev.id: dev for dev in controller.devices.values()}

    # Per-light runtime status for the HomeKit bridge. An optional config `name`
    # overrides the controller's name (and makes the logs read friendlier); the
    # `connected` flag is refreshed from matter_webcontrol's `online` and served
    # via GET /lights so the bridge can drive one Contact Sensor per light.
    # Seed the roster from the CONFIGURED light entries, NOT from the boot-time
    # /api/metadata snapshot: a light whose logical bridge is down at LP start
    # (the Casambi boot race; office skylights) must still appear in /lights so
    # the bridge can create its Contact Sensor. A top-level config entry is a
    # light when it carries a `schedule` and is not a legacy AC entry; sensors
    # only appear nested inside a light's `sensor`/`sensor_condition`, never as
    # top-level entries. connected starts False and is flipped on by the online
    # poll once the device is reachable.
    light_status = {}  # id -> {"name": str, "connected": bool}
    for cfg in configs:
        cid = cfg.get("id")
        if not cid or cfg.get("type") == "ac" or "schedule" not in cfg:
            continue
        dev = device_map.get(cid)
        if cfg.get("name") and isinstance(dev, LightDevice):
            dev.name = cfg["name"]
        name = cfg.get("name") or (dev.name if isinstance(dev, LightDevice) else cid)
        light_status[cid] = {"name": name, "connected": False}
        if not isinstance(dev, LightDevice):
            logging.warning(
                "Configured light %s not found in controller metadata at "
                "startup (offline/bridge down?) — listed in /lights as "
                "disconnected until it appears", cid)

    def lights_provider():
        return [
            {"id": cid, "name": v["name"], "connected": v["connected"]}
            for cid, v in light_status.items()
        ]

    # Reachability poll runs OFF the control loop on its own daemon thread so a
    # stalled matter_webcontrol never blocks scheduling. It only mutates the
    # bool `connected` values of light_status entries created at startup; the
    # KEY SET is fixed before this thread starts and never changes afterwards,
    # so the HTTP server thread reading via lights_provider is GIL-safe without
    # an explicit lock (no concurrent insert/delete; bool assignment is atomic).
    def _online_poll_loop():
        while True:
            online = _fetch_online(online_client)
            if online is not None:
                for cid, v in light_status.items():
                    v["connected"] = online.get(cid, False)
            time.sleep(ONLINE_POLL_INTERVAL)

    threading.Thread(target=_online_poll_loop, daemon=True).start()

    for dev in controller.devices.values():
        if isinstance(dev, SensorDevice):
            dev.subscribe_occupancy(create_sensor_callback(dev.id))

    state_cache = {}

    if mode_state_path:
        mode_http.start_in_thread(
            mode_state_path, mode_http_host, mode_http_port,
            on_change=state_changed_event.set,
            lights_provider=lights_provider,
        )
        logging.info(f"Mode state file: {mode_state_path}")

    prev_kill = False
    prev_auto = True
    logging.info("System initialized. Entering main loop.")

    try:
        while True:
            state_changed_event.wait(timeout=1.0)
            state_changed_event.clear()

            mode = mode_state.load(mode_state_path) if mode_state_path else mode_state.DEFAULT
            kill = mode["kill"]
            auto = mode["auto"]
            now = datetime.now()

            if kill and not prev_kill:
                logging.warning("KILL switch engaged — turning all devices off")
                _force_off_all(configs, device_map)
                state_cache.clear()
            elif not auto and prev_auto:
                logging.info("Auto Mode disabled — schedule paused (devices left as-is)")
            elif (prev_kill and not kill) or (not prev_auto and auto):
                logging.info("Resuming automation — clearing state cache for fresh apply")
                state_cache.clear()

            prev_kill, prev_auto = kill, auto

            if kill or not auto:
                continue

            current_minutes = now.hour * 60 + now.minute

            for config in configs:
                device_id = config['id']
                device = device_map.get(device_id)
                if device is None:
                    continue

                if isinstance(device, LightDevice):
                    _apply_light(config, device, now, current_minutes, state_cache)

    except KeyboardInterrupt:
        logging.info("Terminated by user.")
        sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Matter Lighting Automation")
    parser.add_argument("--server", required=True, help="Server IP:PORT")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--api-key", default=os.environ.get("MATTER_SRV_KEY"),
                        help="X-API-Key for the matter_webcontrol server (or set MATTER_SRV_KEY)")
    parser.add_argument("--mode-state", default=os.environ.get("LP_MODE_STATE"),
                        help="Path to mode-state JSON (auto/kill flags). "
                             "Required to enable the /mode HTTP endpoint and HomeKit bridge integration.")
    parser.add_argument("--mode-http-host", default="127.0.0.1",
                        help="Bind host for the mode HTTP server (default 127.0.0.1; use 0.0.0.0 for LAN).")
    parser.add_argument("--mode-http-port", type=int, default=7870,
                        help="Bind port for the mode HTTP server (default 7870).")
    parser.add_argument("--yeelight-server", default=os.environ.get("LP_YEELIGHT_SERVER"),
                        help="Yeelight bridge IP:PORT for rain 'effect: flow' colour-flow "
                             "animations (e.g. 127.0.0.1:9800). Optional.")
    parser.add_argument("--yeelight-api-key", default=os.environ.get("LP_YEELIGHT_KEY"),
                        help="X-API-Key for the Yeelight bridge, if it requires one.")
    args = parser.parse_args()

    try:
        run_automation(args.server, args.config, api_key=args.api_key,
                       mode_state_path=args.mode_state,
                       mode_http_host=args.mode_http_host,
                       mode_http_port=args.mode_http_port,
                       yeelight_server=args.yeelight_server,
                       yeelight_api_key=args.yeelight_api_key)
    except KeyboardInterrupt:
        logging.info("System halted.")
        sys.exit(0)

if __name__ == "__main__":
    main()
