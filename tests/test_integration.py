"""Behavior tests using actual HTTP services; no cloud accounts or test framework needed."""

from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ivr_poc.a2a_client import A2AClient, ProtocolError
from ivr_poc.http_support import TransportError, request_json, start_server
from ivr_poc.local_demo import LocalStack, cx_request, speech, status
from ivr_poc.specialist import Specialist, make_specialist_server


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = LocalStack(timeout=0.25, slow_seconds=1, normal_seconds=0.025)
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.session = str(uuid.uuid4())

    def turn(self, order_id=None, **kwargs):
        return self.stack.turn(cx_request(self.session, order_id, **kwargs))

    def wait_for_task(self):
        until = time.monotonic() + 1
        while time.monotonic() < until:
            with self.stack.fulfillment.lock:
                session = self.stack.fulfillment.sessions.get(self.session)
                if session and session.current and session.current.task_id:
                    return session.current.task_id
            threading.Event().wait(0.005)
        self.fail("No remote task was created")

    def test_success_returns_spoken_status(self):
        response = self.turn("ORD-1001")
        self.assertEqual(status(response), "completed")
        self.assertIn("has shipped", speech(response))

    def test_unknown_order_is_business_result(self):
        response = self.turn("ORD-9999")
        self.assertEqual(status(response), "completed")
        self.assertIn("couldn't find", speech(response))

    def test_clarification_resumes_same_remote_task(self):
        first = self.turn("ORD-2001")
        second = self.turn(region="west")
        self.assertEqual(status(first), "input-required")
        self.assertEqual(status(second), "completed")
        self.assertEqual(first["sessionInfo"]["parameters"]["a2a_task_id"],
                         second["sessionInfo"]["parameters"]["a2a_task_id"])

    def test_missing_order_can_be_supplied_later(self):
        first, second = self.turn(), self.turn("ORD-1002")
        self.assertEqual(status(first), "input-required")
        self.assertEqual(status(second), "completed")
        self.assertEqual(first["sessionInfo"]["parameters"]["a2a_task_id"],
                         second["sessionInfo"]["parameters"]["a2a_task_id"])

    def test_different_order_abandons_parked_clarification(self):
        first = self.turn("ORD-2001")
        second = self.turn("ORD-1001")
        task_id = first["sessionInfo"]["parameters"]["a2a_task_id"]
        self.assertEqual(status(second), "completed")
        self.assertNotEqual(task_id, second["sessionInfo"]["parameters"]["a2a_task_id"])
        self.assertEqual(self.stack.client.rpc("tasks/get", {"id": task_id})["status"]["state"], "canceled")

    def test_bad_artifact_is_never_spoken(self):
        response = self.turn("ORD-BAD")
        self.assertEqual(status(response), "failed")
        self.assertIn("try again", speech(response))
        self.assertNotIn("unexpected", speech(response))

    def test_remote_failure_has_fallback(self):
        self.assertEqual(status(self.turn("ORD-FAIL")), "failed")

    def test_deadline_cancels_slow_read_only_task(self):
        start = time.monotonic()
        response = self.turn("ORD-SLOW")
        self.assertEqual(status(response), "timeout")
        self.assertLess(time.monotonic() - start, 0.8)
        task_id = response["sessionInfo"]["parameters"]["a2a_task_id"]
        task = self.stack.client.rpc("tasks/get", {"id": task_id})
        self.assertEqual(task["status"]["state"], "canceled")
        threading.Event().wait(0.03)
        self.assertEqual(self.stack.client.rpc("tasks/get", {"id": task_id})["status"]["state"], "canceled")

    def test_cx_retry_reuses_response_and_task(self):
        request = cx_request(self.session, "ORD-1001")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(self.stack.turn, [request, request]))
        self.assertEqual(first, second)
        self.assertEqual(len(self.stack.specialist.entries), 1)

    def test_reused_turn_id_with_changed_content_is_rejected(self):
        self.turn("ORD-1001", turn_id="same-turn")
        with self.assertRaises(TransportError) as error:
            self.turn("ORD-1002", turn_id="same-turn")
        self.assertEqual(error.exception.status, 409)

    def test_new_turn_suppresses_old_speech(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.turn, "ORD-SLOW")
            task_id = self.wait_for_task()
            self.assertEqual(status(self.turn("ORD-1002")), "completed")
            obsolete = old.result(timeout=2)
        self.assertEqual(status(obsolete), "superseded")
        self.assertEqual(speech(obsolete), "")
        self.assertEqual(self.stack.client.rpc("tasks/get", {"id": task_id})["status"]["state"], "canceled")

    def test_hangup_prevents_late_webhook_from_starting_task(self):
        self.stack.fulfillment.start_turn(self.session, "voice-turn")
        result = self.stack.fulfillment.cancel(self.session, "voice-turn")
        self.assertEqual(result["status"], "canceled_locally")
        response = self.turn("ORD-1001", turn_id="voice-turn", registered=True)
        self.assertEqual(status(response), "superseded")
        self.assertEqual(len(self.stack.specialist.entries), 0)

    def test_voice_answer_preserves_clarification_but_hangup_cancels_it(self):
        self.stack.fulfillment.start_turn(self.session, "question")
        first = self.turn("ORD-2001", turn_id="question", registered=True)
        task_id = first["sessionInfo"]["parameters"]["a2a_task_id"]
        interrupt = self.stack.fulfillment.cancel(self.session, "question", reason="interruption")
        self.assertTrue(interrupt["continuation_preserved"])
        self.stack.fulfillment.start_turn(self.session, "answer")
        second = self.turn(region="east", turn_id="answer", registered=True)
        self.assertEqual(status(second), "completed")
        self.assertEqual(task_id, second["sessionInfo"]["parameters"]["a2a_task_id"])
        parked = self.turn("ORD-2001", turn_id="another-question")
        task_id = parked["sessionInfo"]["parameters"]["a2a_task_id"]
        self.stack.fulfillment.cancel(self.session, "another-question", reason="hangup")
        self.assertEqual(self.stack.client.rpc("tasks/get", {"id": task_id})["status"]["state"], "canceled")

    def test_hangup_during_task_cancels_and_suppresses(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.turn, "ORD-SLOW", turn_id="hangup")
            task_id = self.wait_for_task()
            self.stack.fulfillment.cancel(self.session, "hangup")
            response = pending.result(timeout=2)
        self.assertEqual(speech(response), "")
        self.assertEqual(status(response), "superseded")
        self.assertEqual(self.stack.client.rpc("tasks/get", {"id": task_id})["status"]["state"], "canceled")

    def test_remote_unavailable_has_fallback(self):
        self.stack.client.base_url = "http://127.0.0.1:1"
        response = self.turn("ORD-1001")
        self.assertEqual(status(response), "failed")
        self.assertIn("try again", speech(response))

    def test_bearer_is_required_for_both_services(self):
        for url, payload in ((self.stack.fulfillment_url + "/webhook/dialogflow-cx", cx_request(self.session, "ORD-1001")),
                             (self.stack.specialist_url + "/a2a", {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "missing"}})):
            with self.subTest(url=url), self.assertRaises(TransportError) as error:
                request_json(url, payload=payload)
            self.assertEqual(error.exception.status, 401)

    def test_unrecognized_fulfillment_tag_is_rejected(self):
        request = cx_request(self.session, "ORD-1001")
        request["fulfillmentInfo"]["tag"] = "make_payment"
        with self.assertRaises(TransportError) as error:
            self.stack.turn(request)
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(len(self.stack.specialist.entries), 0)

    def test_invalid_order_never_reaches_specialist(self):
        self.assertEqual(status(self.turn("../private")), "invalid-input")
        self.assertEqual(len(self.stack.specialist.entries), 0)

    def test_concurrent_sessions_do_not_share_results(self):
        inputs = [cx_request(str(uuid.uuid4()), "ORD-1001" if i % 2 else "ORD-1002") for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.stack.turn, inputs))
        self.assertTrue(all(status(response) == "completed" for response in results))
        tasks = {response["sessionInfo"]["parameters"]["a2a_task_id"] for response in results}
        self.assertEqual(len(tasks), 8)


class ProtocolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.specialist = Specialist(normal_seconds=0.02)
        self.server = make_specialist_server("127.0.0.1", 0,
            tokens={"alice": "a" * 32, "bob": "b" * 32}, service=self.specialist)
        start_server(self.server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.specialist.close)

    def rpc(self, method, params, token="a" * 32):
        return request_json(self.url + "/a2a", token=token,
            payload={"jsonrpc": "2.0", "id": "request-1", "method": method, "params": params})

    def message(self, data=None):
        return {"message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                            "parts": [{"kind": "data", "data": data or {}}]}}

    def test_task_is_owned_by_authenticated_principal(self):
        task = self.rpc("message/send", self.message())["result"]
        for method in ("tasks/get", "tasks/cancel"):
            self.assertEqual(self.rpc(method, {"id": task["id"]}, token="b" * 32)["error"]["code"], -32001)

    def test_message_retry_deduplicates_and_conflict_rejects(self):
        message = self.message()
        first = self.rpc("message/send", message)["result"]
        second = self.rpc("message/send", message)["result"]
        self.assertEqual(first["id"], second["id"])
        message["message"]["parts"][0]["data"] = {"order_id": "ORD-1001"}
        self.assertEqual(self.rpc("message/send", message)["error"]["code"], -32602)

    def test_canceled_task_cannot_restart(self):
        task = self.rpc("message/send", self.message())["result"]
        self.rpc("tasks/cancel", {"id": task["id"]})
        resumed = self.message({"order_id": "ORD-1001"})
        resumed["message"].update(taskId=task["id"], contextId=task["contextId"])
        self.assertEqual(self.rpc("message/send", resumed)["error"]["code"], -32004)

    def test_wrong_context_is_rejected(self):
        task = self.rpc("message/send", self.message())["result"]
        resumed = self.message({"order_id": "ORD-1001"})
        resumed["message"].update(taskId=task["id"], contextId="wrong")
        self.assertEqual(self.rpc("message/send", resumed)["error"]["code"], -32602)

    def test_card_does_not_authorize_arbitrary_endpoint(self):
        original = self.specialist.card
        self.specialist.card = lambda base: {**original(base), "url": "https://example.invalid/a2a"}
        with self.assertRaises(ProtocolError):
            A2AClient(self.url, "a" * 32).discover()

    def test_unsupported_operations_and_parse_errors(self):
        self.assertEqual(self.rpc("message/stream", {})["error"]["code"], -32601)
        request = Request(self.url + "/a2a", data=b"{broken", headers={
            "Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"})
        with urlopen(request, timeout=1) as response:
            self.assertEqual(json.load(response)["error"]["code"], -32700)

    def test_oversized_body_is_rejected(self):
        request = Request(self.url + "/a2a", data=b"x" * 65537, headers={
            "Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=1)
        self.assertEqual(error.exception.code, 413)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
