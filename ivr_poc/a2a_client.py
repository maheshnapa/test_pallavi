"""Allowlisted A2A 0.3 client: discovery, polling, continuation, and cancellation."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .config import validate_base_url
from .http_support import TransportError, request_json


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    status: str
    task_id: str | None = None
    context_id: str | None = None
    data: dict | None = None
    reason: str | None = None


class A2AClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 2,
                 poll_seconds: float = 0.1, cache_seconds: float = 300):
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.cache_seconds = cache_seconds
        self._card: dict | None = None
        self._card_expires = 0.0
        self._card_lock = threading.Lock()

    def discover(self, timeout: float = 1) -> dict:
        with self._card_lock:
            if self._card and time.monotonic() < self._card_expires:
                return self._card
            card = request_json(self.base_url + "/.well-known/agent-card.json", token=self.token, timeout=timeout)
            # The card describes an approved endpoint; it cannot redirect execution.
            if card.get("url") != self.base_url + "/a2a":
                raise ProtocolError("Agent Card endpoint is outside the configured allowlist")
            if card.get("protocolVersion") != "0.3.0" or card.get("preferredTransport") != "JSONRPC":
                raise ProtocolError("This client targets A2A 0.3.0 JSON-RPC")
            skills = card.get("skills", [])
            if not isinstance(skills, list) or not any(isinstance(s, dict) and s.get("id") == "order_status" for s in skills):
                raise ProtocolError("Agent Card does not advertise order_status")
            self._card = card
            self._card_expires = time.monotonic() + self.cache_seconds
            return card

    def rpc(self, method: str, params: dict, *, timeout: float = 1) -> dict:
        request_id = str(uuid.uuid4())
        response = request_json(self.base_url + "/a2a", token=self.token, timeout=timeout,
                                payload={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise ProtocolError("JSON-RPC response correlation failed")
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else "invalid"
            raise ProtocolError(f"A2A error {code}")
        task = response.get("result")
        if not isinstance(task, dict) or task.get("kind") != "task":
            raise ProtocolError("This specialist must return a Task")
        if not all(isinstance(task.get(key), str) and 0 < len(task[key]) <= 128 for key in ("id", "contextId")):
            raise ProtocolError("Task identifiers are invalid")
        status = task.get("status")
        if not isinstance(status, dict) or status.get("state") not in (
                "submitted", "working", "input-required", "completed", "failed", "canceled", "rejected", "auth-required"):
            raise ProtocolError("Task state is invalid")
        return task

    def cancel_task(self, task_id: str | None) -> bool:
        if not task_id:
            return False
        try:
            return self.rpc("tasks/cancel", {"id": task_id}, timeout=0.25)["status"]["state"] == "canceled"
        except (TransportError, ProtocolError):
            return False  # A local timeout never proves that remote work has stopped.

    @staticmethod
    def _data_parts(container: dict) -> list[dict]:
        parts = container.get("parts", [])
        if not isinstance(parts, list):
            raise ProtocolError("Invalid parts")
        return [part["data"] for part in parts if isinstance(part, dict) and part.get("kind") == "data"
                and isinstance(part.get("data"), dict)]

    def _completed(self, task: dict, query: dict) -> dict:
        artifacts = task.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
            raise ProtocolError("Expected one order artifact")
        parts = self._data_parts(artifacts[0])
        if len(parts) != 1:
            raise ProtocolError("Expected one structured order result")
        data = parts[0]
        if data.get("schema_version") != "1" or data.get("order_id") != query.get("order_id"):
            raise ProtocolError("Result schema or order correlation failed")
        if data.get("status") not in ("shipped", "delivered", "processing", "not_found"):
            raise ProtocolError("Invalid business status")
        if query.get("region") and data.get("region") not in (None, query["region"]):
            raise ProtocolError("Result region does not match the request")
        # Ignore remote prose. Only these validated fields can affect a spoken response.
        return {"order_id": data["order_id"], "status": data["status"]}

    def run(self, query: dict, *, message_id: str, canceled: threading.Event,
            task_id: str | None = None, context_id: str | None = None,
            on_task: Callable[[str, str], None] | None = None) -> Outcome:
        deadline = time.monotonic() + self.timeout

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError
            return seconds

        try:
            if canceled.is_set():
                return Outcome("canceled", task_id, context_id)
            self.discover(timeout=min(1, remaining()))
            message = {"kind": "message", "messageId": message_id, "role": "user",
                       "parts": [{"kind": "data", "data": query}]}
            if task_id:
                message.update(taskId=task_id, contextId=context_id)
            task = self.rpc("message/send", {"message": message,
                            "configuration": {"blocking": False, "acceptedOutputModes": ["application/json"]}},
                            timeout=remaining())
            if task_id and (task["id"] != task_id or task["contextId"] != context_id):
                raise ProtocolError("Continuation identifiers changed")
            task_id, context_id = task["id"], task["contextId"]
            if on_task:
                on_task(task_id, context_id)
            while True:
                if canceled.is_set():
                    self.cancel_task(task_id)
                    return Outcome("canceled", task_id, context_id)
                remaining()
                state = task["status"]["state"]
                if state == "completed":
                    return Outcome("completed", task_id, context_id, self._completed(task, query))
                if state == "input-required":
                    details = self._data_parts(task["status"].get("message", {}))
                    if len(details) != 1 or details[0].get("missing_field") not in ("order_id", "region"):
                        raise ProtocolError("Unsupported clarification request")
                    return Outcome("input-required", task_id, context_id, details[0])
                if state in ("failed", "rejected", "canceled", "auth-required"):
                    return Outcome("failed", task_id, context_id, reason=state)
                if canceled.wait(min(self.poll_seconds, remaining())):
                    continue
                task = self.rpc("tasks/get", {"id": task_id, "historyLength": 0}, timeout=remaining())
                if task["id"] != task_id or task["contextId"] != context_id:
                    raise ProtocolError("Polled task identifiers changed")
        except TimeoutError:
            self.cancel_task(task_id)
            return Outcome("timeout", task_id, context_id, reason="deadline")
        except TransportError as error:
            timed_out = time.monotonic() >= deadline
            self.cancel_task(task_id)
            return Outcome("timeout" if timed_out else "failed", task_id, context_id,
                           reason="deadline" if timed_out else f"transport_{error.status or 'unavailable'}")
        except (ProtocolError, TypeError, AttributeError) as error:
            self.cancel_task(task_id)
            return Outcome("failed", task_id, context_id, reason="invalid_remote_response")
