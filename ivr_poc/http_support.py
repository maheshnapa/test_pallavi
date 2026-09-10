"""Small bounded JSON HTTP transport for the local reference services."""

from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY = 64 * 1024
LOG = logging.getLogger("ivr_poc")


class TransportError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


class HttpProblem(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url: str, *, token: str = "", payload: dict | None = None,
                 timeout: float = 2.0) -> dict:
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        if len(body) > MAX_BODY:
            raise TransportError("Request exceeds the size limit.")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    try:
        # Never follow a card or API redirect carrying service credentials.
        with build_opener(NoRedirect).open(request, timeout=max(0.01, timeout)) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise TransportError("Response exceeds the size limit.")
            if response.headers.get_content_type() != "application/json":
                raise TransportError("Expected a JSON response.")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise TransportError("Expected a JSON object.")
            return parsed
    except HTTPError as error:
        error.close()
        raise TransportError(f"HTTP {error.code}", error.code) from error
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as error:
        raise TransportError("Service unavailable or request timed out.") from error
    except (ValueError, UnicodeError) as error:
        raise TransportError("Invalid JSON response.") from error


def principal_for(headers, tokens: dict[str, str]) -> str:
    supplied = headers.get("Authorization", "")
    for principal, token in tokens.items():
        if hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return principal
    raise HttpProblem(401, "A valid bearer token is required.")


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "IVRPoC/0.1"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(5.0)

    def log_message(self, format, *args):
        pass  # Access logs must not contain customer parameters or credentials.

    def read_json(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise HttpProblem(415, "Content-Type must be application/json.")
        if self.headers.get("Transfer-Encoding"):
            raise HttpProblem(400, "Chunked request bodies are not supported.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise HttpProblem(400, "Invalid Content-Length.") from error
        if size < 1 or size > MAX_BODY:
            raise HttpProblem(413, "JSON request must be between 1 and 65536 bytes.")
        raw = self.rfile.read(size)
        try:
            result = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as error:
            raise HttpProblem(400, "Invalid JSON.") from error
        if not isinstance(result, dict):
            raise HttpProblem(400, "Expected a JSON object.")
        return result

    def send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A disconnected caller must not crash the service.

    def guarded(self, action: Callable[[], Any]) -> None:
        try:
            action()
        except HttpProblem as error:
            self.send_json(error.status, {"error": str(error)})
        except (TimeoutError, socket.timeout):
            self.send_json(408, {"error": "Request timed out."})
        except Exception:
            LOG.exception("Unhandled service error")
            self.send_json(500, {"error": "Internal service error."})


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, capacity: int = 32):
        self.slots = threading.BoundedSemaphore(capacity)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def start_server(server: BoundedServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread
