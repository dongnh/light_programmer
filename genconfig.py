import urllib.request
import json
import argparse
import re

def generate_lighting_config(ip_address, port, output_filename):
    api_url = f"http://{ip_address}:{port}/api/metadata"
    
    try:
        request = urllib.request.urlopen(api_url)
        metadata = json.loads(request.read().decode('utf-8'))
    except Exception as error:
        print(f"Connection error: {error}")
        return

    device_list = metadata.get('devices', [])

    lighting_nodes = [node for node in device_list if 'light' in node.get('hardware_type', '').lower()]
    sensor_nodes = [node for node in device_list if 'sensor' in node.get('hardware_type', '').lower()]

    # Added name field to sensor mapping
    mapped_sensors = [{"id": s.get('node_id'), "name": s.get('name') or s.get('node_id'), "timeout": 5} for s in sensor_nodes]
    system_configurations = []

    for light in lighting_nodes:
        device_name = light.get('name')
        display_name = device_name if device_name else light.get('node_id')

        # Check for color control cluster to determine if kelvin should be included
        has_color_control = light.get('color_control_cluster')

        if has_color_control:
            schedule = [
                {"time": "06:30", "level": 50, "kelvin": 4000},
                {"time": "12:00", "level": 100, "kelvin": 4000},
                {"time": "21:30", "level": 100, "kelvin": 2700}
            ]
        else:
            schedule = [
                {"time": "06:30", "level": 50},
                {"time": "12:00", "level": 100},
                {"time": "21:30", "level": 100}
            ]

        node_config = {
            "id": light.get('node_id'),
            "note": f"Auto-generated configuration for {display_name}",
            "schedule": schedule,
            "sensor": mapped_sensors
        }
        system_configurations.append(node_config)

    raw_json = json.dumps(system_configurations, indent=2)

    # Compact formatting for schedules WITH kelvin
    compact_json = re.sub(
        r'\{\n\s+"time":\s+"([^"]+)",\n\s+"level":\s+(\d+),\n\s+"kelvin":\s+(\d+)\n\s+\}',
        r'{"time": "\1", "level": \2, "kelvin": \3}',
        raw_json
    )

    # Compact formatting for schedules WITHOUT kelvin
    compact_json = re.sub(
        r'\{\n\s+"time":\s+"([^"]+)",\n\s+"level":\s+(\d+)\n\s+\}',
        r'{"time": "\1", "level": \2}',
        compact_json
    )
    
    # Compact formatting for sensor mappings
    compact_json = re.sub(
        r'\{\n\s+"id":\s+"([^"]+)",\n\s+"name":\s+(".+?"|null),\n\s+"timeout":\s+(\d+)\n\s+\}',
        r'{"id": "\1", "name": \2, "timeout": \3}',
        compact_json
    )

    try:
        with open(output_filename, 'w', encoding='utf-8') as file:
            file.write(compact_json)
        print(f"Configuration successfully saved to {output_filename}")
    except IOError as io_error:
        print(f"File write failed: {io_error}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate compact lighting configuration.")
    parser.add_argument("--ip", type=str, required=True, help="IPv4 address of the bridge")
    parser.add_argument("--port", type=int, default=8080, help="Network port")
    parser.add_argument("--out", type=str, default="lighting_config.json", help="Output JSON filename")
    
    args = parser.parse_args()
    generate_lighting_config(args.ip, args.port, args.out)