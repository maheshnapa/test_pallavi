"""One-room LiveKit RTC -> Dialogflow CX audio bridge.

This deliberately uses a simple energy-based endpoint detector and one buffered
detectIntent request per utterance. It is not a production SIP/media gateway.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import sys
import uuid
import wave
from array import array

from ..http_support import request_json
from .cx import CXSession

INPUT_RATE = 16000
OUTPUT_RATE = 24000


def room_token(identity: str) -> tuple[str, str]:
    from livekit import api
    url, key, secret = (os.getenv("LIVEKIT_URL", ""), os.getenv("LIVEKIT_API_KEY", ""),
                         os.getenv("LIVEKIT_API_SECRET", ""))
    if not all((url, key, secret)):
        raise ValueError("Set LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET in .env.")
    room = os.getenv("LIVEKIT_ROOM", "a2a-ivr-demo")
    from datetime import timedelta
    token = (api.AccessToken(key, secret).with_identity(identity).with_name(identity)
             .with_ttl(timedelta(hours=1)).with_grants(api.VideoGrants(room_join=True, room=room,
                 can_publish=True, can_subscribe=True)).to_jwt())
    return url, token


def rms(pcm: bytes) -> float:
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def decode_cx_audio(audio: bytes) -> bytes:
    if audio.startswith(b"RIFF"):
        with wave.open(io.BytesIO(audio), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, OUTPUT_RATE):
                raise ValueError("Expected CX output to be mono 16-bit PCM at 24000 Hz.")
            return stream.readframes(stream.getnframes())
    if len(audio) % 2:
        raise ValueError("Invalid PCM output length.")
    return audio


class VoiceBridge:
    def __init__(self):
        from livekit import rtc
        self.rtc = rtc
        self.room = rtc.Room()
        self.source = rtc.AudioSource(OUTPUT_RATE, 1)
        self.session = CXSession()
        self.caller = os.getenv("LIVEKIT_CALLER_ID", "demo-caller")
        self.threshold = float(os.getenv("VAD_RMS_THRESHOLD", "600"))
        self.silence_seconds = float(os.getenv("VAD_SILENCE_SECONDS", "0.7"))
        self.max_seconds = float(os.getenv("VAD_MAX_UTTERANCE_SECONDS", "10"))
        if not (100 <= self.threshold <= 10000 and 0.3 <= self.silence_seconds <= 2 and 2 <= self.max_seconds <= 20):
            raise ValueError("VAD settings are outside the supported demo range.")
        self.fulfillment_url = f"http://127.0.0.1:{os.getenv('FULFILLMENT_PORT', '8000')}"
        self.webhook_token = os.getenv("CX_WEBHOOK_TOKEN", "")
        self.generation = 0
        self.turn_id: str | None = None
        self.playback: asyncio.Task | None = None
        self.processing: asyncio.Task | None = None
        self.input_task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self.finished = asyncio.Event()
        self.pending: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=1)

    def spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                print(f"Bridge operation failed ({type(done.exception()).__name__}). Check service logs.")
        task.add_done_callback(finished)
        return task

    async def control(self, action: str, turn_id: str, *, reason: str = "hangup") -> None:
        await asyncio.to_thread(request_json, self.fulfillment_url + f"/api/turns/{action}",
            token=self.webhook_token, payload={"session_id": self.session.name, "turn_id": turn_id, "reason": reason}, timeout=1)

    async def interrupt(self) -> None:
        self.generation += 1
        self.source.clear_queue()
        if self.playback and not self.playback.done():
            self.playback.cancel()
        if self.turn_id:
            old = self.turn_id
            self.spawn(self.control("cancel", old, reason="interruption"))
        # Leave an in-flight CX request to finish under its timeout. Its response
        # will be discarded by generation, and the next request remains serialized.

    async def consume(self, track) -> None:
        stream = self.rtc.AudioStream(track, sample_rate=INPUT_RATE, num_channels=1)
        buffer = bytearray()
        preroll = bytearray()
        talking = False
        silence = 0.0
        duration = 0.0
        current_generation = self.generation
        try:
            async for event in stream:
                pcm = bytes(event.frame.data)
                seconds = len(pcm) / (INPUT_RATE * 2)
                voiced = rms(pcm) >= self.threshold
                if not talking:
                    preroll.extend(pcm)
                    del preroll[:-int(INPUT_RATE * 2 * 0.2)]
                    if not voiced:
                        continue
                    await self.interrupt()
                    current_generation = self.generation
                    talking, silence, duration = True, 0.0, 0.0
                    buffer.extend(preroll)
                    preroll.clear()
                else:
                    buffer.extend(pcm)
                duration += seconds
                silence = 0.0 if voiced else silence + seconds
                if silence >= self.silence_seconds or duration >= self.max_seconds:
                    if duration >= 0.25:
                        if self.pending.full():
                            self.pending.get_nowait()  # Keep only the newest complete utterance.
                        self.pending.put_nowait((bytes(buffer), current_generation))
                    buffer.clear()
                    talking = False
        finally:
            await stream.aclose()

    async def process(self) -> None:
        while True:
            pcm, generation = await self.pending.get()
            if generation != self.generation:
                continue
            turn = str(uuid.uuid4())
            self.turn_id = turn
            try:
                await self.control("start", turn)
                if generation != self.generation:
                    await self.control("cancel", turn, reason="interruption")
                    continue
                print(f"Turn {turn[:8]}: sending {len(pcm) / (INPUT_RATE * 2):.1f}s of audio to CX")
                reply = await self.session.detect(audio=pcm, turn_id=turn)
                if generation != self.generation:
                    print(f"Turn {turn[:8]}: stale reply discarded")
                    continue
                print(f"Turn {turn[:8]}: CX reply received")
                if reply.audio:
                    self.playback = self.spawn(self.play(decode_cx_audio(reply.audio), generation))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Voice turn failed ({type(error).__name__}). Check CX and fulfillment configuration.")

    async def play(self, pcm: bytes, generation: int) -> None:
        size = OUTPUT_RATE // 50 * 2  # 20 ms, mono PCM16.
        for offset in range(0, len(pcm), size):
            if generation != self.generation:
                return
            block = pcm[offset:offset + size]
            await self.source.capture_frame(self.rtc.AudioFrame(data=block, sample_rate=OUTPUT_RATE,
                num_channels=1, samples_per_channel=len(block) // 2))
        await self.source.wait_for_playout()

    async def run(self) -> None:
        rtc = self.rtc

        @self.room.on("track_subscribed")
        def subscribed(track, publication, participant):
            if participant.identity == self.caller and track.kind == rtc.TrackKind.KIND_AUDIO:
                if self.input_task and not self.input_task.done():
                    return  # Exactly one microphone track belongs to this single-call bridge.
                print("Caller microphone connected. Speak, then pause.")
                self.input_task = self.spawn(self.consume(track))

        @self.room.on("participant_disconnected")
        def disconnected(participant):
            if participant.identity == self.caller:
                self.finished.set()  # A fresh bridge process creates a fresh CX session for the next call.

        @self.room.on("disconnected")
        def room_disconnected(*args):
            self.finished.set()

        url, token = room_token("ivr-agent")
        try:
            await self.room.connect(url, token)
            track = rtc.LocalAudioTrack.create_audio_track("cx-response", self.source)
            await self.room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            self.processing = self.spawn(self.process())
            print(f"Voice bridge ready in room {os.getenv('LIVEKIT_ROOM', 'a2a-ivr-demo')}. Start the microphone command.")
            await self.finished.wait()
        finally:
            self.generation += 1
            self.source.clear_queue()
            if self.turn_id:
                try:
                    await self.control("cancel", self.turn_id)
                except Exception:
                    pass
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            await self.room.disconnect()
            await self.source.aclose()
            await self.session.close()


async def run_voice() -> None:
    try:
        import livekit.rtc  # noqa: F401
    except ImportError as error:
        raise RuntimeError("Install requirements-cloud.txt before running voice.") from error
    await VoiceBridge().run()
