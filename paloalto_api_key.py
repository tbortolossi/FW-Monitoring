#!/usr/bin/env python3
"""Generate a PAN-OS API key and configure API monitoring safely."""

from __future__ import annotations

import argparse
import copy
import getpass
import re
import shutil
import ssl
import stat
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


PALO_ALTO_VENDORS = {"paloalto", "palo", "panos", "palo_alto"}


def generate_key(host: str, username: str, password: str, port: int, verify_tls: bool, timeout: int) -> str:
    request = urllib.request.Request(
        f"https://{host}:{port}/api/",
        data=urllib.parse.urlencode({"type": "keygen", "user": username, "password": password}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: SLF001
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        root = ET.fromstring(response.read())
    key = root.findtext("./result/key")
    if root.attrib.get("status") != "success" or not key:
        message = " ".join(text.strip() for text in root.itertext() if text.strip())
        raise RuntimeError(message or "PAN-OS did not return an API key")
    return key.strip()


def environment_name(hostname: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", hostname).strip("_").upper()
    return f"PALOALTO_API_KEY_{suffix}"


def validate_target(host: str, port: int) -> None:
    if not host or re.search(r"[\s/?#]", host):
        raise ValueError("host must be a bare IP address or DNS name")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")


def find_inventory_entry(data, host: str, hostname: str | None):
    if not isinstance(data, list):
        raise ValueError("firewalls.yml must contain a list")
    if hostname:
        matches = [
            item for item in data
            if isinstance(item, dict) and item.get("hostname") == hostname
        ]
    else:
        matches = [
            item for item in data
            if isinstance(item, dict) and item.get("host") == host
        ]
    if len(matches) != 1:
        raise ValueError("exactly one Palo Alto inventory entry must match the host or hostname")
    firewall = matches[0]
    if str(firewall.get("vendor", "paloalto")).lower() not in PALO_ALTO_VENDORS:
        raise ValueError("the matching inventory entry is not a Palo Alto firewall")
    return firewall


def paloalto_inventory_entries(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("firewalls.yml must contain a list")
    entries = [
        item for item in data
        if isinstance(item, dict)
        and str(item.get("vendor", "paloalto")).lower() in PALO_ALTO_VENDORS
    ]
    if not entries:
        raise ValueError("firewalls.yml does not contain a Palo Alto firewall")
    for firewall in entries:
        if not firewall.get("hostname") or not firewall.get("host"):
            raise ValueError("each Palo Alto firewall must define hostname and host")
    return entries


def effective_api_host(firewall: dict) -> str:
    api_config = firewall.get("api_monitoring")
    if isinstance(api_config, dict) and api_config.get("host"):
        return str(api_config["host"])
    return str(firewall["host"])


def select_inventory_entry(data, *, input_fn=None, output_fn=None) -> dict:
    input_fn = input_fn or input
    output_fn = output_fn or print
    entries = paloalto_inventory_entries(data)
    output_fn("Palo Alto firewalls declared in the inventory:")
    for index, firewall in enumerate(entries, start=1):
        api_config = firewall.get("api_monitoring")
        enabled = isinstance(api_config, dict) and api_config.get("enabled") is True
        api_host = effective_api_host(firewall)
        suffix = "API enabled" if enabled else "API disabled"
        output_fn(f"  {index}. {firewall['hostname']} ({api_host}) [{suffix}]")
    while True:
        choice = input_fn(f"Select a firewall [1-{len(entries)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            return entries[int(choice) - 1]
        output_fn("Invalid selection. Enter one of the displayed numbers.")


def resolve_inventory_target(
    data,
    host: str | None,
    hostname: str | None,
    *,
    input_fn=None,
    output_fn=None,
) -> tuple[dict, str, str]:
    if host:
        firewall = find_inventory_entry(data, host, hostname)
    elif hostname:
        firewall = find_inventory_entry(data, "", hostname)
    else:
        firewall = select_inventory_entry(data, input_fn=input_fn, output_fn=output_fn)
    selected_hostname = str(firewall["hostname"])
    selected_host = host or effective_api_host(firewall)
    return firewall, selected_host, selected_hostname


def update_env(path: Path, name: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{name}={value}"
    pattern = re.compile(rf"^{re.escape(name)}=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = replacement
            break
    else:
        if lines and lines[-1]:
            lines.append("")
        lines.extend(["# Palo Alto XML API monitoring key.", replacement])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def update_inventory(
    path: Path,
    host: str,
    hostname: str | None,
    verify_tls: bool,
    port: int,
    *,
    api_key: str | None = None,
    api_key_env: str | None = None,
) -> str:
    if bool(api_key) == bool(api_key_env):
        raise ValueError("set exactly one of api_key or api_key_env")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    firewall = find_inventory_entry(data, host, hostname)
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        backup.chmod(stat.S_IRUSR | stat.S_IWUSR)
    existing = firewall.get("api_monitoring")
    api_config = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    api_config.update(enabled=True, port=port, verify_tls=verify_tls)
    api_config.pop("api_key", None)
    api_config.pop("api_key_env", None)
    if str(firewall.get("host")) != host:
        api_config["host"] = host
    else:
        api_config.pop("host", None)
    if api_key:
        api_config["api_key"] = api_key
    else:
        api_config["api_key"] = f"${{{api_key_env}}}"
    firewall["api_monitoring"] = api_config
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return str(backup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        help="PAN-OS API IP or DNS name; overrides the address declared for the selected firewall",
    )
    parser.add_argument(
        "--hostname",
        help="select an inventory firewall non-interactively instead of showing the menu",
    )
    parser.add_argument("--username", help="PAN-OS API username; prompted when omitted")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--inventory", type=Path, default=Path("firewalls.yml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--storage",
        choices=("yaml", "env"),
        default="env",
        help="store the API key in .env and reference it from YAML (default), or store it directly in YAML",
    )
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")
    args = parser.parse_args(argv)

    try:
        inventory = yaml.safe_load(args.inventory.read_text(encoding="utf-8")) or []
        _, host, hostname = resolve_inventory_target(
            inventory,
            args.host,
            args.hostname,
        )
        validate_target(host, args.port)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    username = args.username or input("API username: ").strip()
    password = getpass.getpass("API password: ")
    if not username or not password:
        parser.error("username and password are required")
    env_name = environment_name(hostname)
    try:
        key = generate_key(host, username, password, args.port, not args.insecure, args.timeout)
    except Exception as exc:
        raise SystemExit(f"ERROR: could not generate API key: {exc}") from exc
    if args.storage == "env":
        update_env(args.env_file, env_name, key)
        backup = update_inventory(
            args.inventory,
            host,
            hostname,
            not args.insecure,
            args.port,
            api_key_env=env_name,
        )
        print(f"API key stored in {args.env_file} as {env_name} (mode 0600).")
    else:
        backup = update_inventory(
            args.inventory,
            host,
            hostname,
            not args.insecure,
            args.port,
            api_key=key,
        )
        print(f"API key stored in the ignored local inventory {args.inventory}.")
    print(f"API monitoring enabled in {args.inventory}; original inventory backup: {backup}.")
    print("The API key and password were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
