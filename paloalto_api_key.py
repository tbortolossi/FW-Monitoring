#!/usr/bin/env python3
"""Generate a PAN-OS API key and configure API monitoring safely."""

from __future__ import annotations

import argparse
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
    matches = [item for item in data if isinstance(item, dict) and (item.get("host") == host or (hostname and item.get("hostname") == hostname))]
    if len(matches) != 1:
        raise ValueError("exactly one Palo Alto inventory entry must match the host or hostname")
    firewall = matches[0]
    if str(firewall.get("vendor", "paloalto")).lower() not in {"paloalto", "palo", "panos", "palo_alto"}:
        raise ValueError("the matching inventory entry is not a Palo Alto firewall")
    return firewall


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
    api_config = {
        "enabled": True,
        "port": port,
        "verify_tls": verify_tls,
    }
    if str(firewall.get("host")) != host:
        api_config["host"] = host
    if api_key:
        api_config["api_key"] = api_key
    else:
        api_config["api_key_env"] = api_key_env
    firewall["api_monitoring"] = api_config
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return str(backup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="PAN-OS API IP or hostname; may differ from the inventory SNMP host")
    parser.add_argument("--hostname", help="inventory hostname; defaults to the host value")
    parser.add_argument("--username")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--inventory", type=Path, default=Path("firewalls.yml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--storage",
        choices=("yaml", "env"),
        default="yaml",
        help="store the API key directly in firewalls.yml (default) or reference it from .env",
    )
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")
    args = parser.parse_args(argv)

    host = args.host or input("Firewall IP or hostname: ").strip()
    username = args.username or input("API username: ").strip()
    password = getpass.getpass("API password: ")
    if not host or not username or not password:
        parser.error("host, username and password are required")
    try:
        validate_target(host, args.port)
        inventory = yaml.safe_load(args.inventory.read_text(encoding="utf-8")) or []
        find_inventory_entry(inventory, host, args.hostname)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    env_name = environment_name(args.hostname or host)
    try:
        key = generate_key(host, username, password, args.port, not args.insecure, args.timeout)
    except Exception as exc:
        raise SystemExit(f"ERROR: could not generate API key: {exc}") from exc
    if args.storage == "env":
        update_env(args.env_file, env_name, key)
        backup = update_inventory(
            args.inventory,
            host,
            args.hostname,
            not args.insecure,
            args.port,
            api_key_env=env_name,
        )
        print(f"API key stored in {args.env_file} as {env_name} (mode 0600).")
    else:
        backup = update_inventory(
            args.inventory,
            host,
            args.hostname,
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
