# ADR 0001: Add PAN-OS XML API monitoring as an optional Telegraf collector

- Status: Accepted
- Date: 2026-09-21

## Context

SNMP provides reliable cross-platform counters, but PAN-OS also exposes cumulative hardware interface octets plus richer session and per-core dataplane performance data through operational XML API commands. The feature must support multiple firewalls and chassis without increasing firewall management-plane load unnecessarily or committing API keys.

## Decision

Run a standard-library Python collector under Telegraf `inputs.execd`. It schedules metric categories independently, serializes calls to each firewall, and parallelizes only between firewalls. The collector emits InfluxDB line protocol to Telegraf.

Keep API monitoring optional under each Palo Alto inventory entry. Allow `api_monitoring.host` to override the top-level SNMP address when HTTPS reaches the same device through another interface or DNS name. Accept either `api_key` directly in the ignored local `firewalls.yml`, matching the existing SNMP credential workflow, or `api_key_env` referencing `.env`. During generation, copy only the required API keys into a mode-`0600` runtime environment file for Telegraf, rather than exposing every stack secret to that container. Send keys in the `X-PAN-KEY` request header and verify TLS by default.

Use one API dashboard for compact and chassis platforms. Per-core CPU carries `dataplane` and `core` tags, and Grafana repeats a collapsible detail row for every dataplane. Collect resource pressure per dataplane, interface operational metadata, HA state, storage, and environmental sensors on slower schedules than session and octet counters.

Keep this dashboard API-only: calculate throughput from deltas of the per-interface hardware `ibytes` and `obytes` counters returned by `show counter interface all`. Do not use the session throughput summary. The standard dashboards continue using SNMP `ifHCInOctets` and `ifHCOutOctets`.

## Consequences

- Python is added to the custom Telegraf image, increasing the image size modestly.
- Operational API access and TCP reachability are required for enabled Palo Alto devices.
- A firewall certificate trusted by the container is recommended; lab users can explicitly disable verification.
- XML response formats can vary across PAN-OS releases, so parser fixtures and live-device checks are needed when expanding collected commands.
- Rollback is straightforward: remove or disable `api_monitoring`, rerun `generate.py`, and the existing SNMP collection remains unchanged.
