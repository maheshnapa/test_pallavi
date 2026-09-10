"""Local microphone/speaker participant for the real LiveKit demo."""

from __future__ import annotations

import asyncio
import os

from .voice import INPUT_RATE, OUTPUT_RATE, room_token


async def run_microphone() -> None:
    try:
        import sounddevice as sd
        from livekit import rtc
    except ImportError as error:
        raise RuntimeError("Install requirements-cloud.txt. Linux also needs PortAudio; see CLOUD_SETUP.md.") from error
    room = rtc.Room()
    source = rtc.AudioSource(INPUT_RATE, 1)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    tasks = set()
    finished = asyncio.Event()
    output = sd.RawOutputStream(samplerate=OUTPUT_RATE, channels=1, dtype="int16")

    def enqueue(pcm):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(pcm)

    def capture(indata, frames, timing, status):
        loop.call_soon_threadsafe(enqueue, bytes(indata))

    async def publish_audio():
        while True:
            pcm = await queue.get()
            await source.capture_frame(rtc.AudioFrame(data=pcm, sample_rate=INPUT_RATE,
                num_channels=1, samples_per_channel=len(pcm) // 2))

    async def play_audio(track):
        stream = rtc.AudioStream(track, sample_rate=OUTPUT_RATE, num_channels=1)
        try:
            async for event in stream:
                await asyncio.to_thread(output.write, bytes(event.frame.data))
        finally:
            await stream.aclose()

    def spawn(awaitable):
        task = asyncio.create_task(awaitable)
        tasks.add(task)
        def done(result):
            tasks.discard(result)
            if not result.cancelled() and result.exception():
                print(f"Audio operation failed ({type(result.exception()).__name__}).")
                finished.set()
        task.add_done_callback(done)
        return task

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if participant.identity == "ivr-agent" and track.kind == rtc.TrackKind.KIND_AUDIO:
            spawn(play_audio(track))

    @room.on("disconnected")
    def disconnected(*args):
        finished.set()

    url, token = room_token(os.getenv("LIVEKIT_CALLER_ID", "demo-caller"))
    try:
        output.start()
        await room.connect(url, token)
        track = rtc.LocalAudioTrack.create_audio_track("caller-microphone", source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        spawn(publish_audio())
        with sd.RawInputStream(samplerate=INPUT_RATE, blocksize=320, channels=1, dtype="int16", callback=capture):
            print("Microphone connected. Use headphones. Speak clearly, then pause. Ctrl+C to leave.")
            await finished.wait()
    finally:
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        await room.disconnect()
        await source.aclose()
        output.stop()
        output.close()
