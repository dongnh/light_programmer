"""Tiny stdlib HTTP server exposing /mode and /kill for the HomeKit bridge."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import mode_state


def make_server(state_path: str, host: str, port: int,
                on_change: Optional[Callable] = None,
                lights_provider: Optional[Callable] = None) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer; caller is responsible for serve_forever()
    in a background thread.

    `on_change` is invoked (no args) after every successful POST so the main
    loop can wake up and reapply state immediately.

    `lights_provider` (optional) returns the current per-light status list
    (`[{id, name, connected}, …]`) for `GET /lights`, consumed by the HomeKit
    bridge to drive one Contact Sensor per light.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default stderr noise
            logging.debug("mode_http " + fmt, *args)

        def _send(self, code: int, body: dict):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}

        def do_GET(self):  # noqa: N802
            path = self.path.rstrip("/")
            if path == "/mode":
                self._send(200, mode_state.load(state_path))
            elif path == "/lights":
                if lights_provider is None:
                    self._send(404, {"error": "lights not available"})
                else:
                    self._send(200, {"lights": lights_provider()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.rstrip("/")
            body = self._read_json()
            if path == "/mode":
                changes = {k: body[k] for k in ("auto", "kill") if k in body}
                if not changes:
                    self._send(400, {"error": "expected auto and/or kill"})
                    return
                new_state = mode_state.update(state_path, **changes)
            elif path == "/kill":
                if "kill" not in body:
                    self._send(400, {"error": "expected kill"})
                    return
                new_state = mode_state.update(state_path, kill=body["kill"])
            else:
                self._send(404, {"error": "not found"})
                return
            if on_change:
                try:
                    on_change()
                except Exception as e:  # pragma: no cover
                    logging.warning(f"on_change hook failed: {e}")
            self._send(200, new_state)

    return ThreadingHTTPServer((host, port), Handler)


def start_in_thread(state_path: str, host: str, port: int,
                    on_change: Optional[Callable] = None,
                    lights_provider: Optional[Callable] = None) -> ThreadingHTTPServer:
    server = make_server(state_path, host, port, on_change=on_change,
                         lights_provider=lights_provider)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logging.info(f"Mode HTTP server listening on {host}:{port} (state={state_path})")
    return server
