# Firewall Monitoring Starter

Palo Alto and Fortinet firewall monitoring stack with Docker Compose, Telegraf, InfluxDB, and Grafana. SNMP is the baseline; Palo Alto devices can optionally add read-only PAN-OS XML API performance polling.

![FW-Monitoring dashboard](docs/assets/FW-Monitoring.png)

Docker Compose stack for quick Palo Alto and Fortinet firewall monitoring with Telegraf, InfluxDB, and Grafana.

The goal is simple operational visibility: CPU, memory, sessions, CPS, disk where useful, interface status, errors/discards, and throughput. It is useful when you need a quick factual view of firewall load without deploying a full NMS.

The standard Palo Alto and Fortinet dashboards calculate throughput from IF-MIB interface counters (`ifHCInOctets` and `ifHCOutOctets`). The optional API-only Palo Alto dashboard instead uses the hardware interface byte counters returned by `show counter interface all`. Neither path uses session or feature throughput summaries, which can miss offloaded traffic.

## What You Get

- InfluxDB 2.x for time series storage
- Telegraf SNMP polling generated from `firewalls.yml`
- Optional Palo Alto XML API polling for sessions, management-plane resources, per-core/dataplane CPU, interface state and throughput, HA, storage, environmental sensors, and selected drop counters
- Grafana with provisioned InfluxDB datasource
- Five monitoring dashboards:
  - `Palo Alto Firewall Monitoring`
  - `Palo Alto Chassis Monitoring`
  - `Fortinet Firewall Monitoring`
  - `Palo Alto API Performance Monitoring`
  - `Palo Alto API Chassis Monitoring`
- Best-effort SNMP discovery before Telegraf config generation

## Common Tasks

- [Install or regenerate the stack](#quick-start)
- [Open Grafana and view a dashboard](#open-grafana-and-view-dashboards)
- [Upgrade an existing installation](#upgrade-an-existing-installation)
- [Configure Palo Alto XML API monitoring](#palo-alto-xml-api-setup)

## Requirements

- Linux host with Docker and Docker Compose v2
- Python 3 with `venv` and `pip`
- UDP/161 reachable from the Docker host to each firewall
- For optional Palo Alto API monitoring, TCP/443 (or the configured API port) reachable from the Telegraf container
- SNMP enabled on the firewall management interface or the interface you poll
- A local `.env` file based on `.env.example`
- A local `firewalls.yml` file based on `firewalls_example.yml`

Palo Alto MIB files are downloaded by the generator when needed and are ignored by Git.

## Quick Start

1. Create local secrets:

```bash
cp .env.example .env
nano .env
```

Change every `CHANGE_ME...` value.

2. Create a local firewall inventory:

```bash
cp firewalls_example.yml firewalls.yml
```

3. Configure SNMP on the firewalls. Examples are below. For Palo Alto API monitoring, also follow the XML API setup section.

4. Edit `firewalls.yml`:

```bash
nano firewalls.yml
```

5. Bootstrap the local Python environment:

```bash
./generate.sh
```

This optional wrapper creates `.venv`, installs the Python requirements, then runs `generate.py`.

If Docker is missing on a Debian/Ubuntu host, run the wrapper with `sudo` once. It installs Docker Engine from the official Docker repository, including the GPG keyring and Docker Compose plugin, then continues the generator:

```bash
sudo ./generate.sh
```

After the first run, you can call the Python generator directly:

```bash
.venv/bin/python generate.py
```

The Python generator discovers the firewalls over SNMP, renders `telegraf/telegraf.conf`, and starts the Docker Compose stack.

The Compose services use `restart: unless-stopped`, so they come back automatically after a host reboot as long as Docker starts on boot.

6. [Open Grafana and select a dashboard](#open-grafana-and-view-dashboards).

## Open Grafana and View Dashboards

First confirm that the three services are running:

```bash
docker compose ps
```

Open one of these addresses in a browser:

```text
# Browser running on the Docker host
http://localhost:3000

# Browser running on another machine
http://<docker-host-ip>:3000
```

Run `hostname -I` on the Docker host if you do not know its IP address. Use an address reachable from the browser's network.

Sign in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from the local `.env` file. These values initialize the administrator account on the first start. Changing them later does not automatically change the password already stored in `grafana-data/`.

In Grafana:

1. Open **Dashboards**.
2. Select the required dashboard:
   - **Palo Alto API Performance Monitoring** for the API-only Palo Alto view.
   - **Palo Alto API Chassis Monitoring** for API-only high-end and modular platform details.
   - **Palo Alto Firewall Monitoring** for the standard Palo Alto SNMP view.
   - **Palo Alto Chassis Monitoring** for chassis-specific SNMP metrics.
   - **Fortinet Firewall Monitoring** for Fortinet devices.
3. Use the **hostname** selector at the top of the dashboard when several firewalls are configured.
4. Select a time range that includes recent data. New API metrics may need one or two polling intervals before every panel is populated.

If Grafana opens locally but not from another computer, allow inbound TCP port `3000` from the trusted administration network on the Docker host firewall. Do not expose Grafana directly to the public internet; use a restricted network or a TLS reverse proxy for remote access.

If the page opens but a dashboard has no data, check:

```bash
docker compose ps
docker compose logs --tail=100 telegraf
tail -100 logs/telegraf/telegraf.log
```

## Python Generator

The project generator is Python-based:

- `generate.py` contains the generation logic and is the main entry point.
- `generate.sh` is only a convenience wrapper for creating `.venv`, installing dependencies, and launching `generate.py`.
- `requirements.txt` contains the Python dependencies: `PyYAML` and `Jinja2`.
- `requirements-dev.txt` adds the coverage and dependency-audit tools used by CI.

The custom Telegraf runtime uses the official `telegraf:1.40.1-alpine` image. A build-only Debian stage downloads the standard IANA/IETF MIB corpus required for ENTITY-based chassis monitoring; Debian packages are not copied into the final image. The runtime preserves Telegraf UID `999` so log volumes created by earlier Debian-based releases remain writable during an in-place upgrade. The resulting runtime supports `amd64` and `arm64`. The upstream Alpine image does not publish an `arm/v7` variant; use a supported 64-bit host architecture.

On each run, the generator:

- loads and validates `firewalls.yml`
- performs best-effort SNMP discovery for version, model, serial, VSYS/VDOM, and chassis-related flags
- writes `.firewalls.generated.yml`
- writes the API-only runtime inventory to `telegraf/paloalto-api.json` (without API keys)
- writes only the required Palo Alto keys to the protected `telegraf/paloalto-api.env` runtime file, so Telegraf does not receive unrelated `.env` secrets
- downloads Palo Alto MIB files when needed
- renders `telegraf/telegraf.conf`
- builds the Telegraf image
- starts or refreshes the Docker Compose stack
- configures services to restart automatically after host reboot
- writes a timestamped log under `logs/`

For closed environments, preload a local wheel directory and point pip at it:

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=./wheelhouse ./generate.sh
```

You can also run the Python script directly after the virtual environment exists:

```bash
.venv/bin/python generate.py
```

`sudo` is not required when your user can run Docker and Docker is already installed. When the generator is run as root, it fixes `grafana-data/` ownership for Grafana UID `472`.

## Firewall Inventory

`firewalls.yml` is intentionally simple. In normal use, only declare the hostname, management IP, vendor, SNMP version, and SNMP credentials. Do not declare PAN-OS or FortiOS versions manually. The generator polls SNMP first and writes an ignored `.firewalls.generated.yml` with discovered version/model/feature flags.

`firewalls.yml` is ignored by Git because it usually contains real firewall IPs and SNMP credentials. Commit changes to `firewalls_example.yml` when you want to improve the sample inventory.

### Reference Secrets from `.env`

The recommended configuration keeps secrets out of YAML. Put each secret in the local `.env` file, then use an exact `${VARIABLE_NAME}` reference as the YAML value. The generator resolves these references before validation; the process environment takes precedence over `.env` when both define the same name.

```dotenv
PA_PARIS_SNMP_AUTH=CHANGE_ME_AUTH_PASSWORD
PA_PARIS_SNMP_PRIV=CHANGE_ME_PRIV_PASSWORD
PALOALTO_API_KEY_PA_440=CHANGE_ME_PALO_ALTO_API_KEY
```

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${PA_PARIS_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${PA_PARIS_SNMP_PRIV}
  api_monitoring:
    enabled: true
    api_key: ${PALOALTO_API_KEY_PA_440}
```

The reference must occupy the complete YAML scalar; embedded forms such as `prefix-${NAME}` are not expanded. Generation stops with the missing variable name—but never its value—when a reference cannot be resolved. The generator enforces mode `0600` on `.env`; do not commit it. Direct values remain supported for backward compatibility, but environment references reduce secret duplication and are recommended for new installations.

Minimal Palo Alto SNMPv3:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${PA_440_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${PA_440_SNMP_PRIV}
  api_monitoring:
    enabled: true
    # Optional: set this only when API and SNMP use different addresses.
    # host: 192.0.2.201
    api_key: ${PALOALTO_API_KEY_PA_440}
    verify_tls: true
    interval: 20
    resource_interval: 60
    counter_interval: 60
```

Minimal Fortinet SNMPv3:

```yaml
- hostname: FGT-80F
  host: 192.0.2.102
  vendor: fortinet
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${FGT_80F_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${FGT_80F_SNMP_PRIV}
```

SNMPv2c is also supported:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 2
  community: ${PA_440_SNMP_COMMUNITY}
```

## Palo Alto XML API Setup

API monitoring is optional and Palo Alto-only. Its dedicated dashboard is API-only, including interface throughput; the existing SNMP dashboards remain unchanged.

Create a dedicated PAN-OS administrator with a custom role that grants only XML API **Operational Requests** and **Show** access. Avoid using a full superuser account for ongoing collection.

### Choose Where to Store the API Key

The recommended option stores the API key in `.env` and leaves only a variable reference in `firewalls.yml`:

```dotenv
PALOALTO_API_KEY_PA_440=CHANGE_ME_PALO_ALTO_API_KEY
```

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${PA_440_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${PA_440_SNMP_PRIV}
  api_monitoring:
    enabled: true
    api_key: ${PALOALTO_API_KEY_PA_440}
    port: 443
    verify_tls: true
    interval: 20
    resource_interval: 60
    counter_interval: 60
    system_interval: 3600
```

Never put a real key in `firewalls_example.yml`, a commit, a ticket, or a shared log. The earlier `api_key_env` syntax remains supported for existing installations:

```yaml
  api_monitoring:
    enabled: true
    api_key_env: PALOALTO_API_KEY_PA_440
    verify_tls: true
```

Set exactly one of `api_key` or `api_key_env` for each enabled firewall.

By default, API polling uses the firewall-level `host`, which is also used for SNMP. If the same firewall is reached through different addresses for SNMP and HTTPS, keep the SNMP address at the top level and set the API address inside `api_monitoring`:

```yaml
- hostname: PA-440
  host: 192.0.2.101       # SNMP address
  vendor: paloalto
  snmp_version: 2
  community: ${PA_440_SNMP_COMMUNITY}
  api_monitoring:
    enabled: true
    host: 192.0.2.201     # PAN-OS XML API address
    api_key: ${PALOALTO_API_KEY_PA_440}
```

### Generate and Store a Key

The helper parses `firewalls.yml` and lists only the declared Palo Alto firewalls. Select one by number, enter the API username and password, and the helper uses the declared `host` automatically (or the existing `api_monitoring.host` override). By default it stores the generated secret in `.env`, writes an `${ENVIRONMENT_VARIABLE}` reference in the selected YAML entry, and sets both files to mode `0600`:

```bash
.venv/bin/python paloalto_api_key.py
```

Example interaction:

```text
Palo Alto firewalls declared in the inventory:
  1. PARIS-PA-01 (192.0.2.101) [API disabled]
  2. LYON-PA-01 (192.0.2.102) [API enabled]
Select a firewall [1-2]: 1
API username: fwmon-api
API password:
```

For scripts and unattended workflows, bypass the menu with `--hostname`; the helper resolves the declared API address automatically. `--host` remains available to supply or replace a distinct API address:

```bash
.venv/bin/python paloalto_api_key.py --hostname PA-440 --username fwmon-api
.venv/bin/python paloalto_api_key.py --hostname PA-440 --host api-pa.example.test --username fwmon-api
```

Direct YAML storage remains available for backward compatibility when explicitly requested:

```bash
.venv/bin/python paloalto_api_key.py \
  --hostname PA-440 \
  --username fwmon-api \
  --storage yaml
```

The password and generated key are never printed. Existing polling settings in `api_monitoring` are preserved when a key is rotated or its storage mode changes. Before its first rewrite, the helper preserves the original inventory as `firewalls.yml.bak`; later runs do not overwrite that initial backup.

`verify_tls: true` is the secure default. Install a trusted firewall certificate or the issuing internal CA on the Docker host/container. For a temporary lab with a self-signed certificate, pass `--insecure`; the helper then writes `verify_tls: false` explicitly.

### Docker and Non-Docker Variable Handling

With the normal Docker Compose workflow, no manual `export` or `docker -e` command is required. `generate.py` resolves `${VARIABLE}` references, direct `api_key` values, and the legacy `api_key_env` form. It writes only the required API and SNMP secrets to the mode-`0600` generated file `telegraf/paloalto-api.env`; `telegraf.conf` contains environment-variable references rather than clear-text credentials. Docker Compose injects that protected file into Telegraf. Other `.env` secrets, such as Grafana and InfluxDB administrator passwords, are not passed to the Telegraf container. Generated enriched inventory is mode `0600` and redacts all SNMP and API credentials.

For a one-shot diagnostic from the Linux host rather than from Docker, first generate the runtime files, then let the collector load the protected environment file itself:

```bash
python3 telegraf/paloalto_api_collector.py \
  --config telegraf/paloalto-api.json \
  --env-file telegraf/paloalto-api.env \
  --once
```

The supported full monitoring deployment remains Docker Compose; the host command is intended for connectivity and parser diagnostics.

### Many Palo Alto Firewalls

API monitoring is configured independently for every Palo Alto entry. Firewalls can be migrated gradually, and Fortinet entries are left unchanged:

```yaml
- hostname: PARIS-PA-01
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 2
  community: ${PARIS_PA_01_SNMP_COMMUNITY}
  api_monitoring:
    enabled: true
    api_key: ${PALOALTO_API_KEY_PARIS_PA_01}

- hostname: LYON-PA-01
  host: 192.0.2.102
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${LYON_PA_01_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${LYON_PA_01_SNMP_PRIV}
  api_monitoring:
    enabled: true
    api_key_env: PALOALTO_API_KEY_LYON_PA_01

- hostname: BORDEAUX-PA-01
  host: 192.0.2.103
  vendor: paloalto
  snmp_version: 2
  community: ${BORDEAUX_PA_01_SNMP_COMMUNITY}
  # No api_monitoring block: this firewall remains SNMP-only.
```

Use a unique `hostname` for every firewall and, when using `.env`, a clear unique variable name for every device. Run `paloalto_api_key.py` once per firewall that needs a generated key, or add existing keys manually. The collector serializes calls within one firewall and polls different firewalls in parallel, so adding a slow device does not block the others.

The collector polls API categories sequentially for each firewall and only parallelizes between firewalls. Session and hardware interface counters use `interval`, which cannot be configured below 10 seconds. Management-plane process metrics are aggregated by command name and limited to the 32 busiest processes per poll, avoiding PID-based cardinality.

Global counters use the PAN-OS server-side `severity drop` filter. All active drop counters, including their category, aspect, rate, and description, are retained up to `counter_limit` (default `256`, range `16`–`2048`). Priority resource, policy, DoS, allocation, and TCP counters are retained first if the limit is reached. Grafana exposes **Counter category** and **Counter aspect** selectors for interactive filtering. Cumulative values are stored and Grafana calculates rates, avoiding the shared sampling state created by PAN-OS `delta yes`.

The `Palo Alto API Performance Monitoring` dashboard works for both compact and multi-blade systems and reads only PAN-OS XML API measurements. Its main view mirrors the standard dashboard with platform, PAN-OS version, uptime, MP/DP CPU, RAM, sessions, CPS, session utilization, and global throughput. It adds a colored current-load strip (DP CPU average, hottest DP core, MP CPU/RAM, sessions, session table, CPS, and throughput), a per-port link-utilization table, worst-dataplane resource pressure, and the global drop rate by counter category. Additional details are grouped into collapsible sections for HA (local and peer role, configuration and session sync), interfaces, errors/discards, one repeated section per VSYS, zones and logical interfaces, per-dataplane details, session protocols, curated DoS/zone-protection drops, filtered global counters, MP load/storage, and environmental sensors.

The XML API reveals dataplane saturation that SNMP hides. SNMP and the API both report the average of all dataplane cores, including cores that are not used for packet processing and stay at 0%. On a PA-5500, for example, both report about 57% while every active core is above 90%. The API dashboards therefore also show the hottest core, the active-core average, and a per-core load map for every dataplane.

Per-VSYS sessions come from `show session meter`. Per-zone and per-VSYS throughput are derived from the logical interface (`ifnet`) counters that `show counter interface all` already returns. The collector tags them with the zone and VSYS learned from `show interface all`. The XML API has no per-VSYS or per-zone CPS equivalent to the SNMP `panVsysTotalCps` and zone CPS objects: the `ifnet` `tcp_conn`/`udp_conn` counters do not count created sessions and remain at zero on live firewalls, so the API dashboards do not show per-VSYS or per-zone CPS. Global CPS is still shown. These logical counters also provide subinterface, tunnel, and VLAN throughput and per-reason drops such as no route, no ARP, flow state, and spoofing. Global and per-port throughput still uses only the hardware `ibytes` / `obytes` counters. DoS and zone-protection counters that are not drops, such as SYN-cookie activity and block-table entries, are collected with an additional `aspect dos` global-counter filter and share the `counter_limit` bound. If a platform rejects an optional command such as `show session meter` or the environmental commands, the collector logs it once and stops polling that command until Telegraf restarts.

Data-plane CPU is tagged by dataplane and core. Grafana creates one collapsible row per dataplane, containing its individual core curves and API resource pressure (sessions, packet buffers, packet descriptors, and software tags when exposed). This supports compact systems and multi-DP platforms such as PA-5500/PA-7000/PA-7500; the same series also feed the dedicated high-end/chassis dashboard.

The interface section automatically creates a dedicated In/Out throughput panel for every active physical Ethernet port, matching the drill-down available in the SNMP dashboard. A separate API detail section retains packet-rate curves plus current link state, speed, duplex, mode, zone, VSYS, and forwarding instance. Throughput is calculated from deltas of the hardware `ibytes` / `obytes` counters returned by `show counter interface all`; it does not use SNMP or the less reliable session throughput summary. Global throughput is restricted to physical Ethernet ports so aggregate `internal`, `vlan`, `loopback`, and `tunnel` counters are not double-counted.

The separate `Palo Alto API Chassis Monitoring` dashboard targets high-end PA-5200, PA-5400, PA-5500, PA-7000, and PA-7500 platforms. It contains the complete main view and every section of the API performance dashboard, plus chassis-specific sections. This includes fixed multi-dataplane models such as PA-5580: the API dashboard keeps one curve per returned dataplane/core. On modular models, the dashboard also combines `show chassis inventory`, `show chassis status`, and `show chassis power` for installed-card details, live slot/card state, system role, configuration state, disabled slots, and power. The collector only issues these chassis-specific calls to PA-5450, PA-7050, PA-7080, and PA-7500 models, so a fixed PA-5580 gets its DP, interface, MP, sensor, HA, and counter panels without repeated unsupported-command errors; slot inventory/status/power panels are simply empty.

### API Coverage and Deliberate Limits

The API dashboards intentionally collect the high-value performance and health data that is unavailable, incomplete, or less actionable through the project SNMP views: session protocol counts and utilization, packet rate, MP load/swap/tasks/processes, per-core and per-dataplane CPU including the hottest core, link utilization, per-VSYS sessions, per-zone throughput, egress errors and link flaps, logical interface drop reasons, HA peer and sync state, dataplane session/buffer/descriptor pressure, filtered global drop counters with diagnostic metadata, interface zone/VSYS/forwarding context, HA state, storage, environmental sensors, and modular chassis inventory/status/power.

The XML API can expose much more, but “everything available” is not a safe monitoring target. Route/ARP/User-ID tables, full session lists, logs, ACC reports, configuration object counts, and running configuration are deliberately excluded: they can have high or unbounded cardinality, increase management-plane load, reveal sensitive traffic or configuration data, and may require broader API permissions. This project keeps the steady-state collector read-only, bounded, and focused on capacity/load. Add those datasets to a troubleshooting or capacity-planning tool rather than the 20-second performance loop.

## Upgrade an Existing Installation

Existing inventories remain compatible. If an entry has no `api_monitoring` block, API monitoring stays disabled and its SNMP behavior is unchanged. Configurations using the earlier `api_key_env` format also remain supported.

### 1. Back Up the Local Configuration

Run these commands from the existing project directory before updating it:

```bash
install -d -m 700 ../fw-monitoring-backup-YYYYMMDD
cp -a firewalls.yml .env ../fw-monitoring-backup-YYYYMMDD/
```

Replace `YYYYMMDD` with the upgrade date. Keeping the backup outside the repository prevents configuration copies containing secrets from appearing as untracked project files.

For an important production installation, stop the stack and also copy `influxdb-data/` and `grafana-data/` into that protected backup directory before restarting it. They contain the monitoring history and Grafana state and are not regenerated from the YAML inventory. Keep all backups private because configuration and data directories can contain credentials or operational information.

### 2. Update the Project Files

For a Git checkout:

```bash
git status --short
git pull --ff-only
```

Review any local tracked-file changes before pulling. The normal local configuration files, `.env` and `firewalls.yml`, are ignored by Git and must remain in place. Do not replace `firewalls.yml` with `firewalls_example.yml`.

For an archive-based installation, extract the new project release over a copy of the existing directory and restore the saved `.env` and `firewalls.yml` before running the generator. Preserve `influxdb-data/` and `grafana-data/` if the installation is moved to a new directory.

### 3. Regenerate and Restart the Stack

Always run the generator after an upgrade:

```bash
./generate.sh
```

Do not use only `docker compose up -d`. The generator validates the existing inventory, recreates the Telegraf and API runtime files, downloads any required MIBs, rebuilds the Telegraf image, and starts or refreshes the stack. Existing InfluxDB history and Grafana state remain in their persistent data directories.

### 4. Verify the Upgrade

```bash
docker compose ps
docker compose logs --tail=100 telegraf
```

Then open `http://<docker-host-ip>:3000`, open the relevant dashboard, and verify each configured hostname.

### 5. Enable API Monitoring Gradually

The upgrade does not automatically enable API monitoring. Migrate Palo Alto firewalls one at a time:

1. Add `api_monitoring` only to the Palo Alto firewalls you want to migrate.
2. Generate missing keys with `paloalto_api_key.py`, or paste existing keys into the local inventory.
3. Run `./generate.sh` again to recreate `telegraf/telegraf.conf`, `telegraf/paloalto-api.json`, and `telegraf/paloalto-api.env`.
4. Check `docker compose ps` and `docker compose logs --tail=100 telegraf`.
5. Open `Palo Alto API Performance Monitoring` in Grafana and select each migrated hostname.

### Rollback

To roll back only API monitoring without affecting SNMP, set `api_monitoring.enabled: false` or remove the block, then rerun `./generate.sh`.

To roll back the local configuration, copy `.env` and `firewalls.yml` back from the protected backup directory, then rerun the generator. If the project code itself must also be rolled back, restore the previous release or Git tag first. Do not delete `influxdb-data/` or `grafana-data/` during a routine rollback.

## Palo Alto SNMP Setup

Use SNMPv3 when possible. SNMPv2c works, but the community is sent in clear text.

The examples below create a read-only SNMP view for the standard monitoring tree and a user named `fwmon`.

Replace:

- `CHANGE_ME_AUTH_PASSWORD`
- `CHANGE_ME_PRIV_PASSWORD`
- `CHANGE_ME_COMMUNITY`

### Palo Alto SNMPv3 CLI

```text
configure
set deviceconfig system snmp-setting access-setting version v3 views fw-monitoring view all oid 1.3.6.1
set deviceconfig system snmp-setting access-setting version v3 views fw-monitoring view all option include
set deviceconfig system snmp-setting access-setting version v3 views fw-monitoring view all mask 0xf0
set deviceconfig system snmp-setting access-setting version v3 users fwmon view fw-monitoring
set deviceconfig system snmp-setting access-setting version v3 users fwmon authproto SHA-256
set deviceconfig system snmp-setting access-setting version v3 users fwmon authpwd CHANGE_ME_AUTH_PASSWORD
set deviceconfig system snmp-setting access-setting version v3 users fwmon privproto AES-256
set deviceconfig system snmp-setting access-setting version v3 users fwmon privpwd CHANGE_ME_PRIV_PASSWORD
commit
```

Matching `firewalls.yml`:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${PA_440_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${PA_440_SNMP_PRIV}
```

### Palo Alto SNMPv2c CLI

```text
configure
set deviceconfig system snmp-setting access-setting version v2c snmp-community-string CHANGE_ME_COMMUNITY
commit
```

Matching `firewalls.yml`:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 2
  community: ${PA_440_SNMP_COMMUNITY}
```

Notes:

- Configure each HA peer. PAN-OS does not automatically synchronize SNMP response settings between HA peers.
- If you poll through a dataplane interface instead of the management interface, make sure the required management/interface profile and security policy allow SNMP from the Docker host.

## Fortinet SNMP Setup

Use SNMPv3 when possible. Make sure the interface used by the Docker host allows SNMP administrative access.

Replace:

- `mgmt` with the FortiGate interface name reachable from the Docker host
- `192.0.2.50` with the Docker host IP
- `CHANGE_ME_AUTH_PASSWORD`
- `CHANGE_ME_PRIV_PASSWORD`
- `CHANGE_ME_COMMUNITY`

### Fortinet SNMPv3 CLI

```text
config system interface
    edit "mgmt"
        set allowaccess ping https ssh snmp
    next
end

config system snmp sysinfo
    set status enable
end

config system snmp user
    edit "fwmon"
        set status enable
        set queries enable
        set query-port 161
        set notify-hosts 192.0.2.50
        set security-level auth-priv
        set auth-proto sha256
        set auth-pwd CHANGE_ME_AUTH_PASSWORD
        set priv-proto aes256
        set priv-pwd CHANGE_ME_PRIV_PASSWORD
    next
end
```

Matching `firewalls.yml`:

```yaml
- hostname: FGT-80F
  host: 192.0.2.102
  vendor: fortinet
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: ${FGT_80F_SNMP_AUTH}
  priv_protocol: aes256
  priv_password: ${FGT_80F_SNMP_PRIV}
```

### Fortinet SNMPv2c CLI

```text
config system interface
    edit "mgmt"
        set allowaccess ping https ssh snmp
    next
end

config system snmp sysinfo
    set status enable
end

config system snmp community
    edit 1
        set name "CHANGE_ME_COMMUNITY"
        set status enable
        set query-v2c-status enable
        set query-v2c-port 161
        config hosts
            edit 1
                set ip 192.0.2.50 255.255.255.255
                set host-type query
            next
        end
    next
end
```

Matching `firewalls.yml`:

```yaml
- hostname: FGT-80F
  host: 192.0.2.102
  vendor: fortinet
  snmp_version: 2
  community: ${FGT_80F_SNMP_COMMUNITY}
```

## Discovery

By default, the generator performs a best-effort SNMP discovery poll before rendering Telegraf.

For Palo Alto, discovery records:

- `sysDescr`
- `sysObjectID`
- PAN-OS version
- serial number
- model when it can be parsed
- VSYS table presence
- hardware/chassis profile when the model exposes it

For Fortinet, discovery records:

- `sysDescr`
- `sysObjectID`
- FortiOS version
- serial number
- model when it can be parsed
- VDOM table presence

If a device is offline or credentials are wrong, generation continues with values declared in `firewalls.yml`.

Disable discovery when needed:

```bash
SNMP_DISCOVERY=false ./generate.sh
```

If Palo Alto discovery cannot determine a PAN-OS version, the generator downloads the default Palo Alto MIB version `11-2`. Override it when needed:

```bash
PALO_MIB_VERSION=10-2 ./generate.sh
```

## Validate

Every pull request and push to `main` runs GitHub Actions on Python 3.11 and 3.12. CI executes the unit tests with branch coverage, enforces a 75% project coverage floor, checks that generated dashboards are current, compiles all Python sources, validates `generate.sh` and Docker Compose, audits runtime and CI dependencies, scans tracked files for secrets and configuration problems, builds the custom Telegraf image, smoke-tests its unprivileged user and chassis MIB translations, and scans it for high or critical vulnerabilities. The image job reports every finding and blocks on vulnerabilities with an available fix. Any temporary exception must be scoped, justified, and dated in `.trivyignore.yaml` and `SECURITY.md`. The checks also run every Monday and can be started manually.

Run the same core checks locally:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
COVERAGE_FILE=/tmp/fw-monitoring.coverage .venv/bin/python -m coverage run -m unittest discover -s tests
COVERAGE_FILE=/tmp/fw-monitoring.coverage .venv/bin/python -m coverage report
python3 -m py_compile generate.py paloalto_api_key.py telegraf/paloalto_api_collector.py scripts/build_paloalto_api_dashboard.py
bash -n generate.sh
docker compose config --quiet
.venv/bin/pip-audit -r requirements-dev.txt
```

The coverage threshold prevents large untested regressions, but the percentage is not treated as proof of correctness. Tests prioritize inventory and secret validation, API parsing, dashboard generation, SNMP discovery behavior, and stack orchestration.

The Linux distribution reported by the container scanner is independent of the Docker host. The official Telegraf image used by this project is Debian-based, so an Ubuntu host correctly produces Debian findings for that image.

After startup:

```bash
docker compose ps
docker compose logs -f telegraf
```

The generator writes a timestamped local log file under `logs/`.

Telegraf writes poll and plugin errors to `logs/telegraf/telegraf.log`. That file rotates daily, keeps 7 archives, and also rotates early if it reaches 25 MB. Docker container logs are also size-limited in `docker-compose.yaml` so the Docker daemon log files do not grow without bound.

Useful Telegraf troubleshooting commands:

```bash
tail -f logs/telegraf/telegraf.log
docker compose logs --tail=100 telegraf
```

Useful local SNMP checks from the Telegraf image:

```bash
docker compose run --rm telegraf snmpget -v2c -c CHANGE_ME_COMMUNITY 192.0.2.101 1.3.6.1.2.1.1.1.0
```

For SNMPv3:

```bash
docker compose run --rm telegraf snmpget -v3 -l authPriv -u fwmon -a SHA-256 -A CHANGE_ME_AUTH_PASSWORD -x AES-256 -X CHANGE_ME_PRIV_PASSWORD 192.0.2.101 1.3.6.1.2.1.1.1.0
```

## Generated Files

These files/directories are generated locally and ignored by Git:

- `.env`
- `.venv/`
- `firewalls.yml`
- `firewalls_*.yml`
- `.firewalls.generated.yml`
- `logs/`
- `telegraf/telegraf.conf`
- `telegraf/paloalto-api.json`
- `telegraf/paloalto-api.env`
- `telegraf/mibs/paloalto/`
- `grafana-data/`
- `influxdb-data/`

## References

- Palo Alto Networks SNMP monitoring documentation: https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/monitoring/snmp-monitoring-and-traps/monitor-statistics-using-snmp
- Palo Alto Networks XML API request types: https://docs.paloaltonetworks.com/ngfw/api/pan-os-xml-api-request-types-and-actions
- Palo Alto Networks operational commands through the XML API: https://docs.paloaltonetworks.com/ngfw/api/pan-os-xml-api-request-types-and-actions/run-operational-mode-commands-api
- Palo Alto Networks operational CLI command hierarchy: https://docs.paloaltonetworks.com/ngfw/pan-os-cli-quick-start/cli-command-hierarchy
- Palo Alto Networks XML API request structure and authentication: https://docs.paloaltonetworks.com/ngfw/api/getting-started/structure-of-a-pan-os-xml-api-request
- Palo Alto Networks global counter troubleshooting and filters: https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000ClXOCA0
- Palo Alto Networks guidance for high dataplane CPU: https://live.paloaltonetworks.com/t5/support-faq/support-faq-how-to-handle-high-data-plane-cpu-issues/ta-p/592941
- Palo Alto Networks PA-7000 slot states: https://docs.paloaltonetworks.com/hardware/pa-7000-hardware-reference/service-the-pa-7000-series-hardware/replace-a-pa-7000-series-front-slot-card/replace-a-pa-7000-series-network-processing-card-npc/pa-7000-series-front-slot-states
- Palo Alto Networks PA-7000 power statistics: https://docs.paloaltonetworks.com/hardware/pa-7000-hardware-reference/PA-7000-series-firewall-installation/connect-power-to-a-pa-7000-series-firewall/view-pa-7000-series-firewall-power-statistics
- Palo Alto Networks CLI command hierarchy for SNMPv3: https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-cli-quick-start/cli-command-hierarchy/pan-os-11-1-configure-cli-command-hierarchy
- Fortinet `config system snmp user`: https://docs.fortinet.com/document/fortigate/7.6.3/cli-reference/292257317/config-system-snmp-user
- Fortinet `config system snmp community`: https://docs.fortinet.com/document/fortigate/7.0.1/cli-reference/54620/config-system-snmp-community

## Scope

This project is a quick firewall monitoring starter. It is not intended to replace a full NMS, SIEM, or vendor management platform.

## License

MIT License. See `LICENSE`.
