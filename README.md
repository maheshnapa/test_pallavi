# A2A Order Status IVR — Local PoC

A runnable example of a Dialogflow CX fulfillment service delegating an order lookup to an A2A specialist. The package also includes a LiveKit microphone bridge for a real CX agent.

Start with the local demonstration. It requires Python only and uses fictitious order data. Real LiveKit and Google connections are a separate mode that requires accounts, credentials, and CX webhook configuration.

## 1. Requirements

| Mode | What you need | External services used |
|---|---|---|
| Local demo and tests | Python 3.11+; a terminal; permission to listen on loopback ports | None |
| Local interactive chat | Same, plus the generated `.env` | None |
| Official A2A SDK check | Internet access to install `requirements-sdk.txt` | Package download only; requests still go to your local specialist |
| Real CX text demo | Google Cloud test project, billing/API access, CX agent, Google ADC, an HTTPS webhook tunnel, `requirements-cloud.txt` | Google Dialogflow CX and your tunnel provider |
| Real LiveKit voice demo | All CX requirements, a LiveKit project, microphone, headphones, PortAudio where required | LiveKit media/signaling, Google CX, tunnel |

Use Python **3.11 or 3.12** for the optional audio connectors to improve native-wheel availability. No OpenAI key, Gemini key, separate speech service, database, GPU, Redis, or Docker is required by this implementation.

## 2. Run the local demonstration

Extract the ZIP and open a terminal in `a2a-ivr-poc`, the folder containing this README.

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m ivr_poc demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**macOS or Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m ivr_poc demo
.venv/bin/python -m unittest discover -s tests -v
```

If `py -3.12` is unavailable, install Python 3.12 or use your installed Python 3.11 interpreter. On Linux, install your distribution's Python venv package if venv creation fails. The local demo can also run directly with `python3 -m ivr_poc demo`; installing this project with pip is optional.

Expected ending:

```text
12/12 checks passed. Evidence: artifacts/demo-results.json
```

The demo starts two HTTP listeners on available local ports, runs the scenarios, and stops them. CX input is simulated using its webhook JSON shape. No cloud speech recognition or phone call occurs in this command. Its shorter 0.6-second timeout keeps the failure demonstration quick; persistent services use the `.env` timeout, initially 2 seconds.

## 3. Run persistent services and interactive chat

In the remaining examples, `python` means the virtual environment's interpreter. Either use its full path as above, or activate it:

```powershell
# Windows PowerShell; full interpreter paths also work without activation.
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS/Linux
source .venv/bin/activate
```

Create local secrets once:

```bash
python -m ivr_poc init
```

This reads `.env.example`, creates `.env`, and generates two random bearer tokens. It never overwrites an existing `.env`. Keep the generated file private. There are no cloud credentials to fill in for local mode.

Terminal 1:

```bash
python -m ivr_poc serve
```

Terminal 2, from the same project folder and environment:

```bash
python -m ivr_poc doctor
python -m ivr_poc chat
```

Try these inputs:

```text
check ORD-1001
check ORD-2001
east
check ORD-SLOW
check ORD-FAIL
check ORD-BAD
quit
```

`chat` is a small text parser that simulates CX. It is useful for explaining the delegation flow, but it does not exercise Google's intent recognition. `Ctrl+C` stops the servers.

To demonstrate separate service processes, replace `serve` with two terminals:

```bash
python -m ivr_poc serve --service specialist
```

```bash
python -m ivr_poc serve --service fulfillment
```

## 4. Connect real Dialogflow CX and LiveKit

Follow [the cloud setup instructions](docs/CLOUD_SETUP.md) in order. They explain Google authentication, the two CX intent routes, webhook settings, LiveKit credentials, microphone setup, and every terminal command.

After that setup, the commands are:

```bash
python -m pip install -r requirements-cloud.txt
python -m ivr_poc cx-text
```

For audio, keep the services and webhook tunnel running, then run these in separate terminals:

```bash
python -m ivr_poc voice
```

```bash
python -m ivr_poc microphone
```

The voice bridge subscribes to one caller in a LiveKit room, buffers each utterance, sends it to CX, and publishes CX's synthesized reply. It uses the LiveKit RTC SDK directly. It does not require an LLM or the LiveKit Agents `function_tool` decorator.

## 5. API inventory

| API | Address or SDK operation | Purpose |
|---|---|---|
| Fulfillment health | `GET http://127.0.0.1:8000/health` | Local readiness check |
| CX webhook | `POST http://127.0.0.1:8000/webhook/dialogflow-cx` | Turn intent parameters into an A2A task and return CX fulfillment JSON |
| Voice turn control | `POST /api/turns/start`, `POST /api/turns/cancel` on port 8000 | Register and cancel a caller's turn |
| Specialist health | `GET http://127.0.0.1:8001/health` | Local readiness check |
| Agent Card | `GET http://127.0.0.1:8001/.well-known/agent-card.json` | Discover the approved specialist's protocol and skill |
| A2A task API | `POST http://127.0.0.1:8001/a2a` | JSON-RPC `message/send`, `tasks/get`, `tasks/cancel` |
| Google Dialogflow CX v3 | `SessionsAsyncClient.detect_intent` | Text/audio understanding, CX routing, webhook execution, synthesized output |
| Google authentication | Application Default Credentials | Obtain Google access tokens; no Dialogflow API key |
| LiveKit RTC | `Room.connect`, audio track publish/subscribe | Real-time microphone and reply audio |

Authentication, complete request/response examples, endpoint equivalents, and API limitations are in [API_REFERENCE.md](docs/API_REFERENCE.md).

## 6. What has been verified

The standard-library local services and integration tests were executed on Linux with Python 3.12. The actual results are included under `artifacts/`. Run the commands above to reproduce the evidence on your machine.

The optional LiveKit, Google CX, and official A2A SDK dependencies could not be downloaded in the build environment. Their code was syntax-checked and written against official references, but **a live cloud or SDK interoperability test has not been performed**. Windows/macOS audio devices also require testing on your machine. The dependency files specify compatibility ranges; they are not a tested lockfile.

This project targets a **documented subset of A2A 0.3.0 JSON-RPC**. Its core transport is implemented here with Python's standard library. It is not the official SDK, a complete conformance implementation, or an implementation of the newer 1.x SDK API. `sdk-check` is provided to check the 0.3-series SDK against the running specialist once installed.

## 7. Code and explanation map

| File | Responsibility |
|---|---|
| `ivr_poc/specialist.py` | Agent Card, A2A task state, fictitious orders, fault scenarios |
| `ivr_poc/a2a_client.py` | Card validation/cache, delegation, polling, result validation, cancellation |
| `ivr_poc/fulfillment.py` | CX webhook mapping, continuation, replay handling, safe reply templates |
| `ivr_poc/http_support.py` | Bounded JSON HTTP services, bearer authentication, HTTP client |
| `ivr_poc/local_demo.py` | Self-contained demo and interactive CX simulation |
| `ivr_poc/config.py` | `.env` loading and secret generation |
| `ivr_poc/connectors/cx.py` | Real Google CX text/audio calls |
| `ivr_poc/connectors/voice.py` | LiveKit room audio bridge and turn lifecycle |
| `ivr_poc/connectors/microphone.py` | Local microphone/speaker participant |
| `ivr_poc/connectors/sdk_check.py` | Optional official A2A SDK interoperability check |
| `tests/test_integration.py` | HTTP behavior and failure tests |
| `docs/ARCHITECTURE.md` | Full flow, sequence diagrams, integration points |
| `docs/DESIGN_DECISIONS.md` | Feasibility, design rationale, unresolved engineering questions |
| `docs/DEMO_SCRIPT.md` | Short walkthrough for the team lead/management |
| `docs/TROUBLESHOOTING.md` | Common setup and runtime fixes |

The order database is a fixture, customer identity verification is absent, task state is in memory, and the audio bridge handles one caller per process. These boundaries are deliberate for a local read-only demonstration. Connecting real customer data or booking/payment actions requires additional design described in [DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).
