import json
import threading
import logging
import urllib.request
import urllib.error
import urllib.parse
import sys
import time


class MatterClient:
    """Thin HTTP client for matter_webcontrol's REST API."""

    def __init__(self, server_address: str, api_key: str = None, timeout: float = 5.0):
        self.base_url = f"http://{server_address}"
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self, content_type: str = None) -> dict:
        h = {}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if content_type:
            h["Content-Type"] = content_type
        return h

    def get(self, path: str, params: dict = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers=self._headers("application/json"), method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data else {}

    def open_stream(self, path: str, params: dict = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        return urllib.request.urlopen(req, timeout=None)


class MatterDevice:
    def __init__(self, config: dict, client: MatterClient):
        self.id = config.get("id") or config.get("node_id")
        self.name = config.get("name") or self.id
        self.hardware_type = config.get("hardware_type", "")
        self.capabilities = config.get("capabilities", [])
        self.client = client
        self.dispatcher = None

    def _dispatch(self, fn):
        if self.dispatcher:
            self.dispatcher.enqueue(fn)
            return None
        return fn()


class LightDevice(MatterDevice):
    def turn_on(self):
        return self._dispatch(lambda: self.client.get("/api/toggle", {"id": self.id}))

    def turn_off(self):
        return self._dispatch(lambda: self.client.post("/api/level", {"id": self.id, "level": 0}))

    def set_level(self, level: int):
        level = max(0, min(254, int(level)))
        return self._dispatch(lambda: self.client.post("/api/level", {"id": self.id, "level": level}))

    def read_level(self):
        return self.client.get("/api/level", {"id": self.id})

    def set_color_temperature(self, mireds: int):
        mireds = max(153, min(500, int(mireds)))
        return self._dispatch(lambda: self.client.post("/api/mired", {"id": self.id, "mireds": mireds}))

    def read_color_temperature(self):
        return self.client.get("/api/mired", {"id": self.id})


class SensorDevice(MatterDevice):
    def read_occupancy(self):
        data = self.client.get("/api/sensor", {"id": self.id})
        return data.get("occupancy")

    def subscribe_occupancy(self, callback_function, reconnect_delay: float = 5.0):
        """Open SSE stream to /api/subscribe and invoke callback for each `data:` line."""

        def run_stream():
            while True:
                try:
                    stream = self.client.open_stream("/api/subscribe", {"id": self.id})
                    for raw in stream:
                        line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                        if not line or line.startswith(":"):
                            continue
                        callback_function(line)
                except Exception as e:
                    logging.warning(f"Sensor stream [{self.id}] disconnected: {e}")
                time.sleep(reconnect_delay)

        listener_thread = threading.Thread(target=run_stream, daemon=True)
        listener_thread.start()
        return listener_thread


class ClimateSensorDevice(MatterDevice):
    """Temperature/humidity sensor (read via /api/climate?id=...)."""

    def read_climate(self) -> dict:
        """Return {'temperature': °C, 'humidity': %} (keys may be missing)."""
        data = self.client.get("/api/climate", {"id": self.id})
        # /api/climate?id= returns the device record directly.
        if isinstance(data, dict) and "devices" in data:
            for d in data.get("devices", []):
                if d.get("id") == self.id:
                    return d
            return {}
        return data or {}

    def read_temperature(self):
        return self.read_climate().get("temperature")

    def read_humidity(self):
        return self.read_climate().get("humidity")


# Matter Thermostat system_mode values.
AC_MODE_OFF = 0
AC_MODE_AUTO = 1
AC_MODE_COOL = 3
AC_MODE_HEAT = 4
AC_MODE_FAN = 7
AC_MODE_DRY = 8

AC_MODE_NAMES = {
    "off": AC_MODE_OFF,
    "auto": AC_MODE_AUTO,
    "cool": AC_MODE_COOL,
    "heat": AC_MODE_HEAT,
    "fan": AC_MODE_FAN,
    "fan_only": AC_MODE_FAN,
    "dry": AC_MODE_DRY,
}


def parse_ac_mode(mode) -> int:
    if isinstance(mode, int):
        return mode
    if isinstance(mode, str):
        return AC_MODE_NAMES.get(mode.lower(), AC_MODE_OFF)
    return AC_MODE_OFF


class ACDevice(MatterDevice):
    """AC / Thermostat — Matter cluster 0x0201, controlled via /api/ac."""

    def read_state(self) -> dict:
        return self.client.get("/api/ac", {"id": self.id}) or {}

    def set_state(self, on: bool = None, mode=None, setpoint: float = None):
        payload = {"id": self.id}
        if on is not None:
            payload["on"] = bool(on)
        if mode is not None:
            payload["mode"] = parse_ac_mode(mode)
        if setpoint is not None:
            payload["setpoint"] = float(setpoint)
        return self._dispatch(lambda: self.client.post("/api/ac", payload))

    def turn_on(self, mode=AC_MODE_COOL, setpoint: float = None):
        return self.set_state(on=True, mode=mode, setpoint=setpoint)

    def turn_off(self):
        return self.set_state(on=False, mode=AC_MODE_OFF)

    def set_mode(self, mode):
        return self.set_state(mode=mode)

    def set_setpoint(self, setpoint: float):
        return self.set_state(setpoint=setpoint)


def _classify(dev_config: dict) -> str:
    hw = (dev_config.get("hardware_type") or "").lower()
    caps = [str(c).lower() for c in dev_config.get("capabilities", [])]
    states = dev_config.get("states", {}) or {}

    if ("thermostat" in hw or "ac" in hw or "air_conditioner" in hw
            or "thermostat" in caps or "system_mode" in caps
            or "system_mode" in states or "cooling_setpoint" in states
            or "heating_setpoint" in states):
        return "ac"
    if ("temperature" in caps or "humidity" in caps
            or "climate" in hw or "temperature" in states or "humidity" in states):
        # Pure climate sensors (no occupancy capability).
        if "occupancy" not in caps:
            return "climate"
    if "sensor" in hw or "occupancy" in caps or "rain" in caps or "rain" in states:
        return "sensor"
    if "light" in hw or "on_off" in caps or "brightness" in caps:
        return "light"
    return "unknown"


class MatterController:
    """Loads device list from matter_webcontrol /api/metadata or a JSON file."""

    DEVICE_CLASSES = {
        "light": LightDevice,
        "sensor": SensorDevice,
        "climate": ClimateSensorDevice,
        "ac": ACDevice,
    }

    def __init__(self, server_address: str = None, json_path: str = None,
                 api_key: str = None, client: MatterClient = None):
        if client is not None:
            self.client = client
        elif server_address:
            self.client = MatterClient(server_address, api_key=api_key)
        else:
            self.client = None

        if server_address:
            data = self._fetch_from_api()
        elif json_path:
            data = self._load_from_file(json_path)
        else:
            raise ValueError("Provide either server_address or json_path.")

        self.devices = {}
        for dev_config in data.get("devices", []):
            kind = _classify(dev_config)
            cls = self.DEVICE_CLASSES.get(kind)
            if cls is None:
                continue
            key = dev_config.get("name") or dev_config.get("id")
            self.devices[key] = cls(dev_config, self.client)

    def _fetch_from_api(self) -> dict:
        logging.info(f"Fetching device metadata from {self.client.base_url}/api/metadata")
        try:
            return self.client.get("/api/metadata")
        except urllib.error.HTTPError as e:
            logging.error(f"Metadata request failed: HTTP {e.code} {e.reason}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Failed to retrieve metadata: {e}")
            sys.exit(1)

    @staticmethod
    def _load_from_file(json_path: str) -> dict:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_device(self, name: str):
        return self.devices.get(name)
