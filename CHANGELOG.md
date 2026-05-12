# Changelog

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
