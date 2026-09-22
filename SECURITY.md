# Security Policy

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability or exposed credential. Use GitHub's private vulnerability reporting feature for this repository when available, or contact the repository owner privately.

Never include real firewall addresses, API keys, SNMP credentials, InfluxDB tokens, Grafana passwords, configuration archives, or diagnostic output containing those values in a report.

## Supported Version

Security fixes are applied to the latest release. Operators should upgrade by following the documented backup and regeneration procedure in `README.md` rather than copying generated files between versions.

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
