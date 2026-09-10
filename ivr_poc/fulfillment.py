"""Dialogflow CX fulfillment adapter and per-call task lifecycle.

No LLM routing is used. CX selects the fulfillment tag and extracts parameters.
Only the allowlisted order_status tag can delegate to the specialist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from .a2a_client import A2AClient, Outcome
from .http_support import BoundedServer, HttpProblem, JsonHandler, principal_for
from .specialist import ORDER_PATTERN

LOG = logging.getLogger("ivr_poc")
FALLBACK = "I couldn't retrieve the order status right now. Please try again shortly."


@dataclass
class Turn:
    turn_id: str
    fingerprint: str = ""
    started: bool = False
    canceled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    task_id: str | None = None


@dataclass
class Session:
    current: Turn | None = None
    turns: dict[str, Turn] = field(default_factory=dict)
    continuation: tuple[str, str, dict] | None = None
    touched: float = field(default_factory=time.monotonic)


def webhook_response(text: str | None, status: str, *, task_id: str | None = None,
                     context_id: str | None = None, missing: str | None = None) -> dict:
    messages = [{"text": {"text": [text]}}] if text else []
    return {"fulfillmentResponse": {"messages": messages, "mergeBehavior": "REPLACE"},
            "sessionInfo": {"parameters": {"a2a_status": status, "a2a_task_id": task_id,
                                            "a2a_context_id": context_id, "a2a_missing_field": missing}}}


class Fulfillment:
    def __init__(self, client: A2AClient, *, capacity: int = 128):
        self.client = client
        self.capacity = capacity
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()

    def _session(self, session_id: str) -> Session:
        now = time.monotonic()
        expired = [key for key, session in self.sessions.items() if now - session.touched > 300
                   and (session.current is None or session.current.done.is_set() or not session.current.started)]
        for key in expired:
            self.sessions.pop(key)
        if session_id not in self.sessions:
            if len(self.sessions) >= self.capacity:
                raise HttpProblem(503, "Session capacity reached; try again later.")
            self.sessions[session_id] = Session()
        session = self.sessions[session_id]
        session.touched = now
        return session

    @staticmethod
    def _identifiers(session_id, turn_id) -> None:
        if not isinstance(session_id, str) or not (1 <= len(session_id) <= 256):
            raise HttpProblem(400, "A session identifier of 1–256 characters is required.")
        if not isinstance(turn_id, str) or not (1 <= len(turn_id) <= 128):
            raise HttpProblem(400, "A turn identifier of 1–128 characters is required.")

    def start_turn(self, session_id: str, turn_id: str) -> dict:
        """Register a voice turn before CX; late webhooks from older turns are ignored."""
        self._identifiers(session_id, turn_id)
        with self.lock:
            session = self._session(session_id)
            if turn_id in session.turns:
                return {"status": "registered", "turn_id": turn_id}
            if session.current:
                session.current.canceled.set()
            turn = Turn(turn_id)
            session.current = turn
            session.turns[turn_id] = turn
            self._trim(session)
            return {"status": "registered", "turn_id": turn_id}

    @staticmethod
    def _trim(session: Session) -> None:
        # Replay protection is intentionally bounded to recent turns in this PoC.
        for key in list(session.turns):
            if len(session.turns) <= 32:
                break
            if session.turns[key] is not session.current and (session.turns[key].done.is_set()
                                                             or not session.turns[key].started):
                session.turns.pop(key)

    def cancel(self, session_id: str, turn_id: str, *, reason: str = "hangup") -> dict:
        self._identifiers(session_id, turn_id)
        if reason not in ("hangup", "interruption"):
            raise HttpProblem(400, "Cancellation reason must be hangup or interruption.")
        task_id = None
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.current is None or session.current.turn_id != turn_id:
                return {"status": "ignored", "reason": "not_current_turn"}
            session.current.canceled.set()
            if reason == "interruption" and session.current.done.is_set() and session.continuation:
                # Speaking an answer to the specialist's question is a new voice
                # turn, but must not cancel the task waiting for that answer.
                return {"status": "canceled_locally", "remote_cancel_acknowledged": False,
                        "continuation_preserved": True}
            task_id = session.current.task_id
            if session.continuation:
                task_id = task_id or session.continuation[0]
            session.continuation = None
        acknowledged = self.client.cancel_task(task_id)
        return {"status": "canceled_locally", "remote_cancel_acknowledged": acknowledged}

    def handle(self, payload: dict) -> dict:
        info = payload.get("sessionInfo")
        fulfillment = payload.get("fulfillmentInfo")
        if not isinstance(info, dict) or not isinstance(fulfillment, dict):
            raise HttpProblem(400, "sessionInfo and fulfillmentInfo are required.")
        session_id = info.get("session")
        custom = payload.get("payload") or {}
        if not isinstance(custom, dict):
            raise HttpProblem(400, "payload must be an object.")
        registered = bool(custom.get("ivr_turn_id"))
        turn_id = custom.get("ivr_turn_id") or payload.get("detectIntentResponseId") or str(uuid.uuid4())
        self._identifiers(session_id, turn_id)
        if fulfillment.get("tag") != "order_status":
            raise HttpProblem(400, "Unsupported fulfillment tag; expected order_status.")
        parameters = info.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise HttpProblem(400, "sessionInfo.parameters must be an object.")
        query = {key: parameters[key] for key in ("order_id", "region") if parameters.get(key) not in (None, "")}
        if "order_id" in query:
            if not isinstance(query["order_id"], str) or not ORDER_PATTERN.fullmatch(query["order_id"].upper()):
                return webhook_response("Please give an order number such as ORD-1001.", "invalid-input", missing="order_id")
            query["order_id"] = query["order_id"].upper()
        if "region" in query:
            if not isinstance(query["region"], str) or query["region"].lower() not in ("east", "west"):
                return webhook_response("Is the order for the east or west region?", "invalid-input", missing="region")
            query["region"] = query["region"].lower()
        fingerprint = json.dumps(query, sort_keys=True)
        abandoned_task = None
        with self.lock:
            session = self._session(session_id)
            existing = session.turns.get(turn_id)
            if registered and existing is None:
                raise HttpProblem(409, "Register ivr_turn_id before calling CX.")
            if existing and session.current is not existing:
                return webhook_response(None, "superseded")
            if existing and existing.canceled.is_set():
                return webhook_response(None, "superseded")
            if existing and existing.started:
                if existing.fingerprint != fingerprint:
                    raise HttpProblem(409, "Turn identifier reused with different input.")
                duplicate = existing
                turn = None
                continuation = None
            else:
                duplicate = None
                if existing is None:
                    if session.current:
                        session.current.canceled.set()
                    existing = Turn(turn_id)
                    session.current = existing
                    session.turns[turn_id] = existing
                turn = existing
                turn.started = True
                turn.fingerprint = fingerprint
                continuation = session.continuation
                if (continuation and continuation[2].get("order_id") is not None
                        and query.get("order_id") not in (None, continuation[2]["order_id"])):
                    abandoned_task = continuation[0]
                    continuation = None
                if continuation:
                    query = {**continuation[2], **query}
                session.continuation = None
                self._trim(session)
        if duplicate:
            if not duplicate.done.wait(self.client.timeout + 1):
                return webhook_response(FALLBACK, "timeout")
            return duplicate.response or webhook_response(None, "superseded")

        assert turn is not None
        if abandoned_task:
            self.client.cancel_task(abandoned_task)
        began = time.monotonic()
        task_id = continuation[0] if continuation else None
        context_id = continuation[1] if continuation else None

        def track(task: str, context: str) -> None:
            with self.lock:
                turn.task_id = task

        try:
            # A deterministic business message ID makes a CX retry reuse the same A2A message.
            message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id + ":" + turn_id))
            outcome = self.client.run(query, message_id=message_id, canceled=turn.canceled,
                                      task_id=task_id, context_id=context_id, on_task=track)
            response = self._render(outcome)
            with self.lock:
                session = self.sessions[session_id]
                if session.current is not turn or turn.canceled.is_set():
                    response = webhook_response(None, "superseded")
                elif outcome.status == "input-required":
                    session.continuation = (outcome.task_id, outcome.context_id, query)
                turn.response = response
                turn.done.set()
            LOG.info(json.dumps({"event": "delegation_complete", "turn_id": turn_id,
                                 "session_hash": hashlib.sha256(session_id.encode()).hexdigest()[:12],
                                 "task_id": outcome.task_id, "status": response["sessionInfo"]["parameters"]["a2a_status"],
                                 "reason": outcome.reason, "elapsed_ms": round((time.monotonic() - began) * 1000)}))
            return response
        except Exception:
            with self.lock:
                turn.response = webhook_response(FALLBACK, "failed")
                turn.done.set()
            raise

    @staticmethod
    def _render(outcome: Outcome) -> dict:
        missing = None
        if outcome.status == "completed":
            data = outcome.data or {}
            templates = {"shipped": "Order {order_id} has shipped and is with the delivery carrier.",
                         "delivered": "Order {order_id} has been delivered.",
                         "processing": "Order {order_id} is being processed.",
                         "not_found": "I couldn't find order {order_id}. Please check the order number."}
            text = templates[data["status"]].format(order_id=data["order_id"])
        elif outcome.status == "input-required":
            missing = outcome.data["missing_field"]
            text = "What is your order number?" if missing == "order_id" else "Is the order for the east or west region?"
        elif outcome.status == "canceled":
            text = None
        else:
            text = FALLBACK
        return webhook_response(text, outcome.status, task_id=outcome.task_id,
                                context_id=outcome.context_id, missing=missing)


def make_fulfillment_server(host: str, port: int, *, token: str, service: Fulfillment) -> BoundedServer:
    class Handler(JsonHandler):
        def do_GET(self):
            def action():
                if self.path != "/health":
                    raise HttpProblem(404, "Not found")
                self.send_json(200, {"status": "ok", "service": "fulfillment"})
            self.guarded(action)

        def do_POST(self):
            def action():
                principal_for(self.headers, {"cx": token})
                body = self.read_json()
                if self.path == "/webhook/dialogflow-cx":
                    result = service.handle(body)
                elif self.path in ("/api/turns/start", "/api/turns/cancel"):
                    if self.path.endswith("start"):
                        result = service.start_turn(body.get("session_id"), body.get("turn_id"))
                    else:
                        result = service.cancel(body.get("session_id"), body.get("turn_id"), reason=body.get("reason", "hangup"))
                else:
                    raise HttpProblem(404, "Not found")
                self.send_json(200, result)
            self.guarded(action)

    return BoundedServer((host, port), Handler)
