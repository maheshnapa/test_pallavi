"""Optional external interoperability check against the official 0.3-series SDK."""

from __future__ import annotations

import asyncio
import uuid


async def check(base_url: str, token: str) -> None:
    try:
        import httpx
        from a2a.client import A2ACardResolver, A2AClient
        from a2a.types import GetTaskRequest, MessageSendParams, SendMessageRequest, TaskQueryParams
    except ImportError as error:
        raise RuntimeError("Install requirements-sdk.txt before sdk-check.") from error
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=3,
                                  follow_redirects=False) as http:
        resolver = A2ACardResolver(httpx_client=http, base_url=base_url)
        card = await resolver.get_agent_card()
        if card.url != base_url.rstrip("/") + "/a2a" or card.protocol_version != "0.3.0":
            raise ValueError("Unexpected agent endpoint or protocol version.")
        client = A2AClient(httpx_client=http, agent_card=card)
        request = SendMessageRequest(id=str(uuid.uuid4()), params=MessageSendParams(**{
            "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                        "parts": [{"kind": "data", "data": {"order_id": "ORD-1001"}}]},
            "configuration": {"blocking": False, "acceptedOutputModes": ["application/json"]}}))
        response = await client.send_message(request)
        body = response.model_dump(mode="json", by_alias=True)
        if "error" in body:
            raise RuntimeError("Official SDK received an A2A error.")
        task = body["result"]
        for _ in range(30):
            if task["status"]["state"] == "completed":
                data = task["artifacts"][0]["parts"][0]["data"]
                if data.get("status") != "shipped":
                    raise RuntimeError("Unexpected business result.")
                print("PASS: official SDK discovered the card, sent the request, and received the completed order task.")
                return
            if task["status"]["state"] in ("failed", "canceled", "rejected"):
                raise RuntimeError("Specialist task did not complete.")
            await asyncio.sleep(0.1)
            response = await client.get_task(GetTaskRequest(id=str(uuid.uuid4()),
                                            params=TaskQueryParams(id=task["id"], history_length=0)))
            task = response.model_dump(mode="json", by_alias=True)["result"]
        raise RuntimeError("Official SDK interoperability check timed out.")
