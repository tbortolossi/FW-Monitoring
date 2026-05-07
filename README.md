# Firewall Monitoring Starter

SNMP-only Palo Alto and Fortinet firewall monitoring stack with Docker Compose, Telegraf, InfluxDB, and Grafana.

Docker Compose stack for quick Palo Alto and Fortinet firewall monitoring with Telegraf, InfluxDB, and Grafana.

The goal is simple operational visibility: CPU, memory, sessions, CPS, disk where useful, interface status, errors/discards, and throughput. It is useful when you need a quick factual view of firewall load without deploying a full NMS.

For both vendors, throughput is calculated from IF-MIB interface counters (`ifHCInOctets` and `ifHCOutOctets`). This is intentional: dataplane, NPU, or feature counters can miss traffic that is offloaded or handled outside that counter path.

## What You Get

- InfluxDB 2.x for time series storage
- Telegraf SNMP polling generated from `firewalls.yml`
- Grafana with provisioned InfluxDB datasource
- Three monitoring dashboards:
  - `Palo Alto Firewall Monitoring`
  - `Palo Alto Chassis Monitoring`
  - `Fortinet Firewall Monitoring`
- Best-effort SNMP discovery before Telegraf config generation

## Requirements

- Linux host with Docker and Docker Compose v2
- Python 3 with `venv` and `pip`
- UDP/161 reachable from the Docker host to each firewall
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

3. Configure SNMP on the firewalls. Examples are below.

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
- `telegraf/mibs/paloalto/`
- `grafana-data/`
- `influxdb-data/`

## References

- Palo Alto Networks SNMP monitoring documentation: https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/monitoring/snmp-monitoring-and-traps/monitor-statistics-using-snmp
- Palo Alto Networks CLI command hierarchy for SNMPv3: https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-cli-quick-start/cli-command-hierarchy/pan-os-11-1-configure-cli-command-hierarchy
- Fortinet `config system snmp user`: https://docs.fortinet.com/document/fortigate/7.6.3/cli-reference/292257317/config-system-snmp-user
- Fortinet `config system snmp community`: https://docs.fortinet.com/document/fortigate/7.0.1/cli-reference/54620/config-system-snmp-community

## Scope

This project is a quick firewall monitoring starter. It is not intended to replace a full NMS, SIEM, or vendor management platform.

## License

MIT License. See `LICENSE`.
