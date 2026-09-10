"""Run from the project directory with python -m ivr_poc --help."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import signal
import sys
import threading

from . import __version__
from .a2a_client import A2AClient
from .config import Settings, init_env, load_env
from .fulfillment import Fulfillment, make_fulfillment_server
from .http_support import TransportError, request_json, start_server
from .local_demo import chat, run_demo
from .specialist import Specialist, make_specialist_server


def serve(settings: Settings, selection: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    servers = []
    started = []
    try:
        if selection in ("all", "specialist"):
            service = Specialist(demo_faults=settings.demo_faults, slow_seconds=settings.slow_seconds)
            server = make_specialist_server(settings.host, settings.specialist_port,
                tokens={"fulfillment": settings.specialist_token}, service=service,
                advertised_url=settings.specialist_url)
            servers.append(server)
            print(f"Specialist:  http://127.0.0.1:{settings.specialist_port}", flush=True)
        if selection in ("all", "fulfillment"):
            client = A2AClient(settings.specialist_url, settings.specialist_token, timeout=settings.timeout_seconds,
                               poll_seconds=settings.poll_seconds, cache_seconds=settings.card_cache_seconds)
            server = make_fulfillment_server(settings.host, settings.fulfillment_port,
                token=settings.webhook_token, service=Fulfillment(client))
            servers.append(server)
            print(f"Fulfillment: http://127.0.0.1:{settings.fulfillment_port}", flush=True)
        for server in servers:
            start_server(server)
            started.append(server)
        print("Ready. Ctrl+C stops the services. Service tokens are loaded but never printed.", flush=True)
        while not stop.wait(0.5):
            pass
    finally:
        for server in servers:
            if hasattr(server, "specialist"):
                server.specialist.close()
            if server in started:
                server.shutdown()
            server.server_close()


def doctor(settings: Settings | None) -> int:
    print(f"Python {sys.version.split()[0]} | IVR PoC {__version__}")
    for package in ("google-cloud-dialogflow-cx", "livekit", "livekit-api", "sounddevice", "a2a-sdk"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "not installed (optional)"
        print(f"{package}: {version}")
    if settings is None:
        print("Run python -m ivr_poc init to configure persistent services.")
        return 0
    for name, url in (("fulfillment", f"http://127.0.0.1:{settings.fulfillment_port}"),
                      ("specialist", settings.specialist_url)):
        try:
            result = request_json(url + "/health", timeout=1)
            print(f"{name}: {result['status']}")
        except (TransportError, KeyError):
            print(f"{name}: not reachable; start python -m ivr_poc serve")
    print("Optional cloud configuration (presence only; no connection test):")
    for name in ("CX_PROJECT_ID", "CX_AGENT_ID", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        print(f"  {name}: {'set' if os.getenv(name) else 'missing'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local A2A + Dialogflow CX + LiveKit integration PoC")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create .env with random local service secrets")
    demo = sub.add_parser("demo", help="Run self-contained local scenarios; no .env or dependencies needed")
    demo.add_argument("--output", default="artifacts/demo-results.json")
    server = sub.add_parser("serve", help="Run the local HTTP services using .env")
    server.add_argument("--service", choices=("all", "specialist", "fulfillment"), default="all")
    sub.add_parser("chat", help="Interactive local text simulation against running services")
    sub.add_parser("doctor", help="Check configuration and running services without printing secrets")
    sub.add_parser("cx-text", help="Real Dialogflow CX text session; requires cloud setup")
    sub.add_parser("voice", help="Real LiveKit-to-CX audio bridge; requires cloud setup")
    sub.add_parser("microphone", help="Local microphone participant in the LiveKit room")
    sub.add_parser("sdk-check", help="Run the optional official A2A 0.3 SDK interoperability check")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(f"Created {init_env()}. Keep this file private.")
            return 0
        if args.command == "demo":
            return run_demo(args.output)
        load_env()
        if args.command == "doctor":
            try:
                settings = Settings.from_env()
            except ValueError:
                settings = None
            return doctor(settings)
        settings = Settings.from_env()
        if args.command == "serve":
            serve(settings, args.service)
        elif args.command == "chat":
            chat(f"http://127.0.0.1:{settings.fulfillment_port}", settings.webhook_token)
        elif args.command == "cx-text":
            from .connectors.cx import text_chat
            asyncio.run(text_chat())
        elif args.command == "voice":
            from .connectors.voice import run_voice
            asyncio.run(run_voice())
        elif args.command == "microphone":
            from .connectors.microphone import run_microphone
            asyncio.run(run_microphone())
        elif args.command == "sdk-check":
            from .connectors.sdk_check import check
            asyncio.run(check(settings.specialist_url, settings.specialist_token))
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except (ValueError, RuntimeError, TransportError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
