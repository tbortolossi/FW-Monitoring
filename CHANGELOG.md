# Changelog

## Unreleased

### Added

- GitHub Actions CI for Python 3.11/3.12 tests, 75% branch-coverage enforcement, generated-dashboard drift detection, Compose validation, dependency auditing, repository secret/misconfiguration scanning, container builds, full image vulnerability reporting, and blocking of fixable high/critical findings.
- Unit coverage for generator validation, normalization, SNMP discovery, rendering, and stack orchestration, plus Dependabot update configuration.
- `${VARIABLE}` references in `firewalls.yml`, resolved from `.env` or the process environment, with environment storage now the API-key helper default.
- Interactive Palo Alto inventory selection in the API-key helper, with automatic reuse of declared API hosts and preservation of existing polling settings.
- Dedicated API-only Palo Alto chassis dashboard for slot inventory and live state, chassis power, environmental sensors, interfaces, and repeated per-dataplane details.
- API management-plane swap, I/O wait, task counts, and aggregated process CPU/memory metrics.
- Bounded collection of all active PAN-OS `severity drop` global counters with severity, category, aspect, rate, and description metadata.

### Fixed

- API environmental sensor values now always use floating-point fields, preventing InfluxDB integer/float type conflicts when PAN-OS changes numeric formatting between polls.
- Palo Alto SNMP HA history now keeps standalone devices as a valid time series instead of producing an empty Grafana timeline error.
- Explicit Palo Alto metric-table overrides are preserved during version enrichment, allowing problematic optional walks to be disabled without losing PAN-OS capability detection.

### Security

- Updated the custom collector base image from Telegraf 1.32 to 1.40.1 so CI can gate current OS and Go dependency vulnerabilities.
- Generated enriched inventory now redacts SNMP and API credentials and uses mode `0600`; the protected Telegraf runtime environment contains only required monitoring secrets, generated `telegraf.conf` no longer contains clear-text SNMP credentials, and `.env` is automatically restricted to mode `0600`.
- Added a security policy and a scoped, expiring risk acceptance for the gRPC-Go denial-of-service finding embedded in the latest Telegraf release; the shipped stack exposes no gRPC listener.
- The custom Telegraf image now declares its unprivileged runtime user explicitly and no longer installs the unnecessary `nano` package.
- Replaced the Debian-based Telegraf runtime with the official Alpine 3.23 variant. Standard IANA/IETF MIB text files are retained through a build-only stage, reducing the runtime HIGH/CRITICAL scan from 126 findings to the single documented upstream gRPC-Go finding; CI now smoke-tests the runtime UID and chassis MIB translations.

## 1.1.0 - 2026-09-22

### Added

- Optional Palo Alto XML API performance collector for sessions, management-plane CPU/RAM, per-core/dataplane CPU, hardware interface counters, system inventory, and a bounded set of global drop counters.
- Interactive `paloalto_api_key.py` helper that stores API keys directly in the ignored local inventory by default, with `.env` references available through `--storage env`.
- Direct `api_key` inventory configuration matching the existing SNMP credential workflow, while retaining `api_key_env` compatibility.
- Provisioned `Palo Alto API Performance Monitoring` dashboard supporting compact and multi-blade firewalls.
- API-only throughput calculated from cumulative PAN-OS hardware interface byte counters instead of SNMP data.
- Optional per-firewall API host override when SNMP and HTTPS reach the same Palo Alto device through different addresses.
- Collapsible API dashboard sections matching the standard dashboard layout, including per-interface throughput/status and one repeated CPU/resource row per dataplane.
- API-derived HA state, storage usage, interface metadata, environmental sensors, uptime, and per-dataplane resource pressure.

### Fixed

- Platform and PAN-OS version now render reliably as dashboard text values instead of empty stat panels.
- Dataplane CPU parsing no longer lets the maximum-load table overwrite the average-load series.

### Security

- API keys are injected through environment variables and sent in the `X-PAN-KEY` header; they are not written to generated Telegraf configuration or logged.
- TLS certificate verification is enabled by default and can only be disabled explicitly for lab use.

## 1.0.2 - 2026-05-12

### Added

- Collapsible HA role timeline in Palo Alto, Palo Alto chassis, and Fortinet dashboards to correlate Active/Passive role changes with firewall load.

## 1.0.0 - 2026-05-07

Initial public release.

### Added

- Docker Compose stack with InfluxDB 2.x, Telegraf, and Grafana.
- Generated Telegraf SNMP configuration from local `firewalls.yml`.
- Palo Alto firewall dashboard for CPU, MP RAM, sessions, VSYS, interfaces, errors/discards, storage, and drops.
- Palo Alto chassis dashboard for entity inventory, sensors, chassis power, processor load, blade status, memory, buffers, and storage.
- Fortinet firewall dashboard for CPU, memory, sessions, CPS, interfaces, VDOMs, disk, low memory, and processor drops.
- SNMPv2c and SNMPv3 inventory examples.
- Best-effort SNMP discovery for vendor, version, model, serial, VSYS/VDOM, and chassis-related flags.
- Docker bootstrap support from `generate.sh` on Debian/Ubuntu when run with `sudo`.
- Restart policy for services after host reboot.
- Rotating Telegraf logs under `logs/telegraf/`.
- Git ignore rules for local secrets, generated configs, runtime data, logs, and downloaded Palo Alto MIBs.

### Notes

- The stack is SNMP-only.
- Interface throughput is calculated from IF-MIB `ifHCInOctets` and `ifHCOutOctets`.
- Local runtime files such as `.env`, `firewalls.yml`, and `telegraf/telegraf.conf` are intentionally ignored by Git.
