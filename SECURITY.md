# Security and diagnostic sharing

Private installation folders are ignored by Git. Do not add robot backups,
`vendors.original.ini`, tokens, pairing codes, certificate private keys, factory
serial data, map files, HA config exports or full telemetry captures to commits
or issues. A pairing code contains both a control credential and TLS fingerprint.
Redact identifiers and payloads before sharing diagnostics.

The gateway's local network control API requires pinned TLS and bearer auth.
Discovery does not authorize a client. Stock Tron talks to localhost over TLS;
the installer preserves its previous configuration for rollback. Do not disable
certificate checks to work around a failed pairing.

The optional reference telemetry receiver uses authenticated HTTP on a trusted
LAN. It does not encrypt captures in transit and is not intended for public
exposure. The robot has bounded in-memory delivery and no telemetry disk queue.

Contact the repository owner privately for credential exposure or a security
issue affecting controls. Do not post real credentials in a public issue.
