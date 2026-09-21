# ADR 0001: Add PAN-OS XML API monitoring as an optional Telegraf collector

- Status: Accepted
- Date: 2026-09-21

## Context

SNMP provides reliable cross-platform counters, especially interface octets, but PAN-OS exposes richer session and per-core dataplane performance data through operational XML API commands. The feature must support multiple firewalls and chassis without increasing firewall management-plane load unnecessarily or committing API keys.

## Decision

Run a standard-library Python collector under Telegraf `inputs.execd`. It schedules metric categories independently, serializes calls to each firewall, and parallelizes only between firewalls. The collector emits InfluxDB line protocol to Telegraf.

Keep API monitoring optional under each Palo Alto inventory entry. Accept either `api_key` directly in the ignored local `firewalls.yml`, matching the existing SNMP credential workflow, or `api_key_env` referencing `.env`. During generation, copy only the required API keys into a mode-`0600` runtime environment file for Telegraf, rather than exposing every stack secret to that container. Send keys in the `X-PAN-KEY` request header and verify TLS by default.

Use one API dashboard for compact and chassis platforms. Per-core CPU carries `dataplane` and `core` tags. Continue to calculate throughput from SNMP `ifHCInOctets` and `ifHCOutOctets`, including on the API dashboard.

## Consequences

- Python is added to the custom Telegraf image, increasing the image size modestly.
- Operational API access and TCP reachability are required for enabled Palo Alto devices.
- A firewall certificate trusted by the container is recommended; lab users can explicitly disable verification.
- XML response formats can vary across PAN-OS releases, so parser fixtures and live-device checks are needed when expanding collected commands.
- Rollback is straightforward: remove or disable `api_monitoring`, rerun `generate.py`, and the existing SNMP collection remains unchanged.
