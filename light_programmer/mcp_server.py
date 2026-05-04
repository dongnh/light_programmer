"""MCP server for Light Programmer.

Exposes tools for AI agents to discover Matter devices, inspect current state,
manage launchd services (programmer + matter controller), tail their logs,
read/write the automation config, and control devices directly.

Run:
    pip install light-programmer[mcp]
    light-programmer-mcp                                      # stdio (for Claude Desktop / Code)
    light-programmer-mcp --transport http --host 0.0.0.0 --port 7860   # LAN access
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
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
# Process / log management (launchd, macOS)
# ---------------------------------------------------------------------------

# Override via env: LP_MCP_LABELS="home.lighting.programmer,home.lighting.matter"
_DEFAULT_LABELS = ("home.lighting.programmer,home.lighting.matter,"
                   "home.lighting.mcp_programmer,home.lighting.mcp_matter")
ALLOWED_LABELS = {s.strip() for s in os.environ.get("LP_MCP_LABELS", _DEFAULT_LABELS).split(",") if s.strip()}

# Map label -> log path. Override via env: LP_MCP_LOG_<label_uppercased_with_underscores>
_DEFAULT_LOGS = {
    "home.lighting.programmer":     "/Users/panda/lighting/logs/programmer.log",
    "home.lighting.matter":         "/Users/panda/lighting/logs/matter.log",
    "home.lighting.mcp_programmer": "/Users/panda/lighting/logs/mcp_programmer.log",
    "home.lighting.mcp_matter":     "/Users/panda/lighting/logs/mcp_matter.log",
}
LOG_PATHS = {
    label: os.environ.get(f"LP_MCP_LOG_{label.upper().replace('.', '_')}", default)
    for label, default in _DEFAULT_LOGS.items()
}


def _parse_launchctl_list(text: str) -> dict:
    pid, last_exit = None, None
    for line in text.splitlines():
        s = line.strip().rstrip(";")
        if s.startswith('"PID" = '):
            try: pid = int(s.split("=", 1)[1].strip())
            except ValueError: pass
        elif s.startswith('"LastExitStatus" = '):
            try: last_exit = int(s.split("=", 1)[1].strip())
            except ValueError: pass
    return {"pid": pid, "last_exit_status": last_exit}


@mcp.tool()
def list_managed_services() -> list[str]:
    """List launchd labels this MCP server is allowed to inspect/restart."""
    return sorted(ALLOWED_LABELS)


@mcp.tool()
def get_service_status(label: str) -> dict:
    """Get launchd status for a managed service. PID null = not running."""
    if label not in ALLOWED_LABELS:
        return {"ok": False, "error": f"label '{label}' not allowed", "allowed": sorted(ALLOWED_LABELS)}
    rc = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=5)
    if rc.returncode != 0:
        return {"ok": False, "label": label, "stderr": rc.stderr.strip()}
    parsed = _parse_launchctl_list(rc.stdout)
    return {"ok": True, "label": label, "running": parsed["pid"] is not None, **parsed}


@mcp.tool()
def restart_service(label: str) -> dict:
    """Kickstart-restart a managed launchd service (`launchctl kickstart -k`).

    Returns post-restart status after a short settle delay so callers can confirm
    the service came back up. Note: restarting `home.lighting.matter` will cause
    the programmer to lose its API connection briefly; KeepAlive will respawn it.
    """
    if label not in ALLOWED_LABELS:
        return {"ok": False, "error": f"label '{label}' not allowed", "allowed": sorted(ALLOWED_LABELS)}
    uid = os.getuid()
    rc = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True, text=True, timeout=10,
    )
    import time
    time.sleep(1.5)
    status = get_service_status(label)
    return {"ok": rc.returncode == 0, "label": label,
            "stdout": rc.stdout.strip(), "stderr": rc.stderr.strip(),
            "post_restart": status}


@mcp.tool()
def read_log(label: str, lines: int = 50, grep: Optional[str] = None) -> str:
    """Tail the log file for a managed service. `grep` is a regex; non-matching
    lines are filtered out. `lines` is clamped to [1, 2000]."""
    path = LOG_PATHS.get(label)
    if path is None:
        return f"label '{label}' not in log map; allowed: {sorted(LOG_PATHS)}"
    n = max(1, min(2000, int(lines)))
    rc = subprocess.run(["tail", "-n", str(n), path], capture_output=True, text=True, timeout=5)
    if rc.returncode != 0:
        return f"tail failed: {rc.stderr.strip()}"
    text = rc.stdout
    if grep:
        try:
            pat = re.compile(grep)
        except re.error as e:
            return f"invalid regex: {e}"
        text = "\n".join(line for line in text.splitlines() if pat.search(line))
    return text


# ---------------------------------------------------------------------------
# Repo management — remote git pull + reinstall + restart
# ---------------------------------------------------------------------------

# Override via env: LP_MCP_REPO_<NAME>_PATH, LP_MCP_REPO_<NAME>_EXTRA, LP_MCP_REPO_<NAME>_RESTART (comma-separated labels)
_DEFAULT_REPOS = {
    "light_programmer": {
        "path":    "/Users/panda/lighting/src/light_programmer",
        "extra":   "[mcp]",
        "package": "light-programmer",
        "restart": ["home.lighting.programmer", "home.lighting.mcp_programmer"],
    },
    "matter_webcontrol": {
        "path":    "/Users/panda/lighting/src/matter_webcontrol",
        "extra":   "",
        "package": "matter-web-controller",
        "restart": ["home.lighting.matter", "home.lighting.mcp_matter"],
    },
}


def _repo_config(name: str) -> Optional[dict]:
    if name not in _DEFAULT_REPOS:
        return None
    cfg = dict(_DEFAULT_REPOS[name])
    cfg["path"]    = os.environ.get(f"LP_MCP_REPO_{name.upper()}_PATH",    cfg["path"])
    cfg["extra"]   = os.environ.get(f"LP_MCP_REPO_{name.upper()}_EXTRA",   cfg["extra"])
    restart_env = os.environ.get(f"LP_MCP_REPO_{name.upper()}_RESTART")
    if restart_env is not None:
        cfg["restart"] = [s.strip() for s in restart_env.split(",") if s.strip()]
    cfg["pip"] = os.environ.get("LP_MCP_PIP", "/Users/panda/lighting/.venv-matter/bin/pip")
    return cfg


def _git(args: list, cwd: str, timeout: int = 60) -> dict:
    rc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return {"cmd": "git " + " ".join(args), "rc": rc.returncode,
            "stdout": rc.stdout.strip(), "stderr": rc.stderr.strip()}


def _pip_version(pip: str, package: str) -> Optional[str]:
    """Return the installed version string for `package` (or None if not installed)."""
    rc = subprocess.run([pip, "show", package], capture_output=True, text=True, timeout=10)
    if rc.returncode != 0:
        return None
    for line in rc.stdout.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


@mcp.tool()
def list_managed_repos() -> list[str]:
    """Repositories this MCP server can pull/install/restart on the server."""
    return sorted(_DEFAULT_REPOS)


@mcp.tool()
def get_repo_status(name: str) -> dict:
    """Show current branch, HEAD commit, and any uncommitted/unpushed changes."""
    cfg = _repo_config(name)
    if cfg is None:
        return {"ok": False, "error": f"unknown repo '{name}'", "allowed": sorted(_DEFAULT_REPOS)}
    if not os.path.isdir(os.path.join(cfg["path"], ".git")):
        return {"ok": False, "error": f"not a git repo: {cfg['path']}"}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cfg["path"])
    head   = _git(["log", "-1", "--oneline"], cfg["path"])
    status = _git(["status", "-s"], cfg["path"])
    upstream = _git(["rev-list", "--left-right", "--count", "@{u}...HEAD"], cfg["path"])
    behind, ahead = (None, None)
    if upstream["rc"] == 0 and upstream["stdout"]:
        try:
            behind, ahead = [int(x) for x in upstream["stdout"].split()]
        except ValueError:
            pass
    described = _git(["describe", "--tags", "--always", "--dirty"], cfg["path"])
    installed = _pip_version(cfg["pip"], cfg.get("package", ""))
    return {
        "ok": True, "name": name, "path": cfg["path"],
        "branch": branch["stdout"], "head": head["stdout"],
        "describe": described["stdout"],
        "installed_version": installed,
        "dirty": bool(status["stdout"]),
        "behind_remote": behind, "ahead_remote": ahead,
    }


@mcp.tool()
def list_repo_tags(name: str, limit: int = 20) -> dict:
    """List the most recent N tags published to the repo's remote, newest first."""
    cfg = _repo_config(name)
    if cfg is None:
        return {"ok": False, "error": f"unknown repo '{name}'"}
    fetch = _git(["fetch", "--tags", "origin"], cfg["path"], timeout=120)
    listed = _git(["tag", "--sort=-v:refname"], cfg["path"])
    if listed["rc"] != 0:
        return {"ok": False, "error": "git tag failed", "fetch": fetch, "listing": listed}
    tags = [t for t in listed["stdout"].splitlines() if t][: max(1, int(limit))]
    return {"ok": True, "name": name, "tags": tags}


@mcp.tool()
def update_repo(name: str, ref: Optional[str] = None,
                restart: bool = True, reinstall: bool = True) -> dict:
    """Pull or check out a specific ref, reinstall, optionally restart services.

    Args:
        name: One of `list_managed_repos()`.
        ref:  Optional git ref — a tag (e.g. `v0.4.1`), branch, or commit SHA.
              If omitted, fast-forwards the current branch from `origin`.
        restart: Restart related launchd services after install (default True).
        reinstall: Re-run `pip install -e` (default True).

    Notes:
      - Refuses to pull a dirty tree (uncommitted changes); fix manually first.
      - For a `ref` that's a tag/commit, the working tree ends in detached HEAD
        — that's fine for deployment; call `update_repo(name)` again later to
        return to the tip of the tracking branch.
      - When restarting `home.lighting.mcp_programmer` (this very service), the
        kickstart is delayed 2 s so the response can return before the worker
        dies; KeepAlive respawns it.
    """
    cfg = _repo_config(name)
    if cfg is None:
        return {"ok": False, "error": f"unknown repo '{name}'", "allowed": sorted(_DEFAULT_REPOS)}
    if not os.path.isdir(os.path.join(cfg["path"], ".git")):
        return {"ok": False, "error": f"not a git repo: {cfg['path']}"}

    steps: list[dict] = []

    dirty = _git(["status", "--porcelain"], cfg["path"])
    steps.append({"step": "git status", **dirty})
    if dirty["rc"] != 0:
        return {"ok": False, "error": "git status failed", "steps": steps}
    if dirty["stdout"]:
        return {"ok": False, "error": "working tree is dirty; refusing to pull",
                "uncommitted": dirty["stdout"], "steps": steps}

    fetch = _git(["fetch", "--tags", "origin"], cfg["path"], timeout=120)
    steps.append({"step": "git fetch", **fetch})
    if fetch["rc"] != 0:
        return {"ok": False, "error": "git fetch failed", "steps": steps}

    if ref:
        # Verify ref exists, then check out (detached for tags/SHAs is fine).
        verify = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cfg["path"])
        steps.append({"step": "rev-parse ref", **verify})
        if verify["rc"] != 0:
            return {"ok": False, "error": f"ref '{ref}' not found", "steps": steps}
        co = _git(["checkout", "--detach", ref], cfg["path"])
        steps.append({"step": f"git checkout {ref}", **co})
        if co["rc"] != 0:
            return {"ok": False, "error": "checkout failed", "steps": steps}
    else:
        pull = _git(["pull", "--ff-only", "origin", "HEAD"], cfg["path"], timeout=120)
        steps.append({"step": "git pull --ff-only", **pull})
        if pull["rc"] != 0:
            return {"ok": False, "error": "git pull failed", "steps": steps}

    if reinstall:
        target = cfg["path"] + cfg["extra"]
        rc = subprocess.run(
            [cfg["pip"], "install", "-e", target, "--quiet"],
            capture_output=True, text=True, timeout=180,
        )
        steps.append({"step": "pip install -e", "rc": rc.returncode,
                      "stdout": rc.stdout.strip()[-2000:], "stderr": rc.stderr.strip()[-2000:]})
        if rc.returncode != 0:
            return {"ok": False, "error": "pip install failed", "steps": steps}

    if restart:
        uid = os.getuid()
        own_label = "home.lighting.mcp_programmer"
        for label in cfg["restart"]:
            if label not in ALLOWED_LABELS:
                steps.append({"step": f"skip restart {label}", "reason": "label not whitelisted"})
                continue
            if label == own_label:
                # Defer self-restart so the response can return.
                subprocess.Popen(
                    ["bash", "-c",
                     f"sleep 2 && launchctl kickstart -k gui/{uid}/{label}"],
                    start_new_session=True,
                )
                steps.append({"step": f"restart {label}", "deferred": True, "delay_s": 2})
            else:
                rc = subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                    capture_output=True, text=True, timeout=10,
                )
                steps.append({"step": f"restart {label}", "rc": rc.returncode,
                              "stdout": rc.stdout.strip(), "stderr": rc.stderr.strip()})

    return {"ok": True, "name": name,
            "head": _git(['log', '-1', '--oneline'], cfg["path"])['stdout'],
            "describe": _git(['describe', '--tags', '--always'], cfg["path"])['stdout'],
            "installed_version": _pip_version(cfg["pip"], cfg.get("package", "")),
            "steps": steps}


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
    p = argparse.ArgumentParser(description="Light Programmer MCP server")
    p.add_argument("--transport", default="stdio", choices=["stdio", "sse", "http"],
                   help="Transport: stdio for local AI agents (default), sse/http for LAN access.")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind host for sse/http (default 127.0.0.1; use 0.0.0.0 for LAN).")
    p.add_argument("--port", type=int, default=7860,
                   help="Bind port for sse/http (default 7860).")
    args = p.parse_args()

    if args.transport == "stdio":
        mcp.run()
        return

    # FastMCP bind address.
    try:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    except AttributeError:
        logging.warning("FastMCP.settings unavailable; relying on transport defaults.")

    # MCP SDK enables DNS-rebinding protection by default, which only trusts
    # 127.0.0.1/localhost. For LAN deployments the operator is explicitly
    # opting in to network exposure, so we relax the host/origin filter.
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    except Exception as e:  # pragma: no cover
        logging.warning(f"Could not relax transport_security ({e}); "
                        f"requests may be rejected with 'Invalid Host header'.")

    transport_name = "streamable-http" if args.transport == "http" else "sse"
    logging.info(f"Starting MCP transport={transport_name} bind={args.host}:{args.port}")
    mcp.run(transport=transport_name)


if __name__ == "__main__":
    main()
