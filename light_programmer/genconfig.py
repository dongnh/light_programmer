import argparse
import json
import os
import re
import sys
import urllib.error

from .matter_lib import MatterClient, _classify


def generate_lighting_config(ip_address, port, output_filename, api_key=None):
    client = MatterClient(f"{ip_address}:{port}", api_key=api_key)

    try:
        metadata = client.get("/api/metadata")
    except urllib.error.HTTPError as e:
        print(f"Connection error: HTTP {e.code} {e.reason}")
        return
    except Exception as error:
        print(f"Connection error: {error}")
        return

    devices = metadata.get("devices", [])

    classified = [(d, _classify(d)) for d in devices]
    light_nodes = [d for d, k in classified if k == "light"]
    sensor_nodes = [d for d, k in classified if k == "sensor"]

    mapped_sensors = [
        {"id": s.get("id"), "name": s.get("name") or s.get("id"), "timeout": 5}
        for s in sensor_nodes
    ]

    system_configurations = []
    for light in light_nodes:
        light_id = light.get("id")
        display_name = light.get("name") or light_id
        has_color = "color_temperature" in light.get("capabilities", []) or \
                    "color_temp_mireds" in light.get("states", {})

        if has_color:
            schedule = [
                {"time": "06:30", "level": 50, "kelvin": 4000},
                {"time": "12:00", "level": 100, "kelvin": 4000},
                {"time": "21:30", "level": 100, "kelvin": 2700},
            ]
        else:
            schedule = [
                {"time": "06:30", "level": 50},
                {"time": "12:00", "level": 100},
                {"time": "21:30", "level": 100},
            ]

        system_configurations.append({
            "id": light_id,
            "note": f"Auto-generated configuration for {display_name}",
            "schedule": schedule,
            "sensor": mapped_sensors,
        })

    raw_json = json.dumps(system_configurations, indent=2)

    compact_json = re.sub(
        r'\{\n\s+"time":\s+"([^"]+)",\n\s+"level":\s+(\d+),\n\s+"kelvin":\s+(\d+)\n\s+\}',
        r'{"time": "\1", "level": \2, "kelvin": \3}',
        raw_json,
    )
    compact_json = re.sub(
        r'\{\n\s+"time":\s+"([^"]+)",\n\s+"level":\s+(\d+)\n\s+\}',
        r'{"time": "\1", "level": \2}',
        compact_json,
    )
    compact_json = re.sub(
        r'\{\n\s+"id":\s+"([^"]+)",\n\s+"name":\s+(".+?"|null),\n\s+"timeout":\s+(\d+)\n\s+\}',
        r'{"id": "\1", "name": \2, "timeout": \3}',
        compact_json,
    )

    try:
        with open(output_filename, "w", encoding="utf-8") as file:
            file.write(compact_json)
        print(f"Configuration successfully saved to {output_filename}")
    except IOError as io_error:
        print(f"File write failed: {io_error}")


def main():
    parser = argparse.ArgumentParser(description="Generate compact lighting configuration.")
    parser.add_argument("--ip", type=str, required=True, help="IPv4 address of the bridge")
    parser.add_argument("--port", type=int, default=8080, help="Network port")
    parser.add_argument("--out", type=str, default="lighting_config.json", help="Output JSON filename")
    parser.add_argument("--api-key", type=str, default=os.environ.get("MATTER_SRV_KEY"),
                        help="X-API-Key for the matter_webcontrol server (or set MATTER_SRV_KEY)")

    args = parser.parse_args()
    generate_lighting_config(args.ip, args.port, args.out, api_key=args.api_key)


if __name__ == "__main__":
    main()
