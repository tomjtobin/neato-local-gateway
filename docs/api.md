# Local API and events (v1)

All LAN routes use HTTPS and `Authorization: Bearer TOKEN`. Pin the SHA-256
fingerprint provisioned over USB before sending credentials. mDNS advertises
`_neatogw._tcp.local.` and API version/device ID; those records are untrusted
until verified against the pinned identity.

| Route | Purpose |
|---|---|
| GET `/v1/info` | Identity, firmware, API/gateway version, capabilities, poll interval |
| GET `/v1/state` | Activity/readiness, current alerts, battery/dock/charging, next-run mode |
| GET `/v1/events` | Single-subscriber SSE stream |
| GET `/v1/diagnostics` | Bounded native RPC and optional telemetry counters |
| POST `/v1/commands` | `request_id` UUID plus `action`: start, stop, pause, return_home |
| GET `/v1/commands/UUID` | Recent command outcome |
| PUT `/v1/settings/cleaning-mode` | `{"mode":"eco"}` or `{"mode":"turbo"}` |

Activity and power fields become unknown when stale/disconnected. Docked and
charging are independent: a full robot can be docked without drawing charge.
Reported alerts are not proof that every physical sensor is current. Some missing
bin conditions may only appear after another native event or a start attempt.

Commands are serialized, journaled before dispatch and idempotent by request UUID.
A timeout/interrupted intent has unknown outcome; do not retry with a new UUID.
The journal retains at most 128 commands, preserving a 24-hour deduplication
window; it refuses commands if full. Mode writes occur only when changed.

SSE starts with `ready`, then `robot` frames with stable epoch/sequence IDs.
`Last-Event-ID` replays a bounded 64-event RAM ring. Restart/overflow produces an
explicit gap signal. This is not exactly-once durable event delivery. Event types
are `notification`, `error`, `alerts_changed`, `returned_to_dock`, `charging_started`.
HA adds `stream_gap` for disconnected replay history. Current alerts recover
separately through snapshots.

`returned_to_dock` means the native dock flag changed false→true, including manual
placement/adapter connection; it does not prove autonomous completion. Charging
has its own false→true event. Initial snapshots, missing flags, session changes
and expired continuity windows do not manufacture transitions. Receipt timestamps
are observations, not exact physical transition times.

Native error codes are preserved. The optional HA inbox automation selects one
source to avoid treating notification/error/alert-list changes as three separate
faults. Collector event summaries omit arbitrary payloads and map data.
