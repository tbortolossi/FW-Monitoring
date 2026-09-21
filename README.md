# Firewall Monitoring Starter

Palo Alto and Fortinet firewall monitoring stack with Docker Compose, Telegraf, InfluxDB, and Grafana. SNMP is the baseline; Palo Alto devices can optionally add read-only PAN-OS XML API performance polling.

![FW-Monitoring dashboard](docs/assets/FW-Monitoring.png)

Docker Compose stack for quick Palo Alto and Fortinet firewall monitoring with Telegraf, InfluxDB, and Grafana.

The goal is simple operational visibility: CPU, memory, sessions, CPS, disk where useful, interface status, errors/discards, and throughput. It is useful when you need a quick factual view of firewall load without deploying a full NMS.

For both vendors, throughput is calculated from IF-MIB interface counters (`ifHCInOctets` and `ifHCOutOctets`). This is intentional: dataplane, NPU, or feature counters can miss traffic that is offloaded or handled outside that counter path.

## What You Get

- InfluxDB 2.x for time series storage
- Telegraf SNMP polling generated from `firewalls.yml`
- Optional Palo Alto XML API polling for sessions, management-plane resources, per-core/dataplane CPU, and selected drop counters
- Grafana with provisioned InfluxDB datasource
- Four monitoring dashboards:
  - `Palo Alto Firewall Monitoring`
  - `Palo Alto Chassis Monitoring`
  - `Fortinet Firewall Monitoring`
  - `Palo Alto API Performance Monitoring`
- Best-effort SNMP discovery before Telegraf config generation

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

6. Open Grafana:

```text
http://<docker-host>:3000
```

Use the Grafana admin username/password from `.env`.

## Python Generator

The project generator is Python-based:

- `generate.py` contains the generation logic and is the main entry point.
- `generate.sh` is only a convenience wrapper for creating `.venv`, installing dependencies, and launching `generate.py`.
- `requirements.txt` contains the Python dependencies: `PyYAML` and `Jinja2`.

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

Minimal Palo Alto SNMPv3:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: CHANGE_ME_AUTH_PASSWORD
  priv_protocol: aes256
  priv_password: CHANGE_ME_PRIV_PASSWORD
  api_monitoring:
    enabled: true
    api_key: CHANGE_ME_PALO_ALTO_API_KEY
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
  auth_password: CHANGE_ME_AUTH_PASSWORD
  priv_protocol: aes256
  priv_password: CHANGE_ME_PRIV_PASSWORD
```

SNMPv2c is also supported:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 2
  community: CHANGE_ME_COMMUNITY
```

## Palo Alto XML API Setup

API monitoring is optional and Palo Alto-only. It complements SNMP; it does not replace the SNMP interface counters used for accurate throughput.

Create a dedicated PAN-OS administrator with a custom role that grants only XML API **Operational Requests** and **Show** access. Avoid using a full superuser account for ongoing collection.

### Choose Where to Store the API Key

The simplest option matches the existing SNMPv2c/SNMPv3 inventory: store the API key directly in the local `firewalls.yml`. That file is ignored by Git and already contains firewall credentials:

```yaml
- hostname: PA-440
  host: 192.0.2.101
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: CHANGE_ME_AUTH_PASSWORD
  priv_protocol: aes256
  priv_password: CHANGE_ME_PRIV_PASSWORD
  api_monitoring:
    enabled: true
    api_key: CHANGE_ME_PALO_ALTO_API_KEY
    port: 443
    verify_tls: true
    interval: 20
    resource_interval: 60
    counter_interval: 60
    system_interval: 3600
```

Never put a real key in `firewalls_example.yml`, a commit, a ticket, or a shared log.

If local policy requires secrets to be separate from inventory, put the key in `.env`:

```dotenv
PALOALTO_API_KEY_PA_440=CHANGE_ME_PALO_ALTO_API_KEY
```

Then reference its variable name in `firewalls.yml` instead of using `api_key`:

```yaml
  api_monitoring:
    enabled: true
    api_key_env: PALOALTO_API_KEY_PA_440
    verify_tls: true
```

Set exactly one of `api_key` or `api_key_env` for each enabled firewall.

### Generate and Store a Key

The helper obtains an API key using an interactive password prompt and updates the matching inventory entry. By default it stores the key directly in the ignored local `firewalls.yml`, matching the SNMP credential workflow:

```bash
.venv/bin/python paloalto_api_key.py --host 192.0.2.101 --hostname PA-440 --username fwmon-api
```

Use environment-variable storage instead when required:

```bash
.venv/bin/python paloalto_api_key.py \
  --host 192.0.2.101 \
  --hostname PA-440 \
  --username fwmon-api \
  --storage env
```

The password and generated key are never printed. The helper sets the updated inventory and its backup to mode `0600`. Before its first rewrite, it preserves the original inventory as `firewalls.yml.bak`; later runs do not overwrite that initial backup.

`verify_tls: true` is the secure default. Install a trusted firewall certificate or the issuing internal CA on the Docker host/container. For a temporary lab with a self-signed certificate, pass `--insecure`; the helper then writes `verify_tls: false` explicitly.

### Docker and Non-Docker Variable Handling

With the normal Docker Compose workflow, no manual `export` or `docker -e` command is required. `generate.py` resolves both direct `api_key` values and `.env` references, writes only the required keys to the mode-`0600` generated file `telegraf/paloalto-api.env`, and Docker Compose injects that file into Telegraf. Other `.env` secrets, such as Grafana and InfluxDB administrator passwords, are not passed to the Telegraf container.

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
  community: CHANGE_ME_PARIS_SNMP
  api_monitoring:
    enabled: true
    api_key: CHANGE_ME_PARIS_API_KEY

- hostname: LYON-PA-01
  host: 192.0.2.102
  vendor: paloalto
  snmp_version: 3
  username: fwmon
  auth_protocol: sha256
  auth_password: CHANGE_ME_LYON_AUTH
  priv_protocol: aes256
  priv_password: CHANGE_ME_LYON_PRIV
  api_monitoring:
    enabled: true
    api_key_env: PALOALTO_API_KEY_LYON_PA_01

- hostname: BORDEAUX-PA-01
  host: 192.0.2.103
  vendor: paloalto
  snmp_version: 2
  community: CHANGE_ME_BORDEAUX_SNMP
  # No api_monitoring block: this firewall remains SNMP-only.
```

Use a unique `hostname` for every firewall and, when using `.env`, a clear unique variable name for every device. Run `paloalto_api_key.py` once per firewall that needs a generated key, or add existing keys manually. The collector serializes calls within one firewall and polls different firewalls in parallel, so adding a slow device does not block the others.

The collector polls API categories sequentially for each firewall and only parallelizes between firewalls. Session polling cannot be configured below 10 seconds. Global counters are restricted to a small allowlist of high-value drop/failure counters to bound InfluxDB cardinality and management-plane load.

The `Palo Alto API Performance Monitoring` dashboard works for both compact and multi-blade systems. Data-plane CPU is tagged by dataplane and core and includes a per-dataplane average, so PA-7000/PA-7500 results appear as additional series without a separate chassis dashboard. A complementary CPU panel shows the global MP/DP values and every processor exposed by `pan_hr_processors`. Interface throughput on this dashboard still comes from SNMP `ifHCInOctets` / `ifHCOutOctets`, because API throughput summaries can omit offloaded traffic.

## Upgrade an Existing Installation

Existing inventories remain compatible. If an entry has no `api_monitoring` block, API monitoring stays disabled and its SNMP behavior is unchanged. Configurations using the earlier `api_key_env` format also remain supported.

Before updating the project, preserve the ignored local configuration:

```bash
cp firewalls.yml firewalls.yml.pre-api-upgrade
cp .env .env.pre-api-upgrade
```

Then update the project files using your normal Git or archive workflow. Do not replace the local `firewalls.yml` with `firewalls_example.yml`.

After the update:

1. Add `api_monitoring` only to the Palo Alto firewalls you want to migrate.
2. Generate missing keys with `paloalto_api_key.py`, or paste existing keys into the local inventory.
3. Run `./generate.sh` rather than only `docker compose up -d`. The generator must recreate `telegraf/telegraf.conf`, `telegraf/paloalto-api.json`, and `telegraf/paloalto-api.env`, and rebuild the Telegraf image with Python support.
4. Check `docker compose ps` and `docker compose logs --tail=100 telegraf`.
5. Open `Palo Alto API Performance Monitoring` in Grafana and select each migrated hostname.

To roll back API monitoring without affecting SNMP, set `api_monitoring.enabled: false` or remove the block, then rerun `./generate.sh`. Restore `firewalls.yml.pre-api-upgrade` only if the whole inventory migration must be undone.

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
  auth_password: CHANGE_ME_AUTH_PASSWORD
  priv_protocol: aes256
  priv_password: CHANGE_ME_PRIV_PASSWORD
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
  community: CHANGE_ME_COMMUNITY
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
  auth_password: CHANGE_ME_AUTH_PASSWORD
  priv_protocol: aes256
  priv_password: CHANGE_ME_PRIV_PASSWORD
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
  community: CHANGE_ME_COMMUNITY
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
- Palo Alto Networks XML API request structure and authentication: https://docs.paloaltonetworks.com/ngfw/api/getting-started/structure-of-a-pan-os-xml-api-request
- Palo Alto Networks CLI command hierarchy for SNMPv3: https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-cli-quick-start/cli-command-hierarchy/pan-os-11-1-configure-cli-command-hierarchy
- Fortinet `config system snmp user`: https://docs.fortinet.com/document/fortigate/7.6.3/cli-reference/292257317/config-system-snmp-user
- Fortinet `config system snmp community`: https://docs.fortinet.com/document/fortigate/7.0.1/cli-reference/54620/config-system-snmp-community

## Scope

This project is a quick firewall monitoring starter. It is not intended to replace a full NMS, SIEM, or vendor management platform.

## License

MIT License. See `LICENSE`.
