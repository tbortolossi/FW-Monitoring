# Security Policy

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability or exposed credential. Use GitHub's private vulnerability reporting feature for this repository when available, or contact the repository owner privately.

Never include real firewall addresses, API keys, SNMP credentials, InfluxDB tokens, Grafana passwords, configuration archives, or diagnostic output containing those values in a report.

## Supported Version

Security fixes are applied to the latest release. Operators should upgrade by following the documented backup and regeneration procedure in `README.md` rather than copying generated files between versions.

## Deployment Exposure

- InfluxDB is published on `127.0.0.1:8086` only. Grafana and Telegraf reach it over the internal Compose network, so the InfluxDB API and the admin token it accepts are not reachable from other hosts. Prefer an SSH tunnel for remote queries. If the port must be exposed, bind it to a specific address and restrict it with a host firewall or VPN.
- Grafana is pinned to 13.2.2; 11.1.4 is out of support and affected by later CVEs, including CVE-2026-27876. Self sign-up, usage reporting, and update checks are disabled. Keep port 3000 on a trusted administration network or behind a TLS reverse proxy, and never expose it directly to the internet.
- The Telegraf container runs as the unprivileged `telegraf` user (UID `999`) and needs only outbound SNMP and HTTPS to the firewalls.

## Secrets Handling

- Operator secrets live in `.env` (mode `0600`, enforced by the generator) and are referenced from `firewalls.yml` as `${VARIABLE}`. Neither file is committed.
- Secrets are never passed on a command line. SNMP discovery pipes the credentials to its throwaway container as a Net-SNMP `snmp.conf` on stdin, so they do not appear in `ps`, `docker inspect`, or audit logs. PAN-OS API keys are sent in the `X-PAN-KEY` header, never in a URL.
- Telegraf receives only the SNMP secrets and API keys it needs, through the generated `telegraf/paloalto-api.env` (mode `0600`, Compose `env_file`). Values are double-quoted, with backslash, double quote, and dollar sign escaped by a backslash, so no value can be altered or expanded by Docker Compose; values with line breaks or NUL bytes are rejected, and the error names the variable, never the value.
- `telegraf/telegraf.conf` contains only variable references, and `.firewalls.generated.yml` redacts every credential.
- Generator output, collector logs, and the API-key helper never print secret values.

## Automated Checks

CI performs the following checks:

- unit tests with branch coverage on supported Python versions;
- Python runtime and CI-tool dependency auditing;
- repository secret and configuration scanning;
- reproducibility checks for generated Grafana dashboards;
- Docker Compose validation;
- custom Telegraf image build and vulnerability scanning.

The image scan always reports every HIGH and CRITICAL finding. It blocks releases for findings that have an available fix, except for explicit, scoped, expiring exceptions in `.trivyignore.yaml`. Findings without an upstream fix remain visible in the CI report and are reevaluated by the weekly scheduled run and Dependabot updates.

## Temporary Risk Acceptance

`CVE-2026-84445` in the gRPC-Go library embedded in Telegraf 1.40.1 is temporarily accepted until 2026-10-31 because:

- Telegraf 1.40.1 is the latest stable upstream release at the time of review;
- this stack enables SNMP and `execd` inputs and does not configure a gRPC listener, so the vulnerable request path is not reachable through the shipped configuration;
- the exception is restricted to `/usr/bin/telegraf` and expires automatically.

The exception must be removed immediately when a stable upstream Telegraf image includes the fixed dependency. Expiry must not be extended without a new review.

The runtime image uses the official Alpine variant to minimize the installed operating-system package set. Debian is used only as a disposable build stage for downloading standard MIB text files; no Debian executable, library, or package database is copied into the runtime image.
