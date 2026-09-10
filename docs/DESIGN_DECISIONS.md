# Feasibility and design decisions

## Engineering conclusion

Delegating an order-status lookup from a CX fulfillment service to an A2A specialist is technically feasible. This package demonstrates the local protocol path, task continuation, validated replies, and failure handling. It also supplies the LiveKit and CX adapters needed to attempt a real voice demonstration.

The evidence does not yet establish production voice latency, compatibility with your existing repository, or interoperability with an external specialist. Those require the actual services and configuration. A successful local run should be presented as proof of the integration pattern, not proof that the existing production IVR is ready to change.

## Why each component exists

| Decision | Reason | Consequence |
|---|---|---|
| CX owns intent routing | The current system already uses CX flows and parameters | The new capability is activated by a fulfillment tag; no second intent-routing LLM is introduced |
| A2A client in fulfillment | The CX webhook is an explicit boundary for business work | The existing LiveKit transport can remain in place; a webhook configuration/mapping change is normally needed |
| Read-only order status | Failure, cancellation, and retries can be demonstrated without creating bookings or financial actions | This is not a transaction-execution design |
| Structured data parts and artifacts | Spoken output must correspond to an expected business schema | Unexpected remote content becomes a fallback; arbitrary remote prose is not spoken |
| Approved endpoint and cached Agent Card | Discovery should describe a known integration, and repeated card lookups add work | No dynamic Internet agent selection; card URLs cannot expand the endpoint allowlist |
| Task polling | It exposes long-running and clarification states with a small transport implementation | Polls add requests; streaming or event delivery would need separate implementation |
| Per-call state and turn IDs | A caller can interrupt or leave before a task returns | Stale results are suppressed and cancellation is attempted |
| Service tokens | The webhook and specialist are different authentication boundaries | Two secrets are configured; caller identity still needs a separate design for real records |
| Local standard-library transport | The local code can be run and tested without downloading dependencies | The project owns a scoped A2A implementation; the optional official SDK check is still required for external compatibility evidence |
| A2A 0.3.0 target | A fixed wire version makes the demo's methods and objects explicit | It is not a claim of support for every newer A2A server or SDK; version changes need adapter work |
| Buffered CX audio request | It keeps the standalone voice bridge small and gives CX speech and dialog ownership | Silence detection and upload add latency; streaming media integration remains an optimization |

## Operational boundaries

**Latency.** The default persistent-service A2A budget is two seconds, including discovery on a cache miss and polling. A best-effort cancellation request can add up to 0.25 seconds. A change of order while a clarification task is parked can also issue a cancellation before new delegation. This is separate from audio endpoint detection, CX recognition/routing, tunnel transit, synthesis, and playback. Local fixture timings are not a cloud latency estimate.

The client checks an elapsed-time budget and applies network socket timeouts. Python's standard HTTP transport does not provide a strict wall-clock guarantee against a peer that continually dribbles response bytes. A production adapter should use a maintained client with total-request deadlines, bounded concurrency, and workload isolation. Do not promise a three-second voice response from this demo's timeout setting alone.

**State and scaling.** Tasks and sessions have bounded in-memory stores with a five-minute idle retention window. Recent webhook-turn records are capped at 32 per session; task continuation is capped at eight messages. The specialist allows up to 16 active workers, and each local HTTP server caps request threads at 32. Restarts lose task state and replay protection. Multiple fulfillment replicas would need shared state or deliberate session routing. This project is not a high-availability server.

**Cancellation and retries.** The local read-only worker honors cancellation and cannot complete a canceled task. External agents may finish before cancellation arrives, reject it, or lose the response. No cancellation can be assumed to undo a transaction. Idempotency in this package lasts only as long as the retained in-memory record; durable action IDs and reconciliation are needed for writes.

**Audio.** One bridge process accepts one caller identity in one room. It uses a basic energy threshold with headphones, not production echo cancellation or robust acoustic voice activity detection. A caller interruption invalidates pending output and clears queued playback. An earlier CX call can still change CX session state, so CX requests remain serialized and stale audio is discarded. SIP trunk setup, DTMF, human transfer, reconnection recovery, and multi-call dispatch are not implemented.

**Security.** Secrets are generated locally, POST endpoints authenticate the service, remote origins require HTTPS, redirects are rejected, and remote fields are validated. These controls do not establish end-user identity, tenant entitlement, data residency, or a complete production trust model. The supplied records contain no real customer data. Logs contain correlation identifiers and outcomes, not tokens or transcripts; raw CX requests are not forwarded to A2A.

**Dependencies.** Core tests need no downloaded packages. The optional SDK and cloud dependencies could not be installed in the build environment. Their source is included and syntax-checked; cloud execution and official SDK compatibility remain unverified. The requirement files give version ranges rather than a fabricated tested lockfile.

## What an engineering “spike” would answer

A spike is a small investigation that answers an unresolved technical question. It is useful only when its result changes a design decision. It is not an extra application or a prerequisite for demonstrating the local code.

| Unresolved question | Concrete check | Decision informed |
|---|---|---|
| Does the intended specialist speak the same protocol and business schema? | Run the supplied SDK check, then a send/poll/clarification/cancel contract check against that agent with its authentication | Keep the adapter, implement a version/schema adapter, or use the existing REST API |
| Can the real voice turn meet the agreed response target? | Measure end-of-speech to first reply audio plus delegation p50/p95 with the actual region, network, tunnel/deployment, and backend | Keep synchronous lookup, improve transport, or design an actual asynchronous follow-up capability |
| Does interruption preserve the existing CX conversation? | Interrupt a lookup, change the order, and hang up while inspecting CX session state and remote task state | Add lifecycle wiring or revise page/turn ownership |
| Can a caller be authorized to see the real result? | Demonstrate caller verification and backend entitlement checks for one approved record | Permit real data integration or keep the demonstration on fixtures |

The local tests already answer whether this implementation falls back on malformed data, avoids duplicate tasks on a recent retry, resumes a clarification task, and suppresses old output. Repeating those as separate research projects would add little. Real voice behavior and external interoperability remain the important unknowns.

## A2A versus a direct API

A2A is justified when an independently operated specialist already exposes this protocol, or several consumers need its standard task interface. It adds a discoverable capability description and task lifecycle to the integration boundary.

If the only requirement is calling an existing order-status REST endpoint owned by the same team, a direct API adapter is likely simpler. The business result schema, authentication, timeout, and reply validation are still needed. Replacing A2A with REST does not automatically improve a slow backend; it removes protocol/discovery/polling overhead where that overhead has no practical benefit.

Do not build a full agent mesh or replace CX merely to support this read-only delegation. Exposing the voice agent itself as an A2A server is a separate requirement and is not implemented here.
