# Troubleshooting

| Symptom | Check or fix |
|---|---|
| `No module named ivr_poc` | Run from the extracted `a2a-ivr-poc` directory, not its parent. Use the virtual environment's interpreter. |
| PowerShell blocks `Activate.ps1` | Use `.\.venv\Scripts\python.exe` directly in each command; activation is optional. |
| `venv`/`ensurepip` missing on Linux | Install your OS's Python venv package, or run the local demo with the installed Python 3.11+ interpreter directly. |
| `.env` missing or token too short | Run `python -m ivr_poc init`. Do not copy the `GENERATE_...` placeholders as real tokens. |
| Port already in use | Stop the other process or edit the port settings. If changing the specialist port, change `A2A_BASE_URL` too. If changing port 8000, update the tunnel command. |
| `serve` is running but `chat` cannot connect | Use the same project directory/configuration in both terminals. Run `doctor`. Check local firewall permission for loopback. |
| A2A HTTP 401 / fallback | Fulfillment's `A2A_BEARER_TOKEN` must equal the specialist's configured token. Restart both after changing it. |
| CX webhook HTTP 401 | The CX `Authorization` header must contain the generated `CX_WEBHOOK_TOKEN`, including `Bearer ` and the space. |
| CX webhook timeout | Check the tunnel and `serve`; confirm CX timeout exceeds the A2A timeout plus transport margin. Do not simply raise the A2A budget without considering voice latency. |
| CX recognizes text but produces no specialist result | Check that the matched intent route enables the standard webhook and uses the exact tag `order_status`. |
| CX asks for the wrong/missing order | Inspect simulator parameters; `order_id` must resolve to a canonical fixture such as `ORD-1001`. Reset stale session values for a new order intent. |
| `east` is not accepted | Check `ProvideRegion` entity annotation, its route condition, and that the same CX session is being used. |
| Google `DefaultCredentialsError` | Run `gcloud auth application-default login`. Remove a stale exported `GOOGLE_APPLICATION_CREDENTIALS` value if using ADC. |
| Google permission/quota error | Verify project, agent location, billing, Dialogflow API enablement, runtime permissions, and ADC quota-project permissions. |
| Google agent not found | `CX_AGENT_ID` is the UUID of a CX agent in the specified project and location. ES IDs and display names do not work. |
| LiveKit connects but there is no audio | Verify room names and identities, track publication, OS microphone permissions, output device, and network media connectivity. |
| Microphone/PortAudio device error | Run `python -m sounddevice` to list devices. Select an available OS default input/output and install PortAudio if missing. |
| Echo or repeated self-triggered turns | Wear headphones. This simple bridge has no acoustic echo cancellation. |
| Voice turns are cut off or never finish | Tune the RMS threshold/silence settings for your microphone. Long speech is capped at the configured utterance limit. |
| Interrupted response is heard briefly | Locally queued audio is cleared, but frames already delivered to a receiver can still play. End-to-end interruption behavior requires testing on the target clients. |
| Cloud packages cannot be installed | Check your approved package index and internet access. Local `demo` and tests still run without them. |
| SDK checker import or model error | Check `python -m pip show a2a-sdk`; use the 0.3-series requirement file. A 1.x installation uses a different API. Do not report this as a passed interoperability check until it completes. |
| State disappears after a restart | Expected: this PoC stores tasks, continuation, and replay records in memory. Start a new call/session. |

If a cloud check fails, capture the command, package versions, time, and relevant error category. Remove credentials, tokens, and customer content before sharing logs. The included fixtures can be used to reproduce failures without real records.
