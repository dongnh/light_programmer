"""MCP server for Light Programmer.

Exposes tools for AI agents to discover Matter devices, inspect current state,
and read/write the automation config JSON. Direct device control is also
available for quick experimentation.

Run:
    pip install light-programmer[mcp]
    light-programmer-mcp                       # stdio transport (for Claude Desktop / Code)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The `mcp` package is required. Install with: pip install light-programmer[mcp]"
    ) from e

from .matter_lib import (
    AC_MODE_NAMES,
    ACDevice,
    ClimateSensorDevice,
    LightDevice,
    MatterClient,
    MatterController,
    SensorDevice,
    _classify,
    parse_ac_mode,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

mcp = FastMCP("light-programmer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client(server: str, api_key: Optional[str] = None) -> MatterClient:
    api_key = api_key or os.environ.get("MATTER_SRV_KEY")
    return MatterClient(server, api_key=api_key)


def _load(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Config root must be a JSON array of entries.")
    return data


def _save(path: str, entries: list) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_entry(entry: dict, idx: int) -> list[str]:
    errs: list[str] = []
    where = f"entry[{idx}] ({entry.get('id', '?')})"

    if not entry.get("id"):
        errs.append(f"{where}: missing 'id'")

    cfg_type = (entry.get("type") or "").lower()
    if cfg_type == "ac":
        mode = (entry.get("mode") or "").lower()
        if mode and mode not in AC_MODE_NAMES:
            errs.append(f"{where}: unknown AC mode '{mode}' (valid: {sorted(AC_MODE_NAMES)})")
        if mode in ("cool", "dry", "fan", "fan_only", "auto", ""):
            on_above = entry.get("on_above")
            off_below = entry.get("off_below")
            if on_above is None:
                errs.append(f"{where}: cooling AC needs 'on_above' (°C)")
            if on_above is not None and off_below is not None and off_below >= on_above:
                errs.append(f"{where}: 'off_below' ({off_below}) must be < 'on_above' ({on_above})")
        elif mode == "heat":
            on_below = entry.get("on_below")
            off_above = entry.get("off_above")
            if on_below is None:
                errs.append(f"{where}: heating AC needs 'on_below' (°C)")
            if on_below is not None and off_above is not None and off_above <= on_below:
                errs.append(f"{where}: 'off_above' ({off_above}) must be > 'on_below' ({on_below})")
        if entry.get("setpoint") is not None and not isinstance(entry["setpoint"], (int, float)):
            errs.append(f"{where}: 'setpoint' must be numeric")
        h_on = entry.get("humidity_above")
        h_off = entry.get("humidity_below")
        if h_on is not None and h_off is not None and h_off >= h_on:
            errs.append(f"{where}: 'humidity_below' ({h_off}) must be < 'humidity_above' ({h_on})")
        if entry.get("on_delay_minutes") is not None and entry["on_delay_minutes"] < 0:
            errs.append(f"{where}: 'on_delay_minutes' must be ≥ 0")
        win = entry.get("active_window")
        if win:
            for k in ("start", "end"):
                v = win.get(k)
                if v and not _TIME_RE.match(v):
                    errs.append(f"{where}: active_window.{k}='{v}' must be HH:MM")
    else:
        # Light entry
        for i, p in enumerate(entry.get("schedule", []) or []):
            t = p.get("time", "")
            if not _TIME_RE.match(t):
                errs.append(f"{where}.schedule[{i}].time='{t}' must be HH:MM")
            lvl = p.get("level")
            if lvl is None or not (0 <= lvl <= 100):
                errs.append(f"{where}.schedule[{i}].level must be 0–100")
            if "kelvin" in p and not (1000 <= p["kelvin"] <= 10000):
                errs.append(f"{where}.schedule[{i}].kelvin out of range")

    for i, s in enumerate(entry.get("sensor", []) or []):
        if not s.get("id"):
            errs.append(f"{where}.sensor[{i}] missing 'id'")
        if s.get("timeout") is not None and s["timeout"] < 0:
            errs.append(f"{where}.sensor[{i}].timeout must be ≥ 0")

    return errs


# ---------------------------------------------------------------------------
# Discovery & state tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_devices(server: str, api_key: Optional[str] = None) -> dict:
    """Fetch /api/metadata and return all devices grouped by kind.

    Args:
        server: matter_webcontrol address as IP:PORT (e.g. "192.168.1.220:8080").
        api_key: X-API-Key, or omit to use $MATTER_SRV_KEY.
    """
    data = _client(server, api_key).get("/api/metadata")
    grouped: dict[str, list] = {"light": [], "sensor": [], "climate": [], "ac": [], "unknown": []}
    for d in data.get("devices", []):
        kind = _classify(d)
        grouped.setdefault(kind, []).append({
            "id": d.get("id"),
            "name": d.get("name"),
            "hardware_type": d.get("hardware_type"),
            "capabilities": d.get("capabilities", []),
        })
    return grouped


@mcp.tool()
def read_climate(server: str, sensor_id: str, api_key: Optional[str] = None) -> dict:
    """Read temperature (°C) and humidity (%) from a climate sensor."""
    return _client(server, api_key).get("/api/climate", {"id": sensor_id})


@mcp.tool()
def read_ac_state(server: str, ac_id: str, api_key: Optional[str] = None) -> dict:
    """Read AC/thermostat state: system_mode, on, local_temperature, setpoints."""
    return _client(server, api_key).get("/api/ac", {"id": ac_id})


@mcp.tool()
def read_status(server: str, api_key: Optional[str] = None) -> dict:
    """High-level controller summary: counts of lights/sensors/ACs and bridges."""
    return _client(server, api_key).get("/api/status")


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

@mcp.tool()
def read_config(config_path: str) -> list:
    """Load and return the automation config (a list of entries)."""
    return _load(config_path)


@mcp.tool()
def write_config(config_path: str, entries: list) -> dict:
    """Replace the entire config file. Validates first; refuses on error."""
    errs = []
    for i, e in enumerate(entries):
        errs.extend(_validate_entry(e, i))
    if errs:
        return {"ok": False, "errors": errs}
    _save(config_path, entries)
    return {"ok": True, "entries": len(entries)}


@mcp.tool()
def validate_config(entries: list) -> dict:
    """Validate a candidate config (no write). Returns errors list (empty if ok)."""
    errs = []
    for i, e in enumerate(entries):
        errs.extend(_validate_entry(e, i))
    return {"ok": not errs, "errors": errs}


@mcp.tool()
def upsert_entry(config_path: str, entry: dict) -> dict:
    """Insert or replace an entry in the config, matched by 'id'."""
    errs = _validate_entry(entry, 0)
    if errs:
        return {"ok": False, "errors": errs}
    entries = _load(config_path)
    target_id = entry.get("id")
    for i, e in enumerate(entries):
        if e.get("id") == target_id:
            entries[i] = entry
            _save(config_path, entries)
            return {"ok": True, "action": "replaced", "id": target_id}
    entries.append(entry)
    _save(config_path, entries)
    return {"ok": True, "action": "inserted", "id": target_id}


@mcp.tool()
def remove_entry(config_path: str, device_id: str) -> dict:
    """Delete the entry with the given device id."""
    entries = _load(config_path)
    new = [e for e in entries if e.get("id") != device_id]
    if len(new) == len(entries):
        return {"ok": False, "error": f"id '{device_id}' not found"}
    _save(config_path, new)
    return {"ok": True, "removed": device_id}


# ---------------------------------------------------------------------------
# Direct device control (handy for quick experimentation)
# ---------------------------------------------------------------------------

@mcp.tool()
def set_light(server: str, light_id: str, level: int,
              kelvin: Optional[int] = None, api_key: Optional[str] = None) -> dict:
    """Set a light's level (0–100) and optional color temperature in Kelvin."""
    client = _client(server, api_key)
    matter_level = max(0, min(254, int(round(level / 100.0 * 254))))
    client.post("/api/level", {"id": light_id, "level": matter_level})
    if kelvin and kelvin > 0:
        client.post("/api/mired", {"id": light_id, "mireds": int(1_000_000 / kelvin)})
    return {"ok": True, "id": light_id, "level": level, "kelvin": kelvin}


@mcp.tool()
def set_ac(server: str, ac_id: str, on: bool, mode: str = "cool",
           setpoint: Optional[float] = None, api_key: Optional[str] = None) -> dict:
    """Set an AC: on/off, mode (cool/heat/dry/fan/auto/off), optional °C setpoint."""
    payload: dict[str, Any] = {"id": ac_id, "on": bool(on), "mode": parse_ac_mode(mode)}
    if setpoint is not None:
        payload["setpoint"] = float(setpoint)
    return _client(server, api_key).post("/api/ac", payload)


# ---------------------------------------------------------------------------
# Documentation prompt — bundles the schema so agents can author entries.
# ---------------------------------------------------------------------------

@mcp.prompt()
def config_schema() -> str:
    """Return a concise reference of light & AC entry schemas."""
    return """
Light Programmer config = JSON array of entries.

LIGHT entry:
  { "id": "dev_*", "schedule": [{"time":"HH:MM","level":0-100,"kelvin":2700-6500}, ...],
    "sensor": [{"id":"dev_*","timeout":<minutes>}], OR
    "sensor_condition": <AST: AND/OR/NOT/sensor/time_window> }
  level/kelvin are linearly interpolated between schedule points (cross-midnight ok).

AC entry (climate-driven, no schedule):
  { "id": "dev_*", "type": "ac",
    "climate_sensor":  "dev_*",                       # one sensor (omit to use AC's own thermostat)
    "climate_sensors": ["dev_*", ...],                # OR multiple sensors (any-trigger)
    "mode": "cool"|"heat"|"dry"|"fan"|"auto",
    "setpoint": <C>,
    # temperature thresholds (cool/dry/fan/auto):
    "on_above": <C>, "off_below": <C>,                # off_below < on_above
    # OR for heating mode:
    "on_below": <C>, "off_above": <C>,                # off_above > on_below
    # optional humidity thresholds (active only when sensors expose humidity):
    "humidity_above": <%>, "humidity_below": <%>,
    "on_delay_minutes": 5,                            # require continuous occupancy >= N min
    "active_window": {"start":"HH:MM","end":"HH:MM"},
    "sensor": [...] OR "sensor_condition": {...} }

AC bring-up: temp threshold met AND occupancy continuous ≥ on_delay_minutes AND in window.
AC bring-down: temp crosses off threshold OR occupancy fails OR outside window.
"""


def main():  # entry point for `light-programmer-mcp`
    mcp.run()


if __name__ == "__main__":
    main()
