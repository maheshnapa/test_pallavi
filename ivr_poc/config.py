"""Environment configuration. Importing this module does not read credentials."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def load_env(path: str = ".env") -> None:
    """Load simple KEY=value settings; exported environment variables win."""
    source = Path(path)
    if not source.is_file():
        return
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            raise ValueError(f"Invalid environment setting on line {number} of {path}")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def init_env() -> Path:
    target = Path(".env")
    if target.exists():
        raise ValueError(".env already exists. Keep it, or rename it before running init.")
    template = Path(".env.example")
    if not template.is_file():
        raise ValueError("Run init from the extracted project directory containing .env.example.")
    content = template.read_text(encoding="utf-8")
    for placeholder in ("GENERATE_SPECIALIST_TOKEN", "GENERATE_WEBHOOK_TOKEN"):
        content = content.replace(placeholder, secrets.token_urlsafe(32))
    # Exclusive creation protects an existing configuration even with simultaneous runs.
    with target.open("x", encoding="utf-8") as output:
        output.write(content)
    try:
        target.chmod(0o600)
    except OSError:
        pass  # Windows permissions are managed by the user's account.
    return target


def validate_base_url(value: str, *, allow_local_http: bool = True) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Service URLs must not contain credentials, queries, or fragments.")
    if not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("Set a service base URL with a hostname and no path.")
    if parsed.scheme != "https":
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not (parsed.scheme == "http" and local and allow_local_http):
            raise ValueError("Use HTTPS for remote agents. HTTP is allowed only on loopback.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    fulfillment_port: int = 8000
    specialist_port: int = 8001
    specialist_url: str = "http://127.0.0.1:8001"
    specialist_token: str = ""
    webhook_token: str = ""
    timeout_seconds: float = 2.0
    poll_seconds: float = 0.10
    card_cache_seconds: float = 300.0
    demo_faults: bool = True
    slow_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()
        value = cls(
            host=os.getenv("BIND_HOST", "127.0.0.1"),
            fulfillment_port=int(os.getenv("FULFILLMENT_PORT", "8000")),
            specialist_port=int(os.getenv("SPECIALIST_PORT", "8001")),
            specialist_url=validate_base_url(os.getenv("A2A_BASE_URL", "http://127.0.0.1:8001")),
            specialist_token=os.getenv("A2A_BEARER_TOKEN", ""),
            webhook_token=os.getenv("CX_WEBHOOK_TOKEN", ""),
            timeout_seconds=float(os.getenv("A2A_TIMEOUT_SECONDS", "2")),
            poll_seconds=float(os.getenv("A2A_POLL_SECONDS", "0.1")),
            card_cache_seconds=float(os.getenv("A2A_CARD_CACHE_SECONDS", "300")),
            demo_faults=os.getenv("DEMO_FAULTS", "true").lower() == "true",
            slow_seconds=float(os.getenv("DEMO_SLOW_SECONDS", "5")),
        )
        for name, token in (("A2A_BEARER_TOKEN", value.specialist_token),
                            ("CX_WEBHOOK_TOKEN", value.webhook_token)):
            if len(token) < 24 or token.startswith("GENERATE_"):
                raise ValueError(f"Set {name} to a random secret of at least 24 characters; run python -m ivr_poc init.")
        if value.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("This demo binds to IPv4 loopback. Use an HTTPS tunnel for the CX webhook.")
        if not (0.2 <= value.timeout_seconds <= 10 and 0.02 <= value.poll_seconds <= 1):
            raise ValueError("A2A timeout must be 0.2–10 seconds; polling must be 0.02–1 second.")
        if not (1 <= value.fulfillment_port <= 65535 and 1 <= value.specialist_port <= 65535):
            raise ValueError("Service ports must be between 1 and 65535.")
        if value.fulfillment_port == value.specialist_port:
            raise ValueError("The fulfillment and specialist ports must differ.")
        return value
