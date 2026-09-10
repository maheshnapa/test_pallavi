"""Executable demo scenarios over real loopback HTTP, with simulated CX requests."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from .a2a_client import A2AClient
from .fulfillment import Fulfillment, make_fulfillment_server
from .http_support import request_json, start_server
from .specialist import Specialist, make_specialist_server


def cx_request(session: str, order_id: str | None = None, *, region: str | None = None,
               turn_id: str | None = None, registered: bool = False) -> dict:
    parameters = {}
    if order_id:
        parameters["order_id"] = order_id
    if region:
        parameters["region"] = region
    turn = turn_id or str(uuid.uuid4())
    payload = {"detectIntentResponseId": turn, "fulfillmentInfo": {"tag": "order_status"},
               "sessionInfo": {"session": session, "parameters": parameters}}
    if registered:
        payload["payload"] = {"ivr_turn_id": turn}
    return payload


def speech(response: dict) -> str:
    return " ".join(text for message in response["fulfillmentResponse"]["messages"]
                    for text in message.get("text", {}).get("text", []))


def status(response: dict) -> str:
    return response["sessionInfo"]["parameters"]["a2a_status"]


class LocalStack(AbstractContextManager):
    """Two TCP services in one process, on ephemeral ports by default."""

    def __init__(self, *, fulfillment_port: int = 0, specialist_port: int = 0,
                 specialist_token: str | None = None, webhook_token: str | None = None,
                 timeout: float = 0.6, slow_seconds: float = 2, normal_seconds: float = 0.04,
                 demo_faults: bool = True):
        self.specialist_token = specialist_token or secrets.token_urlsafe(32)
        self.webhook_token = webhook_token or secrets.token_urlsafe(32)
        self.specialist = Specialist(demo_faults=demo_faults, slow_seconds=slow_seconds, normal_seconds=normal_seconds)
        self.specialist_server = make_specialist_server("127.0.0.1", specialist_port,
            tokens={"fulfillment": self.specialist_token}, service=self.specialist)
        self.specialist_url = f"http://127.0.0.1:{self.specialist_server.server_port}"
        self.client = A2AClient(self.specialist_url, self.specialist_token, timeout=timeout, poll_seconds=0.04)
        self.fulfillment = Fulfillment(self.client)
        try:
            self.fulfillment_server = make_fulfillment_server("127.0.0.1", fulfillment_port,
                token=self.webhook_token, service=self.fulfillment)
        except Exception:
            self.specialist_server.server_close()
            raise
        self.fulfillment_url = f"http://127.0.0.1:{self.fulfillment_server.server_port}"
        self.threads = []

    def __enter__(self):
        for server in (self.specialist_server, self.fulfillment_server):
            self.threads.append(start_server(server))
        return self

    def __exit__(self, *args):
        self.specialist.close()
        for server in (self.fulfillment_server, self.specialist_server):
            if self.threads:
                server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=1)

    def turn(self, payload: dict) -> dict:
        return request_json(self.fulfillment_url + "/webhook/dialogflow-cx", token=self.webhook_token,
                            payload=payload, timeout=self.client.timeout + 2)


def run_demo(output: str = "artifacts/demo-results.json") -> int:
    print("LOCAL DEMO | simulated CX webhook input | real HTTP + A2A 0.3 JSON-RPC")
    print("Fictitious orders. No microphone, cloud account, or pip packages required.\n")
    records = []
    with LocalStack() as stack:
        print(f"Fulfillment: {stack.fulfillment_url}   Specialist: {stack.specialist_url}\n")
        session = "demo-" + str(uuid.uuid4())

        def check(name, request, expected, contains=None):
            began = time.monotonic()
            response = stack.turn(request)
            elapsed = round((time.monotonic() - began) * 1000)
            passed = status(response) == expected and (contains is None or contains in speech(response))
            records.append({"scenario": name, "passed": passed, "status": status(response),
                            "elapsed_ms": elapsed, "spoken_response": speech(response)})
            print(f"{'PASS' if passed else 'FAIL'} | {name:<26} | {elapsed:>4} ms | {status(response)}")
            print(f"       {speech(response) or '[no speech: old turn suppressed]'}")
            return response

        check("Successful order lookup", cx_request(session, "ORD-1001"), "completed", "has shipped")
        check("Missing order number", cx_request(session + "-missing"), "input-required", "order number")
        first = check("Regional clarification", cx_request(session + "-region", "ORD-2001"), "input-required", "east or west")
        second = check("Resume regional task", cx_request(session + "-region", region="east"), "completed", "processed")
        same_task = first["sessionInfo"]["parameters"]["a2a_task_id"] == second["sessionInfo"]["parameters"]["a2a_task_id"]
        records.append({"scenario": "Continuation reuses task ID", "passed": same_task})
        print(f"{'PASS' if same_task else 'FAIL'} | Continuation reuses task ID")
        check("Unknown order", cx_request(session, "ORD-9999"), "completed", "couldn't find")
        check("Remote task failure", cx_request(session, "ORD-FAIL"), "failed", "try again")
        check("Malformed remote artifact", cx_request(session, "ORD-BAD"), "failed", "try again")
        check("Slow task deadline", cx_request(session, "ORD-SLOW"), "timeout", "try again")
        payload = cx_request(session + "-retry", "ORD-1002")
        original = stack.turn(payload)
        replay = stack.turn(payload)
        deduplicated = original == replay
        records.append({"scenario": "CX retry reuses response", "passed": deduplicated})
        print(f"{'PASS' if deduplicated else 'FAIL'} | CX retry reuses response")

        old_turn = cx_request(session + "-interrupt", "ORD-SLOW")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(stack.turn, old_turn)
            # Wait for the remote task to exist before demonstrating interruption.
            until = time.monotonic() + 1
            while time.monotonic() < until:
                with stack.fulfillment.lock:
                    state = stack.fulfillment.sessions.get(session + "-interrupt")
                    if state and state.current and state.current.task_id:
                        break
                threading.Event().wait(0.01)
            check("New turn after interruption", cx_request(session + "-interrupt", "ORD-1002"), "completed", "delivered")
            obsolete = pending.result(timeout=2)
        suppressed = status(obsolete) == "superseded" and speech(obsolete) == ""
        records.append({"scenario": "Old turn is not spoken", "passed": suppressed})
        print(f"{'PASS' if suppressed else 'FAIL'} | Old turn is not spoken")

    result = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "local-http",
              "protocol_target": "A2A 0.3.0 JSON-RPC subset", "cloud_services_exercised": False,
              "passed": all(item["passed"] for item in records), "scenarios": records}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\n{sum(item['passed'] for item in records)}/{len(records)} checks passed. Evidence: {path}")
    return 0 if result["passed"] else 1


def chat(base_url: str, token: str) -> None:
    print("LOCAL CHAT: text parser simulates CX. Enter ORD-1001, ORD-2001, east, west, or quit.")
    session = "chat-" + str(uuid.uuid4())
    while True:
        text = input("Caller> ").strip()
        if text.lower() in ("quit", "exit"):
            break
        match = re.search(r"\bORD-(?:[0-9]{4}|SLOW|FAIL|BAD)\b", text.upper())
        region = next((value for value in ("east", "west") if re.search(r"\b" + value + r"\b", text.lower())), None)
        if not match and not region and "order" not in text.lower():
            print("IVR> This demo handles order status. Try: check ORD-1001.")
            continue
        response = request_json(base_url + "/webhook/dialogflow-cx", token=token,
                                payload=cx_request(session, match.group(0) if match else None, region=region), timeout=15)
        print(f"IVR> {speech(response)} [{status(response)}]")
