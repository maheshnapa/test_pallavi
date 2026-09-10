"""Read-only order specialist implementing the documented A2A 0.3 JSON-RPC subset.

Business fixtures are deliberately fictitious. State is bounded, in memory, and
owned by an authenticated service principal. This is not an order system.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .http_support import BoundedServer, HttpProblem, JsonHandler, principal_for

TERMINAL = {"completed", "failed", "canceled", "rejected"}
ORDER_PATTERN = re.compile(r"^ORD-(?:[0-9]{4}|SLOW|FAIL|BAD)$")


class RpcProblem(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def agent_message(data: dict, context: str, task_id: str) -> dict:
    return {"kind": "message", "messageId": str(uuid.uuid4()), "role": "agent",
            "contextId": context, "taskId": task_id,
            "parts": [{"kind": "data", "data": data}]}


@dataclass
class Entry:
    task: dict
    owner: str
    query: dict
    touched: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    messages: dict[str, str] = field(default_factory=dict)


class Specialist:
    def __init__(self, *, demo_faults: bool = True, slow_seconds: float = 5,
                 normal_seconds: float = 0.08, capacity: int = 128):
        self.demo_faults = demo_faults
        self.slow_seconds = slow_seconds
        self.normal_seconds = normal_seconds
        self.capacity = capacity
        self.entries: dict[str, Entry] = {}
        self.message_index: dict[tuple[str, str], str] = {}
        self.lock = threading.RLock()
        self.workers = threading.BoundedSemaphore(16)

    def card(self, base_url: str) -> dict:
        return {
            "protocolVersion": "0.3.0", "name": "Demo Order Status Specialist",
            "description": "Read-only status lookup over fictitious orders; no customer identity verification.",
            "version": "0.1.0", "url": base_url + "/a2a", "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
            "securitySchemes": {"serviceBearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"serviceBearer": []}],
            "skills": [{"id": "order_status", "name": "Order status",
                        "description": "Look up an order_id; a regional order also requires region east or west.",
                        "tags": ["orders", "read-only", "demo"],
                        "inputModes": ["application/json"], "outputModes": ["application/json"]}],
        }

    def _entry(self, task_id, owner: str) -> Entry:
        if not isinstance(task_id, str) or task_id not in self.entries:
            raise RpcProblem(-32001, "Task not found")
        entry = self.entries[task_id]
        if entry.owner != owner:
            raise RpcProblem(-32001, "Task not found")
        entry.touched = time.monotonic()
        return entry

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [task_id for task_id, entry in self.entries.items()
                   if now - entry.touched > 300 and entry.task["status"]["state"] in TERMINAL | {"input-required"}]
        for task_id in expired:
            entry = self.entries.pop(task_id)
            for message_id in entry.messages:
                self.message_index.pop((entry.owner, message_id), None)

    def _status(self, entry: Entry, state: str, details: dict | None = None) -> None:
        status = {"state": state, "timestamp": timestamp()}
        if details:
            status["message"] = agent_message(details, entry.task["contextId"], entry.task["id"])
        entry.task["status"] = status
        entry.touched = time.monotonic()

    def _schedule(self, entry: Entry) -> None:
        query = entry.query
        missing = "order_id" if not query.get("order_id") else None
        if query.get("order_id") == "ORD-2001" and not query.get("region"):
            missing = "region"
        if missing:
            self._status(entry, "input-required", {"missing_field": missing})
            return
        if not self.workers.acquire(blocking=False):
            self._status(entry, "failed", {"reason": "capacity"})
            return
        self._status(entry, "working")
        threading.Thread(target=self._work, args=(entry,), daemon=True).start()

    def _work(self, entry: Entry) -> None:
        try:
            order_id = entry.query["order_id"]
            delay = self.slow_seconds if self.demo_faults and order_id == "ORD-SLOW" else self.normal_seconds
            if entry.cancel.wait(delay):
                return
            with self.lock:
                if entry.task["status"]["state"] in TERMINAL:
                    return
                if self.demo_faults and order_id == "ORD-FAIL":
                    self._status(entry, "failed", {"reason": "demo_backend_unavailable"})
                    return
                result = {"schema_version": "1", "order_id": order_id, "status": "not_found"}
                if order_id == "ORD-1001":
                    result.update(status="shipped", detail="Your order is with the delivery carrier.")
                elif order_id == "ORD-1002":
                    result.update(status="delivered", detail="Your order has been delivered.")
                elif order_id == "ORD-2001":
                    result.update(status="processing", region=entry.query["region"])
                elif self.demo_faults and order_id == "ORD-SLOW":
                    result.update(status="processing")
                elif self.demo_faults and order_id == "ORD-BAD":
                    result = {"order_id": order_id, "status": {"unexpected": "object"}}
                entry.task["artifacts"] = [{"artifactId": str(uuid.uuid4()), "name": "order_status",
                                            "parts": [{"kind": "data", "data": result}]}]
                self._status(entry, "completed")
        finally:
            self.workers.release()

    @staticmethod
    def _query(message: dict) -> dict:
        if message.get("role") != "user" or message.get("kind") != "message":
            raise RpcProblem(-32602, "A user message with kind message is required")
        parts = message.get("parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            raise RpcProblem(-32602, "Exactly one data part is required")
        part = parts[0]
        if part.get("kind") != "data" or not isinstance(part.get("data"), dict):
            raise RpcProblem(-32005, "Use application/json data parts")
        query = part["data"]
        if set(query) - {"order_id", "region"}:
            raise RpcProblem(-32602, "Unexpected business input field")
        order_id, region = query.get("order_id"), query.get("region")
        if order_id is not None and (not isinstance(order_id, str) or not ORDER_PATTERN.fullmatch(order_id)):
            raise RpcProblem(-32602, "Invalid order_id")
        if region is not None and region not in ("east", "west"):
            raise RpcProblem(-32602, "Region must be east or west")
        return dict(query)

    def send(self, params: dict, owner: str) -> dict:
        message = params.get("message")
        if not isinstance(message, dict):
            raise RpcProblem(-32602, "message is required")
        query = self._query(message)
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or not (1 <= len(message_id) <= 128):
            raise RpcProblem(-32602, "messageId is required and must be at most 128 characters")
        configuration = params.get("configuration", {})
        if not isinstance(configuration, dict):
            raise RpcProblem(-32602, "Invalid configuration")
        if configuration.get("pushNotificationConfig") is not None:
            raise RpcProblem(-32003, "Push notifications are not supported")
        modes = configuration.get("acceptedOutputModes", ["application/json"])
        if not isinstance(modes, list) or not all(isinstance(mode, str) for mode in modes):
            raise RpcProblem(-32602, "acceptedOutputModes must be a list of strings")
        if "application/json" not in modes:
            raise RpcProblem(-32005, "Only application/json output is supported")
        if configuration.get("blocking") is True:
            raise RpcProblem(-32004, "Use blocking false and tasks/get")
        fingerprint = json.dumps(message, sort_keys=True, separators=(",", ":"))
        with self.lock:
            self._purge()
            previous = self.message_index.get((owner, message_id))
            if previous:
                entry = self._entry(previous, owner)
                if entry.messages[message_id] != fingerprint:
                    raise RpcProblem(-32602, "messageId reused with different content")
                return copy.deepcopy(entry.task)
            task_id = message.get("taskId")
            if task_id:
                entry = self._entry(task_id, owner)
                if entry.task["status"]["state"] != "input-required":
                    raise RpcProblem(-32004, "Only input-required tasks can be resumed")
                if message.get("contextId") != entry.task["contextId"]:
                    raise RpcProblem(-32602, "contextId does not match the task")
                if len(entry.messages) >= 8:
                    raise RpcProblem(-32004, "Task continuation limit reached")
                if query.get("order_id") and entry.query.get("order_id") not in (None, query["order_id"]):
                    raise RpcProblem(-32602, "Start a new task to change the order")
                entry.query.update({key: value for key, value in query.items() if value is not None})
                entry.task["history"].append(copy.deepcopy(message))
            else:
                if len(self.entries) >= self.capacity:
                    raise RpcProblem(-32004, "Task capacity reached; try again later")
                if message.get("contextId"):
                    raise RpcProblem(-32004, "This specialist requires a new context for each new task")
                task_id, context = str(uuid.uuid4()), str(uuid.uuid4())
                task = {"kind": "task", "id": task_id, "contextId": context,
                        "status": {"state": "submitted", "timestamp": timestamp()},
                        "history": [copy.deepcopy(message)]}
                entry = Entry(task=task, owner=owner, query=query)
                self.entries[task_id] = entry
            entry.messages[message_id] = fingerprint
            self.message_index[(owner, message_id)] = task_id
            self._schedule(entry)
            return copy.deepcopy(entry.task)

    def dispatch(self, request: dict, owner: str) -> dict:
        request_id = request.get("id")
        try:
            if request.get("jsonrpc") != "2.0" or type(request_id) not in (str, int):
                raise RpcProblem(-32600, "JSON-RPC 2.0 and a string or integer id are required")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RpcProblem(-32602, "params must be an object")
            method = request.get("method")
            if method == "message/send":
                task = self.send(params, owner)
            elif method in ("tasks/get", "tasks/cancel"):
                with self.lock:
                    self._purge()
                    entry = self._entry(params.get("id"), owner)
                    if method == "tasks/cancel":
                        state = entry.task["status"]["state"]
                        if state in TERMINAL and state != "canceled":
                            raise RpcProblem(-32002, "Task is already terminal")
                        entry.cancel.set()
                        self._status(entry, "canceled")
                    task = copy.deepcopy(entry.task)
                    history_length = params.get("historyLength")
                    if history_length is not None:
                        if type(history_length) is not int or not 0 <= history_length <= 8:
                            raise RpcProblem(-32602, "historyLength must be 0–8")
                        task["history"] = task.get("history", [])[-history_length:] if history_length else []
            else:
                raise RpcProblem(-32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": task}
        except RpcProblem as error:
            return {"jsonrpc": "2.0", "id": request_id if type(request_id) in (str, int) else None,
                    "error": {"code": error.code, "message": str(error)}}

    def close(self) -> None:
        with self.lock:
            for entry in self.entries.values():
                if entry.task["status"]["state"] not in TERMINAL:
                    entry.cancel.set()
                    self._status(entry, "canceled")


def make_specialist_server(host: str, port: int, *, tokens: dict[str, str],
                           service: Specialist | None = None, advertised_url: str | None = None) -> BoundedServer:
    specialist = service or Specialist()

    class Handler(JsonHandler):
        def do_GET(self):
            def action():
                if self.path == "/health":
                    self.send_json(200, {"status": "ok", "service": "specialist", "protocol": "A2A 0.3.0 subset"})
                elif self.path == "/.well-known/agent-card.json":
                    base = advertised_url or f"http://127.0.0.1:{self.server.server_port}"
                    self.send_json(200, specialist.card(base))
                else:
                    raise HttpProblem(404, "Not found")
            self.guarded(action)

        def do_POST(self):
            def action():
                if self.path != "/a2a":
                    raise HttpProblem(404, "Not found")
                owner = principal_for(self.headers, tokens)
                try:
                    request = self.read_json()
                except HttpProblem as error:
                    if error.status == 400 and str(error) == "Invalid JSON.":
                        self.send_json(200, {"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32700, "message": "Parse error"}})
                        return
                    raise
                self.send_json(200, specialist.dispatch(request, owner))
            self.guarded(action)

    server = BoundedServer((host, port), Handler)
    server.specialist = specialist
    return server
