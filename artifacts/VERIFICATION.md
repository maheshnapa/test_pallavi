# Verification record

Executed: 2026-09-10T16:26:35.981933+00:00

Environment: Linux, Python 3.12.14.

- 29/29 local HTTP, task-lifecycle, and audio-format helper tests passed.
- 12/12 local demonstration checks passed.
- A clean copy successfully ran init, serve, doctor, chat, and graceful shutdown.
- All Python source files compiled successfully.
- Generated local secrets were absent from captured service/CLI output and are not in this package.

These checks do not include live Google CX, LiveKit media, the official A2A SDK,
or Windows/macOS audio hardware. The optional dependencies were unavailable for
installation in the build environment and cloud credentials were not supplied.

Run `python -m unittest discover -s tests -v` and `python -m ivr_poc demo` to
reproduce the local evidence. The demo uses actual HTTP on loopback, a simulated
CX webhook caller, and fictitious specialist records. Its timings are not a
production latency benchmark.

See `test-results.txt`, `demo-output.txt`, `demo-results.json`, and
`verification.json` in this directory for the recorded output.
