# Connect real Dialogflow CX and LiveKit

Use these instructions after the local demo passes. These connectors are included in source, but they have not been run against cloud accounts in the build environment. Your first successful cloud run is the verification of the credentials, installed SDK versions, CX routes, and audio devices on your machine.

## 1. Install the optional packages

From the extracted project folder, with your Python 3.11/3.12 virtual environment active:

```bash
python -m pip install -r requirements-cloud.txt
python -m ivr_poc doctor
```

On Ubuntu/Debian, the microphone client normally needs PortAudio:

```bash
sudo apt-get install libportaudio2
```

If PortAudio is missing on macOS, install it through your normal package manager, for example `brew install portaudio`. Allow terminal microphone access in the operating system. Windows wheels commonly provide the PortAudio runtime; check the [sounddevice installation reference](https://python-sounddevice.readthedocs.io/en/latest/installation.html) if your architecture has no wheel.

The source uses the documented Python RTC and CX interfaces. Dependency ranges are bounded by major version, but dependency resolution and native audio compatibility still need a smoke test. After a successful run, record the actual versions:

```bash
python -m pip freeze > artifacts/cloud-environment.txt
```

## 2. Prepare Google authentication

You need a Google Cloud project with billing and a **Dialogflow CX** test agent. This package does not connect to Dialogflow ES. Use a new test agent or a test copy of the existing CX agent so the demonstration can be configured independently.

Install the Google Cloud CLI. Ask your project administrator for permission to use Dialogflow. Runtime detection uses the Dialogflow API client role (`roles/dialogflow.client`); creating/editing the test agent needs additional editor/admin permissions. Enabling services and setting a quota project may require Service Usage permissions.

Replace `YOUR_PROJECT_ID` with your project ID:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable dialogflow.googleapis.com
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

`gcloud auth login` and Application Default Credentials serve different clients. The Python connector uses ADC. Leave `GOOGLE_APPLICATION_CREDENTIALS` blank in `.env` when using user ADC. If your organization supplies a service-account credential file, set that variable to its path instead; do not put the JSON contents into `.env` or the source tree.

### Create the CX agent

This project does not create the agent — it only calls one that already exists (`ivr_poc/connectors/cx.py`). If you don't have a test agent yet, create one first, then continue below. There is no `gcloud` subcommand for this; use the Console (simplest) or the REST API.

**Console (recommended):**

1. Open the [Dialogflow CX Console](https://dialogflow.cloud.google.com/cx/projects) and select your project.
2. Click **Build your own agent** (or **Create agent**).
3. Set a display name, a location (`global` is simplest and matches the `.env.example` default `CX_LOCATION=global`; pick a regional location such as `us-central1` only if you need data residency there), default language `en`, and a time zone.
4. Click **Create**. You land in the agent's Build view.
5. Open **Agent settings** (gear icon) to find the **Agent ID** — a UUID shown in the settings page and in the browser URL after `/agents/`. That UUID is your `CX_AGENT_ID`.

**REST API (if you want it scriptable):**

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: YOUR_PROJECT_ID" \
  -H "Content-Type: application/json" \
  -d '{"displayName":"a2a-ivr-poc","defaultLanguageCode":"en","timeZone":"America/New_York"}' \
  "https://dialogflow.googleapis.com/v3/projects/YOUR_PROJECT_ID/locations/global/agents"
```

Use `https://REGION-dialogflow.googleapis.com/v3/projects/YOUR_PROJECT_ID/locations/REGION/agents` instead if you picked a regional location. The response's `name` field is
`projects/YOUR_PROJECT_ID/locations/REGION/agents/AGENT_UUID` — the trailing UUID is your `CX_AGENT_ID`.

See [Manage agents with the API](https://docs.cloud.google.com/dialogflow/cx/docs/how/agent-create-api) and [Agents](https://docs.cloud.google.com/dialogflow/cx/docs/concept/agent) for the full resource reference.

Once the agent exists, continue with sections 4–5 below to add its webhook and routes — that configuration happens inside the agent you just created and cannot be skipped; an empty agent will not recognize any of this project's intents.

Set the following in `.env`:

```dotenv
CX_PROJECT_ID=your-project-id
CX_LOCATION=global
CX_AGENT_ID=your-cx-agent-uuid
CX_LANGUAGE_CODE=en-US
CX_DETECT_TIMEOUT_SECONDS=10
```

Use the **agent's actual location**, such as `us-central1`, if it is regional. The agent ID is a UUID from the CX agent resource, not the project ID or display name. This connector uses the draft agent session path, suitable for a test agent. If your existing system uses a deployed CX environment, adapt `CXSession.name` to its environment session path before using it there.

Google's [ADC setup](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment) and [Dialogflow access control](https://docs.cloud.google.com/dialogflow/cx/docs/concept/access-control) describe the authentication and permissions.

## 3. Start local services and make the webhook reachable

If you have not created `.env`, run `python -m ivr_poc init` first.

Terminal 1:

```bash
python -m ivr_poc serve
```

Google's servers cannot call `localhost` on your laptop. Use an HTTPS tunnel approved for your environment. For example, after installing and authenticating ngrok, Terminal 2:

```bash
ngrok http http://127.0.0.1:8000
```

Copy the public **HTTPS** URL and append `/webhook/dialogflow-cx`. For example, if your assigned URL is `https://your-assigned-name.ngrok.app`, the webhook is:

```text
https://your-assigned-name.ngrok.app/webhook/dialogflow-cx
```

Use the actual generated hostname. Keep the tunnel process running. If its address changes, update the webhook in CX. Only port 8000 needs a tunnel; the specialist on port 8001 remains local. The tunnel exposes the fulfillment HTTP service, whose POST endpoints require the generated bearer token. Use only the supplied fictitious records for this demonstration.

See the [ngrok agent quickstart](https://ngrok.com/docs/getting-started/) for installation and account setup. An existing reachable HTTPS test deployment can replace the tunnel.

## 4. Configure the CX webhook

In your test agent's webhook settings, add a **standard** webhook named `A2A Order Fulfillment`:

| Setting | Value |
|---|---|
| URL | Your HTTPS URL ending in `/webhook/dialogflow-cx` |
| Webhook type | Standard webhook, using the CX request/response schema |
| Method | POST |
| Authentication/header | Add `Authorization` with value `Bearer ` followed by the **CX_WEBHOOK_TOKEN** from your local `.env` |
| Timeout | 5 seconds for the default 2-second A2A budget |
| Fulfillment tag | `order_status`, set on the intent route that invokes the webhook |

The header must use `CX_WEBHOOK_TOKEN`, not `A2A_BEARER_TOKEN`. The specialist token is only used between the fulfillment service and the specialist.

Add a CX `webhook.error` event handler in the test flow with a short response such as “I couldn't retrieve the order status right now. Please try again shortly.” This covers failures where CX cannot reach the fulfillment service itself. A reachable fulfillment service already handles remote A2A failures and timeouts.

Webhook calls can be retried. The adapter reuses a response for the same session and `detectIntentResponseId` while its in-memory record is retained. Do not rely on this as durable exactly-once execution. See [CX webhook behavior](https://docs.cloud.google.com/dialogflow/cx/docs/concept/webhook).

## 5. Configure the minimal CX demo routes

The following setup uses two intent routes on the Default Start Flow's Start Page and no extra pages. It keeps all prompts in the webhook for this small demonstration. Existing agents can instead map their current form parameters and fulfillment route to the same webhook contract.

### Entity types

Create custom map entity `order_reference`. Add these canonical values and synonyms. Annotating the training phrases in the next step is necessary; entering text alone does not establish a parameter mapping.

| Canonical value | Example synonyms |
|---|---|
| `ORD-1001` | `ORD-1001`, `1001`, `one thousand one`, `one zero zero one` |
| `ORD-1002` | `ORD-1002`, `1002`, `one thousand two`, `one zero zero two` |
| `ORD-2001` | `ORD-2001`, `2001`, `two thousand one`, `two zero zero one` |
| `ORD-9999` | `ORD-9999`, `9999`, `nine nine nine nine` |
| `ORD-SLOW` | `ORD-SLOW`, `slow order` |
| `ORD-FAIL` | `ORD-FAIL`, `failed order` |
| `ORD-BAD` | `ORD-BAD`, `malformed order` |

Create map entity `order_region` with canonical values `east` and `west`, and matching synonyms. Keep fuzzy matching and automatic expansion disabled for these controlled fixture identifiers.

### OrderStatus intent

Add these training phrases and annotate each order reference with entity `@order_reference`, parameter ID **`order_id`**:

```text
Check order ORD-1001
Where is order 1002
Track order 2001
ORD-1001
1002
Check the slow order
Check the failed order
Check the malformed order
```

Also add `Check my order` without a parameter so the missing-order prompt can be tested.

Add an intent route for `OrderStatus` on the Start Page. Its fulfillment should:

1. Preset session `order_id` to expression `$intent.params.order_id.resolved`. This takes the current intent's value; when absent it must be null rather than a value from an earlier lookup.
2. Preset session `region` to `null`, without quotes, to clear any earlier order's region.
3. Invoke `A2A Order Fulfillment` with tag `order_status`.
4. Add no static agent response and no target page. The webhook supplies the reply.

Verify the parameter preset resolves to a string such as `ORD-1001` in the simulator's request JSON. If your CX configuration leaves an absent intent parameter unresolved, add a conditional preset for a missing value to set `order_id = null`; do not pass the literal expression to the webhook.

### ProvideRegion intent

Add phrases `east`, `west`, `the east region`, and `the west region`. Annotate the region with `@order_region`, parameter ID **`region`**.

Add a Start Page route matching this intent with condition:

```text
$session.params.a2a_missing_field = "region"
```

Its fulfillment presets session `region` to `$intent.params.region.resolved` and invokes the same webhook with tag `order_status`. Preserve `order_id`. Add no static response or target page.

CX remains on the Start Page between these turns. The fulfillment service holds the A2A task/context identifiers and resumes the original task. A different order starts a new task and cancels an abandoned clarification task. A caller saying only an order number is handled by the `OrderStatus` training phrases.

### Text check

First use the CX simulator. Then, Terminal 3:

```bash
python -m ivr_poc cx-text
```

Try:

```text
Check order 1001
Track order 2001
east
Check the slow order
quit
```

Expected responses: shipped, clarification, processing, and the retry-later fallback. If CX produces its default no-match response, inspect entity annotations and intent routing before moving on to microphone audio. `cx-text` must receive a reply through the actual webhook; successful intent recognition alone does not prove delegation worked.

## 6. Set up LiveKit

Create or use a LiveKit Cloud test project. Put its project URL and server API credentials in `.env`:

```dotenv
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
LIVEKIT_ROOM=a2a-ivr-demo
LIVEKIT_CALLER_ID=demo-caller
```

The two Python processes generate one-hour room tokens locally. The API secret remains in local server-side configuration. No public token endpoint is included. Both processes must use the same room and distinct identities (`ivr-agent` and `demo-caller`). Use a fresh room name if another demo is already running.

LiveKit's [room connection documentation](https://docs.livekit.io/reference/python/livekit/rtc/room.html) and [access-token API](https://docs.livekit.io/reference/python/livekit/api/access_token.html) are the connector references. A reachable self-hosted LiveKit server can be substituted by providing its URL and credentials; installation and NAT/TURN configuration of that server are outside this package.

### Optional: verify the LiveKit credentials before wiring up CX

`ivr_poc voice` always requires Dialogflow CX too (`VoiceBridge` constructs a `CXSession`
unconditionally), so it cannot be used as a standalone LiveKit connectivity test. To confirm
`LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` are valid and the room is reachable before
doing anything else, use LiveKit's own CLI instead:

```bash
brew install livekit-cli   # or see https://github.com/livekit/livekit-cli
lk room join --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" \
  --api-secret "$LIVEKIT_API_SECRET" --identity smoke-test "$LIVEKIT_ROOM"
```

A clean connect/leave with no auth or network error confirms the credentials and room are usable.
This does not exercise this project's own code at all — it only isolates whether the LiveKit side
of the setup works before moving on to CX.

## 7. Run the voice demonstration

Keep Terminal 1 (`serve`) and Terminal 2 (HTTPS tunnel) running. End `cx-text` before starting this demonstration so the logs are easy to follow.

Terminal 3:

```bash
python -m ivr_poc voice
```

Terminal 4:

```bash
python -m ivr_poc microphone
```

Wear headphones to keep synthesized speech out of the microphone. Say “Check order one thousand one,” then pause. You should hear CX's synthesized order status. Try “Track order two thousand one,” followed by “east.”

The path is microphone → LiveKit → local audio bridge → CX → HTTPS webhook tunnel → local fulfillment → local A2A specialist → CX synthesis → LiveKit → speaker.

The bridge uses an energy threshold, a 0.7-second end-of-utterance silence period, and a 10-second maximum utterance. If it never detects your speech, lower `VAD_RMS_THRESHOLD` modestly and restart `voice`. If background noise triggers turns, raise the threshold. Changes to `.env` require restarting processes. The bridge exits when its caller leaves; start a fresh bridge for another call.

## 8. Optional official A2A SDK check

With `serve` running:

```bash
python -m pip install -r requirements-sdk.txt
python -m ivr_poc sdk-check
```

Expected: a PASS message after discovery, sending `ORD-1001`, and receiving a completed task. The check targets the official **0.3-series Python SDK**, using the legacy client surface. It does not test the newer 1.x API, all protocol features, or interoperability with a third-party specialist. See the [official SDK](https://github.com/a2aproject/a2a-python) for migration guidance.

## 9. Connecting the existing IVR

Use `fulfillment.py` and `a2a_client.py` from the existing CX fulfillment deployment or call this service from it. Configure the existing CX intent route and parameter mapping in a test environment. Keep the existing LiveKit audio/SIP bridge if it already works.

For lifecycle handling, an existing worker can call `/api/turns/start` before CX and send the returned turn ID as `query_params.payload.ivr_turn_id`; it must use the same complete CX session resource string in both calls. Call `/api/turns/cancel` with `reason: "interruption"` on new speech or `reason: "hangup"` when the call ends. An interruption preserves a task already waiting for clarification. These are application APIs, not A2A methods. Without this optional wiring, the webhook still supports normal completion and timeout, but cannot learn that a real caller hung up.

This package does not provision a SIP trunk, phone number, CX agent, IAM policy, or production deployment. Those depend on your existing accounts and configuration; no such configuration was supplied with the request.
