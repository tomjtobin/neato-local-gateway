# Optional resource telemetry

Telemetry is opt-in. The robot gateway works with HA without a collector computer.
When enabled, collection runs inside the existing gateway process, with two extra
threads and no second Python interpreter, HAL instance or ZeroMQ subscription.

## Run a receiver on your own computer

Choose that computer's reachable LAN IPv4 address and an unused port:

```sh
python3 telemetry/receiver.py init --directory receiver-data --bind YOUR_COMPUTER_IP --port 8766
python3 telemetry/receiver.py serve --directory receiver-data
```

Keep that process running, or supervise it using your own service manager.
While the robot is idle and USB-connected:

```sh
python3 gateway/install_usb.py telemetry-enable --telemetry-directory receiver-data
python3 telemetry/monitor.py --directory receiver-data --watch
python3 gateway/probe.py --diagnostics
```

The installer checks the receiver health endpoint, writes private optional config
and compatible source, then restarts only the gateway. The endpoint is not fixed
to any development computer. To disable later:

```sh
python3 gateway/install_usb.py telemetry-disable
```

## Sampling and overhead

- 30-second idle and 15-second cleaning/returning/paused resource samples.
- Existing gateway error/dock/charging event summaries can produce event-only
  frames without another resource scan. Raw internal navigation IPC subscriptions
  from the earlier standalone experiment are not used.
- Up to 64 resident userspace processes, with RSS, anonymous/file/shared memory,
  threads, FD counts, process identity/start ticks and CPU usage. Bounds and
  truncation are explicit. Zero-RSS kernel threads are omitted.
- PSS/private/resident locked memory for the largest ten processes every five
  minutes, or at most once/minute below 64 MiB available RAM. Cached readings
  include their age. Virtual locked size is distinct from physical locked RAM.
- System available/free/CMA/locked/slab memory, faults/OOM counts, CPU, storage,
  disk/network counters, native RPC counts/latency/timeouts and pending requests.

Reads use procfs/sysfs; they do not issue native `state.show` requests. The quiet
60-second native fallback and pre-command checks are unchanged. Snapshots on the
test D9 suggested about 1 MiB extra proportional memory, not the 15–17 MiB RSS of
a separate sampler process. This is an early estimate, not an overhead guarantee.

## Storage and delivery

The robot writes no telemetry log or offline spool. The sender retains one pending
frame plus one in-flight request, each capped at 64 KiB. A missing/sleeping receiver
causes frames to be dropped, not accumulated on robot storage. Counters expose
failures/coalescing/loss. This is diagnostic telemetry, not a lossless audit log.

Only the receiver computer stores JSONL captures. Rotation bounds the default
storage to a current 10 MiB file and five backups. The time window depends on
sample/event volume. Receiver config and captures are private and ignored by Git.
The reference receiver uses bearer-authenticated HTTP on the LAN, not encryption;
keep it on a trusted network. HTTPS endpoints are supported by the sender with
normal certificate validation, but the reference receiver does not terminate TLS.

[Wire contract](receiver-spec.md). Initial battery/state fields may be unknown
until Tron supplies its first valid snapshot. The collector sends no argv,
environment variables, raw map images or credentials as diagnostic fields.
