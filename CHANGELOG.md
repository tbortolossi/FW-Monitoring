# Changelog

## 1.4.2 - 2026-09-24

### Added

- A collapsed **Load Test** section right under the overview of the five dashboards (Palo Alto SNMP, Palo Alto Chassis SNMP, Fortinet SNMP, Palo Alto API, Palo Alto API Chassis) for capacity ramps driven by a traffic generator: eight peak tiles reduced over the selected time range (throughput received and sent, packets/s, CPS, sessions, dataplane CPU, packet buffer or busiest processor, packets dropped during the range), a **Throughput vs CPU** ramp with the dataplane CPU on a right axis, a **CPU vs Throughput** scatter plot (one point per minute, the curve of vendor test reports), packet and connection rates, sessions with session-table utilization (NPU offload on Fortinet), and dataplane drops with interface errors. No collector or SNMP template change; Grafana reloads the provisioned files. Usage is described in the README section "Follow a Load Test".

## 1.4.1 - 2026-09-23

### Fixed

- Palo Alto API dashboards showed **MP RAM: No data** on large management planes such as the PA-5580. `top` reports a value wider than its column as `1031206.+total`; the collector now accepts it when only the decimal was dropped, and still ignores a line whose integer digits may be missing rather than writing a wrong value. Rebuild the Telegraf image (`./generate.sh`) to apply.

## 1.4.0 - 2026-09-23

### Added

- API-only Palo Alto firewalls: `snmp: false` on an entry with `api_monitoring` enabled skips SNMP credentials, discovery and polling for that firewall, so a device reachable only over HTTPS is monitored by the XML API collector alone. It is rejected on Fortinet entries and without an enabled API block.
- `README.md` has an "Install on Ubuntu" section (packages, `git clone`, Docker, `docker` group) and an installation troubleshooting table.
- `generate.py` checks Palo Alto XML API access for every firewall with `api_monitoring` enabled: one read-only `show system info` per firewall, run in parallel, printing `API OK` with model and PAN-OS version, or why it failed (key rejected, untrusted TLS certificate, unreachable, missing key). The check never stops generation or prints the key; `API_CHECK=false` skips it and `API_CHECK_TIMEOUT` (default 5 s) bounds it.

### Fixed

- `generate.py` now restarts Telegraf after `docker compose up -d`. `telegraf.conf` and `paloalto-api.json` are bind mounts, so Compose did not recreate the container when only their content changed and a rerun of `./generate.sh` (new firewall, `verify_tls: false`, changed credentials) was silently ignored until a manual `docker compose restart telegraf`.
- `generate.py` checks that the Docker daemon is reachable before doing any work and explains how to fix a `docker.sock` permission error (add the user to the `docker` group) or a stopped daemon, instead of failing later with a traceback during the discovery image build.
- `sudo ./generate.sh` adds the user who ran it to the `docker` group when it installs Docker, so later runs work without `sudo`.
- `generate.sh` is now stored as executable in Git, so `./generate.sh` works right after `git clone` without `chmod +x`.

## 1.3.0 - 2026-09-23

### Added

- Every panel of the two Palo Alto API dashboards now shows, behind the (i) icon in its header, the PAN-OS CLI command the collector runs to obtain the data and its default polling interval; the builder derives it from a measurement-to-command table checked against the collector by the tests.
- API per-VSYS CPS, packet rate and TCP/UDP/ICMP session counts from `show session info` scoped to each VSYS (`paloalto_api_vsys` fields `cps`, `packet_rate_pps`, `sessions_tcp`, `sessions_udp`, `sessions_icmp`), with a **VSYS CPS** panel in the repeated VSYS section of both API dashboards.
- The repeated per-interface **Throughput** and **Errors / Discards** panels of the API dashboards now include subinterfaces, tunnels, VLAN, loopback and aggregate interfaces from the logical `ifnet` counters, matching the SNMP dashboard; physical ports keep the hardware counters.
- **Scan / Packet-Based Drops** panel (scan, packet-based attack, IPv6 and DoS session-accounting counters) in the API "Data Plane Pressure and Key Drops" section, equivalent to the SNMP panel.

### Changed

- `README.md` explains how to write `firewalls.yml` step by step (minimal SNMPv2c/SNMPv3 entries, `${VARIABLE}` secrets, `api_monitoring` block) and documents how `paloalto_api_key.py` works: menu, key generation, what it writes into `firewalls.yml`, the `--storage env` (default, key in `.env`) versus `--storage yaml` (key in the inventory) choice, and every command-line option.

### Fixed

- VSYS slots that `show session meter` lists but that are not configured on the firewall (PAN-OS answers "You must specify a valid vsys") are no longer written to `paloalto_api_vsys`; they are probed again every `system_interval` (default one hour). Existing stale points can be removed with an InfluxDB delete on `_measurement="paloalto_api_vsys" AND vsys="<name>"`.
- `request_xml()` in the collector keeps the PAN-OS explanation of HTTP 4xx answers in the error message.

## 1.2.0 - 2026-09-22

Behavior changes to review before upgrading (see "Upgrade from v1.0.2" and "Upgrade from v1.1.0" in `README.md`):

- API sensor fields `min`, `max`, `rpm` (and any other physical value that v1.1.0 wrote as an integer) and chassis power figures are now always floats; installations with existing `paloalto_api_sensors` data can see InfluxDB field-type conflicts until the next shard group or until that history is deleted.
- `fortinet_hw_sensors.value` is now a float instead of a string, with the same temporary field-type conflict on existing installations.
- The `pan_pa_cluster` table is polled only with the new operator flag `pa_cluster: true`; it no longer follows PAN-OS 11.2+ detection.
- `pan_interfaces_cps` (`panIfTable`) is polled only when PAN-OS 10.2+ is discovered or declared (`panos_10_2_metrics`).
- SNMP polling uses a second 60-second instance per firewall; global counters, storage, host-resource devices, ENTITY tables, Fortinet VDOMs, hardware sensors, and HA members refresh every minute instead of every 20 seconds.
- InfluxDB is published on `127.0.0.1:8086` only.
- Grafana is upgraded from 11.1.4 to 13.2.2; its database migrates automatically on first start and cannot be opened again by 11.1.4.
- Docker Compose v2.24 or later is required (optional `env_file` entry).

### Added

- GitHub Actions CI for Python 3.11/3.12 tests, 85% branch-coverage enforcement, generated-dashboard drift detection, Compose validation, dependency auditing, repository secret/misconfiguration scanning, container builds, full image vulnerability reporting, and blocking of fixable high/critical findings.
- Unit coverage for generator validation, normalization, SNMP discovery, template rendering, dashboard provisioning, collector runtime, and stack orchestration, plus Dependabot update configuration.
- `${VARIABLE}` references in `firewalls.yml`, resolved from `.env` or the process environment, with environment storage now the API-key helper default.
- Interactive Palo Alto inventory selection in the API-key helper, with automatic reuse of declared API hosts and preservation of existing polling settings.
- Dedicated API-only Palo Alto chassis dashboard for slot inventory and live state, chassis power, environmental sensors, interfaces, and repeated per-dataplane details.
- API management-plane swap, I/O wait, task counts, and aggregated process CPU/memory metrics.
- Bounded collection of all active PAN-OS `severity drop` global counters with severity, category, aspect, rate, and description metadata.
- Dedicated API-only throughput and ingress error/discard panels repeated for every active physical Ethernet port, with global throughput excluding internal and logical aggregate counters.
- API dashboards now match the SNMP dashboards: the chassis API dashboard includes the full main view (sessions, CPS, session utilization, and repeated per-interface throughput and error panels), and both dashboards include per-VSYS sections (sessions and throughput by zone) and curated policy-deny, DoS, zone-protection, SYN-cookie, and block-table panels.
- API current-load strip with thresholds, a hottest-core line per dataplane, an active-core CPU summary and per-core load map, a link-utilization table, worst-dataplane resource pressure, global drop rate by category, and a top-counters table with PAN-OS descriptions.
- API collection of per-VSYS sessions (`show session meter`), logical interface counters tagged with zone and VSYS (throughput and per-reason drops), hardware port transmit errors and link-down counts, DoS-aspect global counters, and HA peer state and configuration/session synchronization.
- Optional API commands that a platform rejects, or that the API administrator role is not authorized to run, are disabled for that firewall after the first failure instead of being logged on every poll.
- New optional API categories, each disabled automatically on platforms that reject it: `ingress_backlogs` (`show running resource-monitor ingress-backlogs`, per-dataplane usage and session count), `logging` (`debug log-receiver statistics`, log rates and discarded counters), `globalprotect` (gateway user counts), `software` (`show system software status`, per-process running state), and `raid` (`show system raid detail`, high-end and chassis models only).
- API per-core dataplane peak `cpu_max_pct` next to `cpu_pct`.
- Extra API HA fields (HA1/HA2 link status, link and path monitoring, local and peer priority, preemption, state reason and duration) and system-info fields (App-ID, Threat, antivirus, WildFire and URL-filtering content versions, device certificate status, operational mode, multi-VSYS, family).
- API dashboards: "Ingress Backlog by Dataplane" overview panel, a collapsed "Logging and Management Health" row (log rate, logs discarded, content versions, processes not running, GlobalProtect users, RAID), an "HA Links and Monitoring" table, and the one-minute peak in the hottest-core series.
- API chassis dashboard: RAID state in the slot inventory row.
- Generator flags `panos_10_2_metrics` (inferred from PAN-OS 10.2+) and `pa_cluster` (operator-only, default `false`), documented with the other advanced overrides in `firewalls_example.yml` and `README.md`.
- `SNMP_DISCOVERY_TIMEOUT` environment override for the discovery timeout (default 2 seconds, one retry).
- Healthchecks for InfluxDB and Grafana; Telegraf starts only after InfluxDB is healthy.
- ADR 0002 describing the SNMP fast/slow instances, the GET-based global counters, the `snmp.conf` discovery, and the runtime env-file encoding.

### Changed

- API dashboards: the current-load tiles now carry a 30-minute sparkline, the CPU, RAM, session-utilization, resource-pressure and per-core panels draw dashed 70/90% guide lines, the global drop rate is stacked by category, and the environmental section is split into temperature, fan, power and alarm panels on both API dashboards.
- API chassis dashboard: new uncollapsed chassis health strip (cards up / not up, power budget used, hottest sensor, sensor alarms, slowest fan) and a colored per-slot state timeline directly under the load strip.
- SNMP Palo Alto chassis dashboard: ENTITY-SENSOR values are scaled with `entPhySensorScale`/`entPhySensorPrecision`, split into temperature, fan and voltage/current/power panels, and labelled with the new `entity_name` tag; the blade device status table pivots status and error counts into columns with MIB-accurate colors (running green, warning orange, down red).
- API per-core dataplane CPU now reads `show running resource-monitor minute last 1` (one-minute average) instead of a one-second sample.
- SNMP templates: two `[[inputs.snmp]]` instances per firewall. The fast instance (20 s) keeps `pan_system`/`fortinet_system` scalars, interfaces, processors, VSYS, zones, `pan_interfaces_cps`, and `pan_interface_utilization`; the slow instance (60 s) holds the PAN-OS global counters, now fetched as scalar GET fields instead of one walk per counter, plus `pan_hr_storage`, `pan_hr_devices`, `pan_pa_cluster`, and the chassis ENTITY tables (Fortinet: VDOMs, hardware sensors, HA members). Both use `max_repetitions = 25`. Measurement, field, and tag names are unchanged.
- `pan_entity_sensors` and `pan_entity_states` gain an `entity_name` tag; `fortinet_hw_sensors.value` is converted to a float; unused `data_type` lines are removed and the templates are plain Jinja2.
- `.firewalls.generated.yml` flags are re-inferred after SNMP discovery while values declared in `firewalls.yml` are preserved, so the discovered model and version now drive `chassis`, `pan_entity_ext`, and the version flags.
- `telegraf/paloalto-api.env` values are double-quoted with `\\`, `\"`, and `\$` escapes so they round-trip exactly through Docker Compose; the collector's `--env-file` loader accepts this format and the legacy single-quoted one.
- Grafana is pinned to 13.2.2 (from 11.1.4), with the compatibility rationale documented in `docker-compose.yaml`.
- The generator configures its log file only when run as a script, not at import time.
- `grafana-data/` and `logs/telegraf/` permissions are widened only when the container user cannot already write; without root, the generator warns and suggests `sudo chown -R 472:472 grafana-data`.
- `.coverage` and `coverage.xml` are ignored by Git.

### Fixed

- SNMP Palo Alto storage and packet-buffer usage panels fall back to `hrStorageUsed / hrStorageSize` when `panhrStorageUsage` is unavailable (PAN-OS before 11.2 or `panos_11_2_metrics: false`) and use a 0-100% axis.
- Fortinet "CPU Per Processor" no longer averages the 1-minute and 5-second series under one label; it plots the 1-minute value per processor.
- Fortinet "Disk / Low Memory" converts MB and KB fields to bytes so both pairs share one axis; the HA role timeline uses the FortiOS primary/secondary/standalone wording and keeps standalone units.
- Interface error/discard and drop-rate panels use packets per second instead of `ops` on every dashboard.
- Every API physical sensor value (temperature, fan RPM, watts, volts, amps, value, min, max) and chassis power figure is now always a float, preventing InfluxDB integer/float type conflicts when PAN-OS changes numeric formatting between polls.
- The API sessions parser honors alias priority (for example `num-active` over `num-installed`) regardless of XML element order.
- The API HA group identifier is read from `group-id` instead of the `group` container element.
- Chassis detection works on first install: flags inferred before discovery are recomputed once SNMP discovery has found the real model.
- Palo Alto SNMP HA history now keeps standalone devices as a valid time series instead of producing an empty Grafana timeline error.
- Explicit Palo Alto metric-table overrides are preserved during version enrichment, allowing problematic optional walks to be disabled without losing PAN-OS capability detection.

### Security

- Updated the custom collector base image from Telegraf 1.32 to 1.40.1 so CI can gate current OS and Go dependency vulnerabilities.
- Generated enriched inventory now redacts SNMP and API credentials and uses mode `0600`; the protected Telegraf runtime environment contains only required monitoring secrets, generated `telegraf.conf` no longer contains clear-text SNMP credentials, and `.env` is automatically restricted to mode `0600`.
- SNMP discovery credentials are piped to the discovery container as a Net-SNMP `snmp.conf` on stdin instead of command-line arguments, so they no longer appear in `ps`, `docker inspect`, or audit logs.
- InfluxDB is published on `127.0.0.1:8086` only; Grafana and Telegraf keep using the Compose network.
- Grafana 13.2.2 replaces the unsupported 11.1.4 release (affected by later CVEs, including CVE-2026-27876); self sign-up, usage reporting, and update checks are disabled.
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
