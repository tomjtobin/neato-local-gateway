# Neato Local Gateway

Run an already **Vacuula-bitten Neato D9** without a separate Vacuula server.
A small service on the robot supplies the login/time/notification compatibility
that stock Tron expects, and exposes a local HTTPS API to a Home Assistant
custom integration. Navigation, sensors, charging and cleaning remain handled
by Neato's installed software.

**Experimental; tested on one D9 running firmware 1.7.0-2933 and Home Assistant
2026.10.4.** D8/D10 and other releases are unverified; Gen3 D3–D7 are not supported.
The gateway runtime and controls have been used on a real D9. The portable
installer has automated tests but has not yet been used for a fresh second robot.

## Features

- HA discovery, USB-provisioned pairing and pinned HTTPS; no cloud account or MQTT.
- Start/resume, pause, Stop (pauses the run), and return to base.
- Eco/Turbo preference for the next new HA-started run.
- Battery percentage, charging state, current alerts, Problem sensor and live
  events, including observed docking and charging-start transitions.
- Native pushed state plus a 60-second quiet fallback, rather than continuously
  querying stock firmware every five seconds. Commands still fetch fresh state.
- Optional resource telemetry to your own computer, inside the same gateway
  process. No robot-side telemetry log or offline disk spool.

Not implemented: map editing/promotion, room selection, schedules, arbitrary
native commands, firmware replacement or a standalone phone/web control panel.
Received map/results uploads are retained as the latest two bounded requests and
acknowledged without map promotion. This does not expose map management in HA.

## Requirements

1. An already-bitten robot with working Wi-Fi and root access over its USB console.
   This repository does not perform the bite. See the [Vacuula project](https://github.com/vacuula/vacuula).
2. A macOS or Linux installation computer with Python **3.9+**, USB and OpenSSL
   **1.1.1+** (`req -addext` support). The robot uses its existing Python 3.7;
   you do not install pip or a second Python runtime on it.
3. Home Assistant **2026.10+**, reachable on the robot's LAN. Cross-VLAN discovery
   requires multicast routing; manual address setup is also available.

Keep the robot idle for installation and keep USB available through first
activation. The installer backs up the current vendor settings so rollback
returns it to its previous Vacuula endpoint. Leave that server available until
you have verified the local gateway.

## Install the robot gateway

Clone this repository and run from its root:

```sh
git clone https://github.com/tomjtobin/neato-local-gateway.git
cd neato-local-gateway

# Host-only download of four pinned, hash-verified pure-Python dependencies.
python3 gateway/download_dependencies.py

# Read-only inspection: current IPv4, existing in-use maps and Python/architecture.
python3 gateway/install_usb.py inspect
```

The console auto-selects one `/dev/cu.usbmodem*` (Mac) or `/dev/ttyACM*` (Linux)
port. If needed, add `--port /dev/ttyACM0` (or your actual Mac device path) to each
installer command. Close other serial applications first. If auto-login is not
enabled, log in as root yourself, close that serial application, then retry.

With exactly one in-use map, staging selects it without changing the map:

```sh
python3 gateway/install_usb.py stage
```

With no in-use map, explicitly opt into an unmapped cleaning profile:

```sh
python3 gateway/install_usb.py stage --allow-unmapped
```

With multiple in-use maps, select the UUID shown by `inspect`:

```sh
python3 gateway/install_usb.py stage --map-id YOUR_EXISTING_MAP_UUID
```

`--timezone Europe/London` overrides the robot's current timezone; `--mode turbo`
changes the initial next-run preference. These are configuration choices, not
movement commands. The saved-map path preserves no-go settings. Unmapped setup
fails closed if an in-use map later appears; reconfigure deliberately rather
than guessing which floor plan should be used.

On macOS, if the bundled OpenSSL lacks `-addext`, install OpenSSL 3 and select it:

```sh
brew install openssl@3
python3 gateway/install_usb.py stage --openssl "$(brew --prefix openssl@3)/bin/openssl"
# Include --allow-unmapped or --map-id above when applicable.
```

Staging writes the package to `/user/neato-gateway` and generates a new identity,
TLS certificate and token for **your** robot. It has not redirected Tron yet.
Private recovery/pairing files are saved in ignored `gateway-data/current/`.
Keep that directory private and backed up; never commit or publish it. For more
than one robot, use a separate `--directory gateway-data/NAME` consistently for
dependency download, staging, probing and updates.

Activate, enable boot startup and verify without issuing movement commands:

```sh
python3 gateway/install_usb.py activate
python3 gateway/install_usb.py status
python3 gateway/probe.py
```

Activation redirects stock Tron to verified TLS on localhost:9443 and restarts
Tron. The LAN API listens on HTTPS 8443. If native command permissions initially
remain empty, a normal dock/bumper state transition can refresh them; do not
start a run merely to establish installation readiness. USB can be removed
after successful verification; no installation computer is needed for HA control.

## Install the HA integration

Copy `custom_components/neato_gateway` into your HA configuration directory as
`/config/custom_components/neato_gateway`, then restart HA. Alternatively, add
this URL as an **Integration custom repository** in HACS; this repository has
standard HACS layout but has not been submitted to its default catalog.

HA should discover **Neato Gateway**. Add it and paste the contents of the private
`gateway-data/current/pairing-code.txt` file. This code contains the bearer token
and certificate fingerprint; mDNS discovery alone is not trusted pairing.

If discovery does not work, choose **Settings → Devices & services → Add
integration → Neato Gateway**, then enter the robot IP, port **8443**, and pairing
code. Use the robot address, not the installation computer or telemetry receiver.
After DHCP changes, HA verifies the same pinned identity before accepting a new
advertised address. `gateway/probe.py --host ROBOT_IP` can override its saved IP.

HA creates a vacuum, Next cleaning mode selector, Battery sensor, Charging binary
sensor, Current alert sensor, Problem binary sensor and Notification event entity.
The normal HA controls are sufficient; no custom dashboard is required.

Start one supervised test yourself: Start → Stop/Pause → Return home. Stop maps
to native pause so the robot keeps its run/navigation context. Some idle or
cancelled states cannot return home; the native firmware decides availability.
Eco/Turbo applies only to a new gateway-started clean, not a resumed run or a
physical-button start. These controls do not depend on resource telemetry.

For optional HA sidebar fault notifications, see
[the automation example](home_assistant/README.md). Phone push is a separate user
choice; installing this integration sends none.

## Update and rollback

```sh
git pull
python3 gateway/download_dependencies.py
python3 gateway/install_usb.py update
```

Updating preserves robot identity, settings and pairing, and restarts only the
gateway. After updating HA integration files, restart HA to load the Python code.
The portable installer changes in this repo are not automatically deployed to
any already-running robot.

To restore the vendor endpoint saved during staging:

```sh
python3 gateway/install_usb.py rollback
```

Rollback restores the saved vendor file, disables/stops the local gateway and
restarts Tron. It preserves source and recovery files for inspection. It does
not un-bite the robot or reinstall firmware. A rollback procedure exists and is
tested at the planning/source level, but has not been physically exercised on
the test D9. See [architecture and limitations](docs/architecture.md).

## Optional telemetry and development

[Resource telemetry](telemetry/README.md) is opt-in, uses your own receiver address
and never assumes a developer's computer exists. [API and events](docs/api.md)
describes the control contract and delivery limits.

```sh
python3 -m unittest discover -s gateway/tests -v
python3 -m unittest discover -s telemetry/tests -v
# In an environment with Home Assistant installed:
python3 -m unittest discover -s home_assistant/tests -v
python3 tools/check_public_tree.py
```

Tests use simulated native connections, temporary databases and local HTTP/PTY
fixtures. They do not operate a physical robot. CI runs host tests and HA runtime
contract checks. See [SECURITY.md](SECURITY.md) before sharing diagnostics.

Licensed under **AGPL-3.0-only**. No Neato firmware, proprietary Python modules,
robot backups, map data, device keys or real pairing material are included.
