import json
import subprocess
import threading
import logging
import urllib.request
import sys

class MatterDevice:
    def __init__(self, config):
        self.node_id = config.get('node_id')
        self.name = config.get('name')
        self.hardware_type = config.get('hardware_type')
        self.events = config.get('events', {})
        self.dispatcher = None

    def _run_script(self, event_name, *args):
        if event_name not in self.events:
            raise ValueError(f"Event '{event_name}' is not defined for {self.name}.")

        script = self.events[event_name]['script']
        cmd = ["python3", "-c", script] + [str(a) for a in args]

        if self.dispatcher:
            self.dispatcher.enqueue_command(cmd)
            return "Command queued"

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
            if not script:
                return

            process = subprocess.Popen(
                ["python3", "-u", "-c", script],
                stdout=subprocess.PIPE, text=True
            )
            for line in process.stdout:
                if line.strip():
                    callback_function(line.strip())

        listener_thread = threading.Thread(target=run_stream, daemon=True)
        listener_thread.start()
        return listener_thread


class MatterController:
    def __init__(self, server_address: str = None, json_path: str = None):
        if server_address:
            data = self._fetch_from_api(server_address)
        elif json_path:
            data = self._load_from_file(json_path)
        else:
            raise ValueError("Provide either server_address or json_path.")

        self.devices = {}
        for dev_config in data.get('devices', []):
            hw_type = dev_config.get('hardware_type', '')
            if 'light' in hw_type:
                self.devices[dev_config['name']] = LightDevice(dev_config)
            elif 'sensor' in hw_type:
                self.devices[dev_config['name']] = SensorDevice(dev_config)

    def _fetch_from_api(self, server_address: str) -> dict:
        url = f"http://{server_address}/api/metadata"
        logging.info("Fetching hardware metadata from: " + url)
        try:
            response = urllib.request.urlopen(url)
            return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            logging.error("Failed to retrieve metadata: " + str(e))
            sys.exit(1)

    def _load_from_file(self, json_path: str) -> dict:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_device(self, name: str):
        return self.devices.get(name)
