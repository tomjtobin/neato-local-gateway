# Architecture, compatibility and limitations

```
Stock robot/navigation/charging/UI
              ↕ existing stock IPC/HAL
            Stock Tron
              ↕ verified TLS, loopback port 9443
       Onboard Python gateway
              ↕ pinned HTTPS + bearer token, LAN port 8443
       HA custom integration
```

The gateway supplies the subset of the Orbital protocol needed for local
login/timezone/heartbeats, state and error/notification handling, no-update OTA
responses and bounded map receipt. It does not instantiate another NeatoRobot
object, HAL connection, navigation controller or map renderer. Stock software
continues to own physical operation. Native autonomous schedules and button
behavior have not been comprehensively validated against this replacement.

Native state events update the gateway immediately. A quiet-state fallback queries
once per 60 seconds; failed attempts are rate limited too. Independent heartbeats
are sent every 30 seconds. Display snapshots expire after 90 seconds or when the
native session changes/disconnects. Every movement command still fetches state
first and uses a separate 15-second freshness guard. HA polls cached HTTP state
every five seconds and maintains one SSE connection for events.

Stop is native pause, not cancel. Start resumes a resumable clean, otherwise uses
the saved profile. Return home pauses when necessary, rechecks permissions and
requests navigation return-to-base; some idle/cancelled states cannot do this.
There is no automatic retry of unknown movement outcomes. Native permission
refusal is surfaced to HA; HA's standard buttons do not always reflect every
native sub-state restriction.

Map uploads contain run metadata and compressed map data. The last two requests
are stored before returning 201 / `floorplan:false`. No promotion, decoding,
editing, deletion or external processing is implemented. Receipt does not imply
that arbitrary saved maps can be reconstructed from these uploads.

The API uses a generated TLS identity and credential unique to each staged
installation. HA pins the USB-provisioned certificate fingerprint; discovery is
not authorization. Do not configure a reverse proxy or expose the control API to
the internet as part of this setup. Certificates last ten years; rotation and
pairing re-provisioning require deliberate USB administration.

## Validation scope

- Physically tested: one D9, firmware 1.7.0-2933, Python 3.7.8 on aarch64;
  HA 2026.10.4. Start, pause/Stop, resume, return to base, dock/charging flags and
  native fault events have been observed. Eco/Turbo parameter persistence is
  verified, but comparative cleaning performance is not.
- Other firmware/models are not asserted compatible. The installer requires
  root USB access, stock Linux layout, the factory/maps SQLite schemas, `wlan0`,
  Python 3.7 standard libraries and systemd. It is POSIX host code, not a Windows
  installer or a firmware-biting tool.
- New portable installation planning, dependency verification and serial protocol
  handling are tested without hardware. A fresh second-device install and physical
  rollback remain unverified. Keep original vendor endpoint and USB recovery
  available during first adoption.
- Memory exhaustion occurred in testing. An old external-server baseline also
  retained substantial native working memory. Reduced status polling and optional
  resource telemetry support investigation; neither proves the cause fixed.
  The gateway is roughly 24–28 MiB RSS in early observations on the test robot;
  workload peaks and long-term behavior require more validation.

## Recovery

`rollback` restores the vendor file captured during staging, disables the gateway
boot service, stops it and restarts Tron. It does not restore a full firmware
image, revert the bite, reset Wi-Fi, or erase maps. If the original remote Vacuula
server is unavailable, restoring its address alone cannot restore service.

`stage` refuses an already-staged robot or reuse of an existing local identity
folder. After an uncertain upload, inspect before retrying; a lost host-side
acknowledgement does not establish that the robot wrote nothing. The installer
keeps source/config/recovery files under `/user/neato-gateway` and a systemd unit
under `/etc/systemd/system`. These files are not removed by rollback.

Fresh installer units retain bounded lifecycle/warning messages in systemd journal;
HTTP access/payload logging remains disabled. Existing units from an older manual
installation are not rewritten by a source-only update.
