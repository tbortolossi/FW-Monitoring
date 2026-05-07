# AGENTS.md

Instructions for Codex and other coding agents working in this directory.

## Project Goal

This project should provide an easy-to-install Docker Compose stack for basic Palo Alto and Fortinet firewall monitoring, with useful capacity/load visibility.

The core use case is helping users understand what a firewall is actually doing with minimal setup. Prioritize CPU, RAM, sessions, CPS, disk where useful, interface status, and interface throughput over deep feature-specific monitoring.

Throughput must be calculated from interface octet counters (`ifHCInOctets` / `ifHCOutOctets`) for both Palo Alto and Fortinet. Dataplane, NPU, or feature counters can be incomplete when traffic is offloaded or bypasses the counter path.

For CPU views, dashboards should show both the global CPU and every per-processor/dataplane CPU exposed by the vendor MIB (`pan_hr_processors` for Palo Alto and `fortinet_processors` for Fortinet). The global line is useful for quick reading; per-CPU lines reveal imbalance and saturated dataplanes.

The expected user journey is:

1. Edit `firewalls.yml` with one or more firewall definitions.
2. Run `generate.py` on a Linux host with Docker, usually through the optional `generate.sh` bootstrap wrapper.
3. Start or refresh the stack with Docker Compose.
4. Open Grafana and use the provisioned dashboards for firewall monitoring.

Keep changes aligned with that goal: simple install, clear configuration, reliable SNMP polling, and dashboards that work out of the box.

## Repository Shape

- `docker-compose.yaml`: InfluxDB, custom Telegraf image, and Grafana services.
- `firewalls.yml`: user-facing inventory for Palo Alto and Fortinet devices.
- `.firewalls.generated.yml`: generated inventory enriched from `firewalls.yml`; ignored by Git and safe to recreate.
- `generate.py`: main Python generator. It checks Docker, prepares MIBs, enriches inventory, renders `telegraf/telegraf.conf`, builds Telegraf, and starts the stack.
- `generate.sh`: optional convenience wrapper that creates a local `.venv`, installs `requirements.txt`, and executes `generate.py`.
- `telegraf/header.tmpl`: common Telegraf agent and InfluxDB output config.
- `telegraf/inputs_paloalto.tmpl`: SNMP input template for Palo Alto devices.
- `telegraf/inputs_fortinet.tmpl`: SNMP input template for Fortinet devices.
- `telegraf/Dockerfile`: custom Telegraf image with Net-SNMP and vendor MIB support.
- `telegraf/mibs/`: bundled vendor MIBs.
- `grafana/provisioning/`: Grafana datasources and dashboards.

## Important Behavior

- `firewalls.yml` is the main operator-facing config file.
- Supported vendors are currently `paloalto` and `fortinet`.
- Palo Alto entries may omit `vendor`; templates and generation logic default missing vendor values to `paloalto`.
- Palo Alto system metrics use the shared measurement `pan_system`; `hostname` is a tag. Do not reintroduce per-host measurement names, because dashboards must work with multiple firewalls declared in `firewalls.yml`.
- Palo Alto VSYS metrics are important for multi-tenant or multi-context firewalls; keep the `vsys` measurement and prefer dashboards that can show global load plus per-VSYS sessions and CPS.
- MIB comparison notes: `panIfTable` exists in PAN-OS 10.2+, `panhrStorageUsage` and PA cluster summary objects appear in 11.2+, and `panVsysTotalCps` plus `panInterfaceUtilizationTable` appear in 12.1+. `generate.py` performs best-effort Palo Alto SNMP discovery, then infers these flags from discovered or declared `panos_version` into `.firewalls.generated.yml`; keep the user-facing `firewalls.yml` simple unless an override is genuinely needed.
- `generate.py` also performs best-effort Fortinet SNMP discovery and should prefer discovered `fortios_version`, serial, model, and VDOM presence over user-declared values.
- Keep `firewalls.yml` minimal. Chassis mode is normally inferred by `generate.py`; only document or use `chassis: true` as an advanced override when SNMP discovery cannot identify the platform.
- Palo Alto HOST-RESOURCES tables are collected for all Palo Alto devices so multi-DP appliances such as PA-5200 Series can expose per-processor load. Chassis mode additionally enables ENTITY, ENTITY-SENSOR, and ENTITY-STATE polling in `telegraf/inputs_paloalto.tmpl`. Keep this table-based where possible because sensor and slot indexes vary by platform.
- SNMP v2c and SNMP v3 are both represented in `firewalls.yml`; preserve both paths when changing templates.
- Generated Telegraf config is written to `telegraf/telegraf.conf`.
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
