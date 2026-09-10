# API and environment reference

## Service boundaries

Local ports are configurable. The default fulfillment service is `http://127.0.0.1:8000`; the specialist is `http://127.0.0.1:8001`. Public cloud access reaches only the fulfillment service through your HTTPS tunnel.

All POST bodies and responses are JSON. The local servers accept bodies up to 64 KiB. Requests must have `Content-Type: application/json`. Chunked request bodies and batch JSON-RPC requests are not supported. Health and Agent Card GETs contain no credentials or order data.

| Caller → receiver | Authentication | Data sent |
|---|---|---|
| CX/tunnel → fulfillment | `Authorization: Bearer <CX_WEBHOOK_TOKEN>` | CX session ID, fulfillment tag, order/region parameters, response ID; CX may include other conversation fields |
| Voice bridge → fulfillment control | Same webhook bearer token | CX session resource name and application turn ID |
| Fulfillment → specialist | `Authorization: Bearer <A2A_BEARER_TOKEN>` | Order ID/region and A2A message/task identifiers |
| Python connector → Google CX | Google ADC access token | Typed input or microphone audio, session ID, language, turn payload |
| Microphone/bridge → LiveKit | Locally signed room access token | Room membership and audio media |

The adapter forwards only `order_id` and `region` as business data to the specialist. It does not forward the full CX request, transcript, caller telephone number, or audio. Service authentication in this demo does **not** prove that a caller owns an order; all records are fictitious.

## CX webhook

`POST /webhook/dialogflow-cx`

Example request from CX or a local simulator:

```json
{
  "detectIntentResponseId": "turn-demo-1",
  "fulfillmentInfo": {"tag": "order_status"},
  "sessionInfo": {
    "session": "projects/PROJECT/locations/global/agents/AGENT/sessions/CALL",
    "parameters": {"order_id": "ORD-1001"}
  }
}
```

Example successful response; identifiers vary:

```json
{
  "fulfillmentResponse": {
    "messages": [{"text": {"text": ["Order ORD-1001 has shipped and is with the delivery carrier."]}}],
    "mergeBehavior": "REPLACE"
  },
  "sessionInfo": {
    "parameters": {
      "a2a_status": "completed",
      "a2a_task_id": "TASK_UUID",
      "a2a_context_id": "CONTEXT_UUID",
      "a2a_missing_field": null
    }
  }
}
```

| Field | Contract |
|---|---|
| `fulfillmentInfo.tag` | Must be `order_status` |
| `sessionInfo.session` | Stable per call; 1–256 characters; use the full CX session name for cloud control calls |
| `detectIntentResponseId` | Used to deduplicate normal CX webhook retries; if missing, the adapter generates a new ID and cannot deduplicate separate retries |
| `sessionInfo.parameters.order_id` | Optional for clarification; `ORD-` plus four digits, or one of the named fault fixtures; normalized to uppercase |
| `sessionInfo.parameters.region` | Optional; `east` or `west`; normalized to lowercase |
| `payload.ivr_turn_id` | Optional bridge-controlled correlation ID; requires prior registration through `/api/turns/start` |

`a2a_status` is `completed`, `input-required`, `timeout`, `failed`, `invalid-input`, or `superseded`. A completed task can contain business status `not_found`. A superseded turn has no response messages. The normal application-level fallback returns HTTP 200 so CX can speak it. Invalid webhook input/tag returns 400, authentication failure 401, replay conflict 409, oversized body 413, wrong media type 415, and local capacity exhaustion 503.

This is the standard CX webhook schema, not the flexible-webhook mapping format. The adapter does not use `targetPage` or `targetFlow`; your CX routes own navigation. See the [CX webhook reference](https://docs.cloud.google.com/dialogflow/cx/docs/concept/webhook).

## Voice turn lifecycle

`POST /api/turns/start` and `POST /api/turns/cancel` share this body:

```json
{
  "session_id": "projects/PROJECT/locations/global/agents/AGENT/sessions/CALL",
  "turn_id": "application-turn-uuid"
}
```

`start` returns `{"status":"registered","turn_id":"application-turn-uuid"}`. Register each new utterance before calling CX. Carry its ID through `QueryParameters.payload`, which CX forwards to the webhook. See [QueryParameters](https://docs.cloud.google.com/python/docs/reference/dialogflow-cx/latest/google.cloud.dialogflowcx_v3.types.QueryParameters).

For cancellation, also supply `reason: "interruption"` when new speech starts, or `reason: "hangup"` when the call ends. The default is `hangup`. An interruption preserves a task that is already waiting for clarification, so the caller can answer it. A hangup cancels that parked task too.

`cancel` invalidates the matching current turn and attempts cancellation of a known remote task. It returns `status: canceled_locally` plus `remote_cancel_acknowledged: true|false`, and `continuation_preserved: true` when keeping a clarification task. An old/noncurrent ID is ignored. A false acknowledgement means remote cancellation is not confirmed; it does not mean the local turn is still valid.

These endpoints are part of this application, not the A2A specification.

## Agent discovery and A2A

`GET /.well-known/agent-card.json`

The card declares protocol `0.3.0`, transport `JSONRPC`, the configured `/a2a` URL, one `order_status` skill, JSON data modes, bearer authentication, and disabled streaming/push notifications. The client caches it for 300 seconds by default and requires its endpoint to match the configured base URL exactly. Discovery is validation of an approved specialist, not open-web agent search.

`POST /a2a`

Send a new task:

```json
{
  "jsonrpc": "2.0",
  "id": "rpc-1",
  "method": "message/send",
  "params": {
    "message": {
      "kind": "message",
      "messageId": "message-1",
      "role": "user",
      "parts": [{"kind": "data", "data": {"order_id": "ORD-1001"}}]
    },
    "configuration": {"blocking": false, "acceptedOutputModes": ["application/json"]}
  }
}
```

Poll the returned task identifier:

```json
{
  "jsonrpc": "2.0",
  "id": "rpc-2",
  "method": "tasks/get",
  "params": {"id": "TASK_UUID", "historyLength": 0}
}
```

Cancel it with `method: "tasks/cancel"` and `params: {"id":"TASK_UUID"}`. Resume an `input-required` task with a **new** message ID, the same `taskId` and `contextId` inside the message, and a data part such as `{"region":"east"}`.

Example completed result artifact:

```json
{
  "artifactId": "ARTIFACT_UUID",
  "name": "order_status",
  "parts": [{
    "kind": "data",
    "data": {
      "schema_version": "1",
      "order_id": "ORD-1001",
      "status": "shipped",
      "detail": "Your order is with the delivery carrier."
    }
  }]
}
```

The client accepts only the validated business fields it needs and ignores remote `detail` text. A wrong schema, order ID, artifact shape, or business status triggers the local fallback.

Supported task lifecycle: `submitted`, `working`, `input-required`, `completed`, `failed`, and `canceled`. Only `input-required` tasks can be resumed. A canceled task stays canceled. Reusing a message ID with identical content returns the same task while retained; changing its content returns an error. Task ownership is bound to the authenticated service principal.

| Error code | Meaning in this implementation |
|---|---|
| `-32700` | Invalid JSON parse |
| `-32600` | Invalid JSON-RPC envelope |
| `-32601` | Unsupported method |
| `-32602` | Invalid parameters or conflicting message ID |
| `-32001` | Task absent or owned by another service principal |
| `-32002` | Task already terminal and cannot be canceled |
| `-32003` | Push notifications unsupported |
| `-32004` | Unsupported operation, continuation/state limit, or capacity limit |
| `-32005` | Unsupported content mode |

This deliberately limited transport follows the [A2A 0.3.0 JSON-RPC specification](https://a2a-protocol.org/v0.3.0/specification/). No streaming, push delivery, extended authenticated card, file parts, cross-task contexts, durable task storage, or general A2A Message-result handling is implemented. It has not been certified for conformance.

## External API inventory

| Service | Endpoint or interface | Used when |
|---|---|---|
| Google Dialogflow CX | gRPC `google.cloud.dialogflow.cx.v3.Sessions/DetectIntent` at `dialogflow.googleapis.com:443` for global, or `<location>-dialogflow.googleapis.com:443` | `cx-text` and `voice` |
| Equivalent CX REST resource | `POST https://<endpoint>/v3/projects/<project>/locations/<location>/agents/<agent>/sessions/<session>:detectIntent` | Reference only; supplied connector uses the official gRPC Python client |
| Google OAuth | Browser sign-in and token refresh through Google authentication services, normally `accounts.google.com` and `oauth2.googleapis.com/token` for user ADC | ADC setup and token refresh; alternative identity configurations use their own documented endpoints |
| Google Service Usage | `serviceusage.googleapis.com`, used by `gcloud services enable` | Account/project setup only |
| LiveKit signaling/media | Your `wss://...` project URL plus the WebRTC ICE/TURN/media endpoints provided by that server | `voice` and `microphone`; firewall rules depend on the LiveKit deployment |
| HTTPS tunnel | The assigned public HTTPS hostname forwarding to port 8000 | Real CX webhook calls to the laptop |
| Python package index | Your pip-configured index, commonly PyPI and its file host | Optional package installation only |

The connector requests speech input and output through CX's `detectIntent` API. It does not call a separate Speech-to-Text or Text-to-Speech API. See [AudioInput](https://docs.cloud.google.com/python/docs/reference/dialogflow-cx/latest/google.cloud.dialogflowcx_v3.types.AudioInput) and [OutputAudioConfig](https://docs.cloud.google.com/python/docs/reference/dialogflow-cx/latest/google.cloud.dialogflowcx_v3.types.OutputAudioConfig).

There is no Google “A2A API” to enable. A2A is the protocol spoken by the local specialist. No real CRM, order-management, scheduling, payment, SIP-provider, or LLM API is connected.

## Complete environment-variable map

| Variable | Default/example | Required for / purpose |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | Local services; loopback only |
| `FULFILLMENT_PORT` | `8000` | Fulfillment server and local control client |
| `SPECIALIST_PORT` | `8001` | Local specialist listener |
| `A2A_BASE_URL` | `http://127.0.0.1:8001` | Approved specialist origin; remote origins require HTTPS |
| `A2A_BEARER_TOKEN` | Generated by `init` | Authentication from fulfillment to specialist |
| `CX_WEBHOOK_TOKEN` | Generated by `init` | Authentication to fulfillment and turn-control APIs |
| `A2A_TIMEOUT_SECONDS` | `2.0` | Delegation budget; configure 0.2–10 seconds |
| `A2A_POLL_SECONDS` | `0.10` | Poll interval; configure 0.02–1 second |
| `A2A_CARD_CACHE_SECONDS` | `300` | Agent Card cache TTL |
| `DEMO_FAULTS` | `true` | Enables named synthetic failure cases |
| `DEMO_SLOW_SECONDS` | `5.0` | Backend delay for `ORD-SLOW` |
| `CX_PROJECT_ID` | Empty | Google Cloud project for real CX calls |
| `CX_LOCATION` | `global` | Actual CX agent location |
| `CX_AGENT_ID` | Empty | Actual CX agent UUID |
| `CX_LANGUAGE_CODE` | `en-US` | Recognizer/agent language |
| `CX_DETECT_TIMEOUT_SECONDS` | `10` | Timeout for the whole CX request, separate from A2A's budget |
| `GOOGLE_APPLICATION_CREDENTIALS` | Empty | Optional credential-file path; leave blank for normal local user ADC |
| `LIVEKIT_URL` | Empty | LiveKit project WebSocket URL |
| `LIVEKIT_API_KEY` | Empty | Signing room tokens locally |
| `LIVEKIT_API_SECRET` | Empty | Secret used to sign room tokens locally |
| `LIVEKIT_ROOM` | `a2a-ivr-demo` | Shared room name |
| `LIVEKIT_CALLER_ID` | `demo-caller` | Accepted caller identity for the one-call bridge |
| `VAD_RMS_THRESHOLD` | `600` | Microphone energy threshold |
| `VAD_SILENCE_SECONDS` | `0.7` | Silence needed to end an utterance |
| `VAD_MAX_UTTERANCE_SECONDS` | `10` | Maximum buffered utterance length |

Exported process environment variables take precedence over `.env`. Blank `.env` values are skipped, including the optional Google credentials path. `.env` is a simple key/value format; shell substitution and interpolation are intentionally unsupported. Restart a command after changing its configuration.
