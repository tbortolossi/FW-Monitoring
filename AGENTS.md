# AGENTS.md

Instructions for Codex and other coding agents working in this directory.

## Project Goal

This project should provide an easy-to-install Docker Compose stack for basic Palo Alto and Fortinet firewall monitoring, with useful capacity/load visibility.

The core use case is helping users understand what a firewall is actually doing with minimal setup. Prioritize CPU, RAM, sessions, CPS, disk where useful, interface status, and interface throughput over deep feature-specific monitoring.

Throughput must be calculated from cumulative interface octet counters. The standard Palo Alto and Fortinet dashboards use `ifHCInOctets` / `ifHCOutOctets`. The Palo Alto API dashboard is intentionally API-only and uses the MAC-level `port/rx-bytes` / `port/tx-bytes` counters of physical ports from `show counter interface all` (falling back to `ibytes` / `obytes` only when the `port` block is absent): `ibytes` / `obytes` are dataplane counters that miss hardware-offloaded flows. Do not use dataplane, NPU, session, or feature throughput summaries, which can be incomplete when traffic is offloaded or bypasses their counter path.

For CPU views, the standard SNMP dashboards should show both the global CPU and every per-processor/dataplane CPU exposed by the vendor MIB (`pan_hr_processors` for Palo Alto and `fortinet_processors` for Fortinet). The Palo Alto API dashboard must remain API-only and show management-plane CPU plus every dataplane/core returned by `resource-monitor`. The overview is useful for quick reading; per-CPU lines reveal imbalance and saturated dataplanes.

The expected user journey is:

1. Edit `firewalls.yml` with one or more firewall definitions.
2. Run `generate.py` on a Linux host with Docker, usually through the optional `generate.sh` bootstrap wrapper.
3. Start or refresh the stack with Docker Compose.
4. Open Grafana and use the provisioned dashboards for firewall monitoring.

Keep changes aligned with that goal: simple install, clear configuration, reliable SNMP polling, and dashboards that work out of the box.

## Repository Shape

- `docker-compose.yaml`: InfluxDB (published on `127.0.0.1:8086` only), custom Telegraf image, and Grafana (pinned 13.2.2, rationale in the file) services, with healthchecks.
- `firewalls.yml`: user-facing inventory for Palo Alto and Fortinet devices; `firewalls_example.yml` is the committed sample.
- `.firewalls.generated.yml`: generated inventory enriched from `firewalls.yml`; credentials redacted, mode `0600`, ignored by Git and safe to recreate.
- `generate.py`: main Python generator. It checks Docker, resolves `${VARIABLE}` references, runs SNMP discovery, enriches inventory, prepares MIBs, writes the API runtime files, renders `telegraf/telegraf.conf`, builds Telegraf, and starts the stack.
- `generate.sh`: optional convenience wrapper that creates a local `.venv`, installs `requirements.txt`, and executes `generate.py`.
- `paloalto_api_key.py`: PAN-OS API key helper; stores the key in `.env` (default `--storage env`) and writes an `${PALOALTO_API_KEY_<HOSTNAME>}` reference into `firewalls.yml`.
- `telegraf/header.tmpl`: common Telegraf agent (20 s interval) and InfluxDB output config.
- `telegraf/inputs_paloalto.tmpl`, `telegraf/inputs_fortinet.tmpl`: plain Jinja2 SNMP input templates (two instances per firewall, see below).
- `telegraf/inputs_paloalto_api.tmpl`: `inputs.execd` block for the API collector.
- `telegraf/paloalto_api_collector.py`: standard-library PAN-OS XML API collector (line protocol on stdout).
- `telegraf/Dockerfile`: custom Alpine Telegraf 1.40.1 image with Net-SNMP, standard and vendor MIBs, and the collector; keeps UID `999`.
- `telegraf/mibs/`: bundled vendor MIBs.
- `grafana/provisioning/`: Grafana datasources and dashboards.
- `scripts/build_paloalto_api_dashboard.py`: generates `Palo_API_Dashboard.json` and `Palo_API_Chassis_Dashboard.json`. Never hand-edit those two JSON files; edit the script and regenerate (CI checks for drift). Its panel factories (`timeseries`, `peak`, `xychart`, `right_axis`, ...) were also used to build the collapsed **Load Test** row of the three SNMP dashboards; keep the five sections structurally aligned (`test_every_dashboard_has_a_load_test_section`).
- `tests/`: unittest suite (branch coverage floor in `.coveragerc`).
- `docs/adr/`: architecture decision records.

## Important Behavior

- `firewalls.yml` is the main operator-facing config file. Secrets should be exact `${VARIABLE}` references resolved from `.env` or the process environment; direct values remain supported.
- Supported vendors are currently `paloalto` and `fortinet`.
- Palo Alto entries may omit `vendor`; templates and generation logic default missing vendor values to `paloalto`.
- Palo Alto system metrics use the shared measurement `pan_system`; `hostname` is a tag. Do not reintroduce per-host measurement names, because dashboards must work with multiple firewalls declared in `firewalls.yml`.
- Palo Alto VSYS metrics are important for multi-tenant or multi-context firewalls; keep the `vsys` measurement and prefer dashboards that can show global load plus per-VSYS sessions and CPS.
- SNMP polling uses two `[[inputs.snmp]]` instances per firewall. Fast (agent interval, 20 s): `pan_system`/`fortinet_system` scalars, `interfaces`, processors, `vsys`, `pan_zones`, `pan_interfaces_cps`, `pan_interface_utilization`. Slow (`interval = "60s"`): `pan_global_counters` as scalar GET fields (the instance name keeps them in that measurement), `pan_hr_storage`, `pan_hr_devices`, `pan_pa_cluster`, ENTITY tables in chassis mode; Fortinet VDOMs, hardware sensors, HA members. Both use `max_repetitions = 25`. Measurement, field, and tag names are the dashboard contract; keep them unchanged.
- MIB comparison notes: `panIfTable` exists in PAN-OS 10.2+, `panhrStorageUsage` and PA cluster summary objects appear in 11.2+, and `panVsysTotalCps` plus `panInterfaceUtilizationTable` appear in 12.1+. `generate.py` performs best-effort Palo Alto SNMP discovery, then infers `panos_10_2_metrics`, `panos_11_2_metrics`, `panos_12_metrics`, `vsys_total_cps`, `interface_utilization`, `chassis`, and `pan_entity_ext` into `.firewalls.generated.yml`. `enrich_inventory()` runs before and after discovery; flags it inferred are recomputed, values declared in `firewalls.yml` are never overwritten. `pa_cluster` is operator-only (default `false`) and gates `pan_pa_cluster`, because some PAN-OS releases stall `snmpd` on those objects. Keep the user-facing `firewalls.yml` simple unless an override is genuinely needed.
- `generate.py` also performs best-effort Fortinet SNMP discovery and should prefer discovered `fortios_version`, serial, model, and VDOM presence over user-declared values.
- SNMP discovery never puts credentials on a command line: `build_snmp_conf()` renders a Net-SNMP `snmp.conf` that is piped on stdin into the throwaway discovery container (`SNMPCONFPATH`). `SNMP_DISCOVERY_TIMEOUT` (default 2 s) and `SNMP_DISCOVERY=false` are the knobs. Keep it that way.
- For Palo Alto firewalls with `api_monitoring` enabled, `generate.py` runs a best-effort `check_paloalto_api_access()` (one `show system info` from the host, key in the `X-PAN-KEY` header only). It reports and never aborts; `API_CHECK=false` and `API_CHECK_TIMEOUT` (default 5 s) are the knobs.
- Keep `firewalls.yml` minimal. Chassis mode is normally inferred by `generate.py`; only document or use `chassis: true` as an advanced override when SNMP discovery cannot identify the platform.
- Palo Alto HOST-RESOURCES tables are collected for all Palo Alto devices so multi-DP appliances such as PA-5200 Series can expose per-processor load. Chassis mode additionally enables ENTITY, ENTITY-SENSOR, and ENTITY-STATE polling (with an `entity_name` tag) in `telegraf/inputs_paloalto.tmpl`. Keep this table-based where possible because sensor and slot indexes vary by platform.
- SNMP v2c and SNMP v3 are both represented in `firewalls.yml`; preserve both paths when changing templates.
- `snmp: false` makes a Palo Alto entry API-only (requires `api_monitoring.enabled`). `snmp_enabled()` in `generate.py` is the single test: such entries get no SNMP validation, discovery, enrichment, MIB download, SNMP secrets, or `[[inputs.snmp]]` instance. Keep new SNMP-side logic behind it.
- Generated Telegraf config is written to `telegraf/telegraf.conf`; it contains `$FIREWALL_SNMP_*` and API-key variable references, never secret values.
- Runtime secrets go to `telegraf/paloalto-api.env` (mode `0600`, injected by Compose `env_file`). Encoding contract: `compose_environment_value()` in `generate.py` wraps each value in double quotes and prefixes `\`, `"` and `$` with a backslash; newlines and NUL are rejected with an error naming the field only. `load_environment_file()` in the collector must decode the same format (and still accept the legacy single-quoted one). Change both sides and their tests together.
- Palo Alto XML API collector categories and schedules live in `CATEGORY_SCHEDULES`; `OPTIONAL_CATEGORIES` (`vsys`, `thermal`, `fans`, `power`, `ingress_backlogs`, `logging`, `globalprotect`, `software`, `raid`) are disabled per firewall after PAN-OS rejects the command. Chassis categories run only on PA-5450/7050/7080/7500; `raid` only on high-end or chassis models. Physical sensor and chassis power fields are always floats; counters stay integers. Do not change a field's type once written: InfluxDB rejects the new type until the shard rolls over.
- Grafana uses InfluxDB Flux with the datasource UID currently set to `P951FEA4DE68E13C5`; avoid changing it casually because dashboards may depend on it.
- Fortinet system metrics use the shared measurement `fortinet_system`; `hostname` is a tag. Do not reintroduce per-host measurement names, because dashboards must work with multiple firewalls declared in `firewalls.yml`.
- Fortinet entries can set optional `model` and `cluster` values; the Fortinet template collects system, interface, VDOM, processor, hardware sensor, and HA member metrics while preserving `cpu_pct`, `mem_pct`, `sessions_active`, and the shared `interfaces` measurement expected by dashboards.

## Commands

Typical install or refresh on the target Linux host:

```bash
./generate.sh
```

Direct run after `.venv` exists:

```bash
.venv/bin/python generate.py
```

Useful manual commands:

```bash
docker compose build telegraf
docker compose up -d
docker compose ps
docker compose logs -f telegraf
```

This workspace may be edited from Windows, but the generator is intended for Linux hosts. Be careful with line endings in shell scripts; keep `generate.sh` LF.

## Versioning and Releases

- The current project version is stored in `VERSION`.
- Use semantic versioning. Dashboard-only fixes and small operator-facing improvements are usually patch releases.
- When preparing a release, update both `VERSION` and `CHANGELOG.md` in the same commit.
- Tags use the `vX.Y.Z` format and should match `VERSION`.
- After tagging and pushing, create a GitHub Release for the tag when previous releases exist.
- Do not bump versions, create tags, or publish releases unless the user explicitly asks for a new version or release.

## Coding Guidelines

- Prefer small, practical changes that improve installability and reduce operator friction.
- Keep the stack Compose-based and avoid adding heavyweight dependencies unless they clearly simplify installation.
- Keep shell scripts POSIX/Bash-friendly and readable.
- Preserve UTF-8 encoding. Project files, comments, logs, and documentation should stay in English.
- Keep `firewalls.yml` examples clear and safe. Use placeholder IPs, usernames, passwords, tokens, and communities.
- Do not commit real firewall IPs, SNMP communities, SNMPv3 credentials, InfluxDB tokens, or Grafana passwords.
- If adding config values, document them in `firewalls.yml` comments or a README if one exists.
- Prefer vendor-specific templates over large conditional blocks when adding firewall-specific SNMP metrics.
- Keep MIB paths consistent with `telegraf/Dockerfile` and `docker-compose.yaml`.

## Security Notes

Runtime stack secrets belong in `.env`, created from `.env.example`. Keep `.env` ignored by Git and do not reintroduce real or sample service passwords directly in `docker-compose.yaml`.

Do not print secrets in logs, generated files beyond what Telegraf requires, or examples intended for sharing.

## Validation

After changing generation logic or templates, validate at least:

```bash
python3 -m py_compile generate.py
bash -n generate.sh
python3 -m pip install -r requirements.txt
```

When Docker is available, also validate:

```bash
./generate.sh
docker compose ps
docker compose logs --tail=100 telegraf
```

Dashboard Flux queries can be checked against a running stack before editing JSON: InfluxDB listens on `127.0.0.1:8086` on the Docker host, so `docker compose exec influxdb influx query '<flux>'` (or the HTTP API with the local token) shows whether a query returns the expected tables for a real firewall.

After changing `scripts/build_paloalto_api_dashboard.py`, regenerate both API dashboard JSON files with the script and run the dashboard tests.

For dashboard or datasource changes, confirm Grafana starts and the datasource still targets:

- URL: `http://influxdb:8086`
- organization: `netops`
- bucket: `firewalls`

## Agent Conduct

- Read the existing files before making structural changes.
- Do not delete generated or local artifact files unless the user explicitly asks.
- Do not rewrite dashboards wholesale unless the user asks for dashboard redesign.
- If adding support for another vendor, add a dedicated template, update `generate.py`, ensure MIB availability, and keep existing Palo Alto/Fortinet behavior unchanged.
- If a command needs network access, Docker image pulls, package installation, or writes outside this workspace, ask for approval first.
