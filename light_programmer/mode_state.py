"""Persistent global mode flags shared between programmer loop, HTTP API,
and MCP tools.

State is a small JSON file `{"auto": bool, "kill": bool}` written atomically
(tempfile + rename) so concurrent readers never see a torn document.
"""
import json
import os
import tempfile
import threading

DEFAULT = {"auto": True, "kill": False}
_lock = threading.Lock()


def load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT)
    return {
        "auto": bool(data.get("auto", DEFAULT["auto"])),
        "kill": bool(data.get("kill", DEFAULT["kill"])),
    }


def save(path: str, state: dict) -> dict:
    normalized = {
        "auto": bool(state.get("auto", DEFAULT["auto"])),
        "kill": bool(state.get("kill", DEFAULT["kill"])),
    }
    with _lock:
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".mode_state.", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(normalized, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return normalized


def update(path: str, **changes) -> dict:
    with _lock:
        current = load(path)
        current.update({k: bool(v) for k, v in changes.items() if v is not None})
    return save(path, current)
