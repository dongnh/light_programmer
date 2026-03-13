import json
import subprocess
import threading

class MatterDevice:
    # Initialize basic device properties
    def __init__(self, config):
        self.node_id = config.get('node_id')
        self.name = config.get('name')
        self.hardware_type = config.get('hardware_type')
        self.events = config.get('events', {})

    # Execute the embedded python script in an isolated process
    def _run_script(self, event_name, *args):
        if event_name not in self.events:
            raise ValueError(f"Event '{event_name}' is not defined for {self.name}.")
        
        script = self.events[event_name]['script']
        cmd = ["python3", "-c", script] + [str(a) for a in args]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip()


class LightDevice(MatterDevice):
    # Standard lighting control methods
    def turn_on(self):
        return self._run_script("turn_on")

    def turn_off(self):
        return self._run_script("turn_off")

    def set_level(self, level: int):
        return self._run_script("set_level", level)

    def read_level(self):
        return self._run_script("read_level")

    # Color temperature methods are only executed if defined in the payload
    def set_color_temperature(self, mireds: int):
        return self._run_script("set_color_temperature", mireds)

    def read_color_temperature(self):
        return self._run_script("read_color_temperature")


class SensorDevice(MatterDevice):
    # Standard sensor polling method
    def read_occupancy(self):
        return self._run_script("read_occupancy")

    # Execute asynchronous stream listener in a background thread
    def subscribe_occupancy(self, callback_function):
        def run_stream():
            script = self.events.get("subscribe_occupancy", {}).get("script", "")
            if not script:
                return
                
            process = subprocess.Popen(["python3", "-c", script], stdout=subprocess.PIPE, text=True)
            for line in process.stdout:
                if line.strip():
                    callback_function(line.strip())
                    
        listener_thread = threading.Thread(target=run_stream, daemon=True)
        listener_thread.start()
        return listener_thread


class MatterController:
    # Parse JSON configuration and instantiate hardware device objects
    def __init__(self, json_path: str):
        with open(json_path, 'r', encoding='utf-8') as file:
            data = json.load(file)

        self.bridge_config = data.get('bridge', {})
        self.devices = {}

        for dev_config in data.get('devices', []):
            hw_type = dev_config.get('hardware_type', '')
            if 'light' in hw_type:
                self.devices[dev_config['name']] = LightDevice(dev_config)
            elif 'sensor' in hw_type:
                self.devices[dev_config['name']] = SensorDevice(dev_config)

    # Retrieve an instantiated device object by its name attribute
    def get_device(self, name: str):
        return self.devices.get(name)