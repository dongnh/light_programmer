"""Tiny stdlib HTTP server exposing /mode and /kill for the HomeKit bridge."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import mode_state


def make_server(state_path: str, host: str, port: int,
                on_change: Optional[Callable] = None,
                lights_provider: Optional[Callable] = None,
                api_key: Optional[str] = None) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer; caller is responsible for serve_forever()
    in a background thread.

    `on_change` is invoked (no args) after every successful POST so the main
    loop can wake up and reapply state immediately.

    `lights_provider` (optional) returns the current per-light status list
    (`[{id, name, connected}, …]`) for `GET /lights`, consumed by the HomeKit
    bridge to drive one Contact Sensor per light.

    `api_key` (optional): when set, every request must carry a matching
    `X-API-Key` header or it gets 401; unset = unauthenticated, fine for loopback.
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

        def _authed(self) -> bool:
            """True if no key is configured, or the request carries a matching
            X-API-Key. On mismatch, emits 401 and returns False."""
            if not api_key:
                return True
            if self.headers.get("X-API-Key") == api_key:
                return True
            logging.warning("mode_http: rejected unauthenticated %s %s from %s",
                            self.command, self.path, self.client_address[0])
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._authed():
                return
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
            if not self._authed():
                return
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
                    lights_provider: Optional[Callable] = None,
                    api_key: Optional[str] = None) -> ThreadingHTTPServer:
    if not api_key and host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("Mode HTTP server bound to non-loopback host %s with NO "
                        "X-API-Key — /mode, /kill and /lights are unauthenticated "
                        "and reachable on the LAN. Set --mode-http-key / "
                        "LP_MODE_HTTP_KEY.", host)
    server = make_server(state_path, host, port, on_change=on_change,
                         lights_provider=lights_provider, api_key=api_key)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logging.info(f"Mode HTTP server listening on {host}:{port} (state={state_path}, "
                 f"auth={'on' if api_key else 'off'})")
    return server
