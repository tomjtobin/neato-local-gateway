# Telemetry receiver contract

The optional embedded collector sends `POST /api/telemetry/v1/frames` with
`Authorization: Bearer TOKEN` and JSON content type. Maximum body/frame size is
65,536 bytes. Successful receipt returns HTTP 204; authentication failure returns
401, invalid data 400, oversized data 413 and storage failure 503.

Frames use schema `neato.telemetry.v1`, a stable device ID, boot ID, a random
collector-session ID and increasing integer sequence. Gateway restarts create
new sessions; whole-robot restarts change the boot ID too. `sent_at_utc` is a UTC
ISO timestamp; monotonic timestamps help interpret clock corrections. The receiver
adds its own receipt timestamp and peer address.

`kind=sample` includes a metrics object; `kind=event` has metrics null. Both have
robot context, up to eight event summaries and sampler counters. Interval is
between 10 and 60 seconds (normally 30 idle / 15 active). The receiver rejects
nonfinite numbers, unsupported schema and invalid envelope fields.

Metrics include `memory_bytes`, `vm_counters`, `cpu`, `storage`, `disk`, `network`,
`processes`, `processes_truncated` and `native_rpc`. Missing process fields are
unknown; a process can exit during collection. RSS is not private memory and
must not simply be summed across shared-library processes. PSS/private/locked
readings may be older than the resource sample; check `pss_age_seconds`.

The sender has one pending frame, one in-flight HTTP request, a two-second socket
timeout and five-second failure backoff. Failed frames are dropped. Sequence gaps
and coalescing/failure counters are expected during outages. Arbitrary native
payloads and map binaries are omitted from event summaries.

The authenticated `GET /api/telemetry/v1/latest` endpoint returns up to eight recent
session summaries, each retaining the latest frame and latest resource sample.
An event-only frame does not discard the latest sample. In-memory counters/latest
summaries reset when the receiver restarts; JSONL captures survive.

`GET /healthz` is unauthenticated and identifies the receiver/schema only. All
state/capture endpoints require the configured bearer token. The reference HTTP
receiver is for a trusted LAN and does not configure CORS or expose a dashboard.
