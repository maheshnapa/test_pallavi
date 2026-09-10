# Team-lead demonstration

## What to say before running it

“This example adds one order-status specialist behind Dialogflow CX fulfillment. CX keeps control of the conversation. I will first show the task flow locally, including failure and interruption. The local requests simulate the CX webhook; the services and A2A HTTP calls are real. A separate mode connects an actual CX agent and LiveKit microphone.”

## Local walkthrough

1. Open `docs/ARCHITECTURE.md` to show the component flow and sequence diagrams. Standalone SVG copies of the main diagrams are in `diagrams/` for opening in a browser or including in a presentation.
2. Run `python -m ivr_poc demo` from the project directory.
3. Show the successful lookup: `ORD-1001` produces a shipped response.
4. Point out `ORD-2001`: the specialist asks for a region, and the second turn resumes the same task ID.
5. Show the unknown order as a normal business result rather than a service failure.
6. Show failure, malformed data, and timeout: all give a controlled retry-later response.
7. Show the retry and interruption checks: a recent duplicate turn reuses its result; an obsolete turn produces no speech.
8. Open `artifacts/demo-results.json` to show the measured local timings and explicit `cloud_services_exercised: false` value.

For an interactive conversation, run `serve` in one terminal and `chat` in another. Enter `check ORD-2001`, then `west`.

## What each fixture proves

| Input | Result | Point to explain |
|---|---|---|
| `ORD-1001` | Shipped | Structured remote result becomes a controlled reply |
| `ORD-1002` | Delivered | A separate result uses the same integration |
| No order number | Ask for order number | Missing data can be requested without failing the conversation |
| `ORD-2001`, then `east`/`west` | Clarification, then processing | The remote task survives across turns |
| `ORD-9999` | Order not found | Business outcome is separate from protocol failure |
| `ORD-SLOW` | Timeout fallback | A slow backend does not leave the local caller waiting indefinitely under normal transport behavior |
| `ORD-FAIL` | Failure fallback | Remote failure is contained |
| `ORD-BAD` | Validation fallback | Malformed agent data is not passed into speech |

Fault fixtures require `DEMO_FAULTS=true` in persistent-service mode. Turning it off makes those special IDs behave as unknown orders. The automated `demo` command always runs with its own isolated fault-enabled configuration.

## Real voice walkthrough

Only present this as a completed voice integration after you have followed `CLOUD_SETUP.md` and run it successfully with your accounts. Keep the services, tunnel, bridge, and microphone processes visible. Say “Check order one thousand one,” then try the regional clarification.

Explain that LiveKit carries audio, CX recognizes the caller and invokes fulfillment, A2A obtains the specialist result, and CX synthesizes the response. The specialist's records are still fixtures even in cloud mode.

## Questions management may ask

**Can this be added to our existing IVR?** The fulfillment integration pattern is compatible with a CX-driven IVR. The existing repository, parameter names, deployment, and lifecycle hooks must be checked before calling it a drop-in change.

**What is new?** An A2A client adapter in fulfillment, a specialist endpoint to call, credentials for that endpoint, and explicit failure/task-lifecycle handling. A real independently maintained specialist must provide the business capability; A2A itself does not supply it.

**Did we prove the three-second voice target?** No. The included timings are local fixture timings. The actual end-to-end voice path needs measurement with the intended backend and deployment.

**Why not call REST?** REST is appropriate when the backend already exposes the necessary API and there is no practical need for agent interoperability. This demonstration shows what the A2A task boundary adds.

**Can we use this for booking or payment?** Not without additional transaction authorization, durable idempotency, confirmation, reconciliation, and recovery design. This package performs read-only lookups.
