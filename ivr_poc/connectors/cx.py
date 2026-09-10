"""Google Dialogflow CX text/audio API adapter using Application Default Credentials."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass


@dataclass
class Reply:
    text: str
    audio: bytes


class CXSession:
    def __init__(self, session_id: str | None = None):
        try:
            from google.cloud import dialogflowcx_v3 as cx
        except ImportError as error:
            raise RuntimeError("Install requirements-cloud.txt before running a CX connector.") from error
        project, location, agent = (os.getenv("CX_PROJECT_ID", ""), os.getenv("CX_LOCATION", "global"),
                                    os.getenv("CX_AGENT_ID", ""))
        if not project or not agent:
            raise ValueError("Set CX_PROJECT_ID and CX_AGENT_ID in .env.")
        endpoint = "dialogflow.googleapis.com" if location == "global" else f"{location}-dialogflow.googleapis.com"
        self.cx = cx
        self.client = cx.SessionsAsyncClient(client_options={"api_endpoint": endpoint})
        self.name = f"projects/{project}/locations/{location}/agents/{agent}/sessions/{session_id or uuid.uuid4()}"
        self.language = os.getenv("CX_LANGUAGE_CODE", "en-US")
        self.timeout = float(os.getenv("CX_DETECT_TIMEOUT_SECONDS", "10"))
        self.lock = asyncio.Lock()

    async def detect(self, *, text: str | None = None, audio: bytes | None = None,
                     turn_id: str | None = None) -> Reply:
        cx = self.cx
        if (text is None) == (audio is None):
            raise ValueError("Supply either text or PCM audio.")
        if audio is not None:
            query = cx.QueryInput(language_code=self.language, audio=cx.AudioInput(
                config=cx.InputAudioConfig(audio_encoding=cx.AudioEncoding.AUDIO_ENCODING_LINEAR_16,
                                           sample_rate_hertz=16000), audio=audio))
        else:
            query = cx.QueryInput(language_code=self.language, text=cx.TextInput(text=text))
        request = cx.DetectIntentRequest(session=self.name, query_input=query,
            query_params=cx.QueryParameters(payload={"ivr_turn_id": turn_id} if turn_id else {}),
            output_audio_config=cx.OutputAudioConfig(
                audio_encoding=cx.OutputAudioEncoding.OUTPUT_AUDIO_ENCODING_LINEAR_16, sample_rate_hertz=24000))
        # A CX session is stateful. Never run overlapping detectIntent calls in it.
        async with self.lock:
            response = await self.client.detect_intent(request=request, timeout=self.timeout, retry=None)
        lines = [line for message in response.query_result.response_messages for line in message.text.text]
        return Reply(" ".join(lines), bytes(response.output_audio))

    async def close(self) -> None:
        await self.client.transport.close()


async def text_chat() -> None:
    session = CXSession()
    print("REAL DIALOGFLOW CX | use a test agent configured as described in docs/CLOUD_SETUP.md")
    print("Type quit to stop. Requests use Google ADC and may incur service charges.")
    try:
        while True:
            text = await asyncio.to_thread(input, "Caller> ")
            if text.strip().lower() in ("quit", "exit"):
                break
            try:
                reply = await session.detect(text=text)
                print("CX> " + (reply.text or "[no text response]"))
            except Exception as error:
                print(f"CX request failed ({type(error).__name__}). Check credentials, region, and webhook logs.")
    finally:
        await session.close()
