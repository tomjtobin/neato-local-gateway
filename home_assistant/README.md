# HA adapter and optional notifications

Install the root `custom_components/neato_gateway` directory under HA's
`/config/custom_components`, or use this repository as a HACS custom Integration
repository. Restart HA after installing/updating Python files. The main README
covers USB provisioning and pairing.

The standard vacuum UI is used. Stop/Pause preserve the run; Start resumes it;
Return home performs the native pause/check/dock sequence when allowed. Battery
and charging are separate entities. Next cleaning mode configures Eco/Turbo for
new HA-started cleans. The event entity describes notifications/errors/alert-list
changes and observed docking/charging transitions. An `alerts_changed` event is
not necessarily a new fault; an empty alert list means reported alerts cleared.

## Sidebar notification example

The integration does not automatically create HA inbox or phone notifications.
To opt into retained sidebar messages, copy the rule in
`automations/neato_attention.yaml` into your `automations.yaml` list (or create it
in the YAML automation editor). Replace `REPLACE_WITH_HA_DEVICE_ID` with your
HA device registry ID from the device page URL, and update `neato_vacuum_entity`
if HA chose a different entity ID. Reload automations after configuration checks.

The example requires this device, the native error-code source and an error or
critical severity. Pickup is special: it additionally requires cleaning, returning
or paused, irrespective of severity, and uses a neutral “Robot lifted” title.
Routine idle handling should remain in event history without an inbox alert.
Cached/unknown HA state cannot prove exact mission context, so this conservative
filter can omit pickup inbox messages after the state has already become error.

A stable notification ID per robot/code suppresses repeated cards. Clearing the
robot alert does not dismiss the recorded inbox event; Current alert describes
live status. Persistence across every HA restart is not guaranteed. Phone push
requires your own chosen `notify.mobile_app_*` action and phone registration.
