#!/usr/bin/env python3
"""Collect basic Palo Alto performance metrics through the PAN-OS XML API.

The process is designed for Telegraf's ``inputs.execd`` plugin.  It keeps API
calls for a given firewall sequentially while polling different firewalls in
parallel, then emits InfluxDB line protocol on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


SYSTEM_INFO_COMMAND = "<show><system><info></info></system></show>"
SESSION_COMMAND = "<show><session><info></info></session></show>"
MANAGEMENT_COMMAND = "<show><system><resources></resources></system></show>"
INTERFACE_STATUS_COMMAND = "<show><interface>all</interface></show>"
HA_COMMAND = "<show><high-availability><state></state></high-availability></show>"
STORAGE_COMMAND = "<show><system><disk-space></disk-space></system></show>"
THERMAL_COMMAND = "<show><system><environmentals><thermal></thermal></environmentals></system></show>"
FAN_COMMAND = "<show><system><environmentals><fans></fans></environmentals></system></show>"
POWER_COMMAND = "<show><system><environmentals><power></power></environmentals></system></show>"
DATAPLANE_COMMAND = (
    "<show><running><resource-monitor><second><last>1</last></second>"
    "</resource-monitor></running></show>"
)
COUNTER_COMMAND = (
    "<show><counter><global><filter><severity>drop</severity></filter>"
    "</global></counter></show>"
)
INTERFACE_COMMAND = "<show><counter><interface>all</interface></counter></show>"

# Keep cardinality bounded. These are stable, high-value failure/drop counters.
COUNTER_ALLOWLIST = {
    "flow_policy_deny",
    "flow_dos_ag_max_sess_limit",
    "flow_dos_cl_max_sess_limit",
    "flow_dos_drop_ip_blocked",
    "flow_dos_rule_deny",
    "flow_dos_rule_drop",
    "flow_dos_rule_drop_aggr",
    "flow_dos_rule_drop_classified",
    "flow_rcv_dot1q_tag_err",
    "flow_scan_drop",
    "flow_tcp_non_syn",
    "pkt_alloc_fail",
    "pkt_alloc_failure",
    "session_discard",
    "tcp_alloc_wqe_failed",
    "tcp_drop_out_of_wnd",
    "tcp_drop_packet",
}


class ApiError(RuntimeError):
    """PAN-OS API request or response failure."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _number(value: object):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return None
    number = float(text)
    return int(number) if number.is_integer() else number


def _last_number(value: object):
    """Parse a scalar or the comma-separated history returned by resource-monitor."""
    if value is None:
        return None
    samples = str(value).strip().replace("%", "").split(",")
    numbers = [_number(sample) for sample in samples]
    valid = [number for number in numbers if number is not None]
    return valid[-1] if valid else None


def _first_number(root: ET.Element, *names: str):
    wanted = {name.lower().replace("_", "-") for name in names}
    for element in root.iter():
        name = _local_name(element.tag).lower().replace("_", "-")
        if name in wanted:
            value = _number(element.text)
            if value is not None:
                return value
    return None


def _result(root: ET.Element) -> ET.Element:
    if root.attrib.get("status") != "success":
        message = " ".join(text.strip() for text in root.itertext() if text.strip())
        raise ApiError(message or "PAN-OS returned an unsuccessful response")
    result = root.find("result")
    if result is None:
        raise ApiError("PAN-OS response does not contain a result element")
    return result


def request_xml(config: dict, command: str) -> ET.Element:
    key_name = config["api_key_env"]
    api_key = os.environ.get(key_name)
    if not api_key:
        raise ApiError(f"environment variable {key_name} is not set")

    host = config["host"]
    port = int(config.get("port", 443))
    request = urllib.request.Request(
        f"https://{host}:{port}/api/",
        data=urllib.parse.urlencode({"type": "op", "cmd": command}).encode(),
        headers={"X-PAN-KEY": api_key, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    context = ssl.create_default_context()
    if not config.get("verify_tls", True):
        context = ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("timeout", 15)), context=context) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ApiError(f"request failed: {exc}") from exc
    try:
        return _result(ET.fromstring(payload))
    except ET.ParseError as exc:
        raise ApiError("PAN-OS returned invalid XML") from exc


def parse_system_info(result: ET.Element) -> dict:
    system = result.find(".//system")
    if system is None:
        system = result
    fields = {}
    aliases = {
        "model": "model",
        "serial": "serial",
        "sw-version": "panos_version",
        "uptime": "uptime",
    }
    for element in system.iter():
        field = aliases.get(_local_name(element.tag))
        if field and element.text:
            fields[field] = element.text.strip()
    uptime_seconds = parse_uptime_seconds(fields.get("uptime"))
    if uptime_seconds is not None:
        fields["uptime_seconds"] = uptime_seconds
    return fields


def parse_uptime_seconds(value: object):
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d+):(\d+)", text, re.I)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_sessions(result: ET.Element) -> dict:
    aliases = {
        "sessions_active": ("num-active", "active-sessions", "num-installed"),
        "sessions_max": ("num-max", "max-sessions"),
        "sessions_tcp": ("num-tcp", "num-active-tcp"),
        "sessions_udp": ("num-udp", "num-active-udp"),
        "sessions_icmp": ("num-icmp", "num-active-icmp"),
        "cps": ("cps", "session-cps"),
        "packet_rate_pps": ("pps", "packet-rate"),
    }
    fields = {}
    for output, names in aliases.items():
        value = _first_number(result, *names)
        if value is not None:
            fields[output] = value
    active, maximum = fields.get("sessions_active"), fields.get("sessions_max")
    if active is not None and maximum:
        fields["session_utilization_pct"] = float(active) / float(maximum) * 100.0
    return fields


def _result_text(result: ET.Element) -> str:
    return "\n".join(text for text in result.itertext() if text and text.strip())


def parse_management_resources(result: ET.Element) -> dict:
    text = _result_text(result)
    fields = {}
    load = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", text, re.I)
    if load:
        fields.update(
            load_1m=float(load.group(1)),
            load_5m=float(load.group(2)),
            load_15m=float(load.group(3)),
        )
    cpu = re.search(r"%?Cpu(?:\(s\))?\s*:\s*(.*?)(?:\n|$)", text, re.I)
    if cpu:
        idle = re.search(r"([\d.]+)\s*%?\s*(?:id|idle)\b", cpu.group(1), re.I)
        if idle:
            fields["mp_cpu_pct"] = max(0.0, 100.0 - float(idle.group(1)))
    memory = re.search(
        r"(KiB|MiB|GiB)\s+Mem\s*:\s*([\d.]+)\s+total,\s*([\d.]+)\s+free,\s*([\d.]+)\s+used",
        text,
        re.I,
    )
    if memory:
        multiplier = {"kib": 1024, "mib": 1024**2, "gib": 1024**3}[memory.group(1).lower()]
        total = float(memory.group(2)) * multiplier
        free = float(memory.group(3)) * multiplier
        used = float(memory.group(4)) * multiplier
        fields.update(memory_total_bytes=total, memory_free_bytes=free, memory_used_bytes=used)
        if total:
            fields["memory_used_pct"] = used / total * 100.0
    else:
        legacy = re.search(
            r"Mem\s*:\s*([\d.]+)([kmg])?\s+total,\s*([\d.]+)([kmg])?\s+used,\s*([\d.]+)([kmg])?\s+free",
            text,
            re.I,
        )
        if legacy:
            multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
            total = float(legacy.group(1)) * multipliers[(legacy.group(2) or "").lower()]
            used = float(legacy.group(3)) * multipliers[(legacy.group(4) or "").lower()]
            free = float(legacy.group(5)) * multipliers[(legacy.group(6) or "").lower()]
            fields.update(memory_total_bytes=total, memory_free_bytes=free, memory_used_bytes=used)
            if total:
                fields["memory_used_pct"] = used / total * 100.0
    return fields


def parse_dataplane_resources(result: ET.Element) -> list[tuple[dict, dict]]:
    """Return per-core average CPU values from the variable resource-monitor XML tree."""
    points = []

    def walk(node: ET.Element, dataplane: str | None = None):
        local = _local_name(node.tag).lower()
        candidate = node.attrib.get("name", local).lower()
        if re.search(r"(?i)(?:^|[-_])(?:s(?:lot)?\d+[-_]?)?dp\d+$", candidate):
            dataplane = candidate
        if local == "cpu-load-average":
            for entry in node.findall("./entry"):
                core = (entry.findtext("coreid") or entry.findtext("core") or "all").strip()
                value = _last_number(entry.findtext("value"))
                if value is not None:
                    points.append(
                        ({"dataplane": dataplane or "dp0", "core": core}, {"cpu_pct": float(value)})
                    )
        for child in node:
            walk(child, dataplane)

    walk(result)
    deduplicated = {}
    for tags, fields in points:
        deduplicated[(tags["dataplane"], tags["core"])] = (tags, fields)
    per_dataplane = {}
    for tags, fields in deduplicated.values():
        per_dataplane.setdefault(tags["dataplane"], []).append(float(fields["cpu_pct"]))
    for dataplane, values in per_dataplane.items():
        deduplicated[(dataplane, "average")] = (
            {"dataplane": dataplane, "core": "average"},
            {"cpu_pct": sum(values) / len(values)},
        )
    return list(deduplicated.values())


def parse_dataplane_utilization(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []

    def walk(node: ET.Element, dataplane: str | None = None):
        local = _local_name(node.tag).lower()
        candidate = node.attrib.get("name", local).lower()
        if re.search(r"(?i)(?:^|[-_])(?:s(?:lot)?\d+[-_]?)?dp\d+$", candidate):
            dataplane = candidate
        if local == "resource-utilization":
            for entry in node.findall("./entry"):
                resource = re.sub(r"[^a-z0-9]+", "_", (entry.findtext("name") or "").lower()).strip("_")
                value = _last_number(entry.findtext("value"))
                if resource and value is not None:
                    points.append(
                        ({"dataplane": dataplane or "dp0", "resource": resource}, {"utilization_pct": float(value)})
                    )
        for child in node:
            walk(child, dataplane)

    walk(result)
    return points


def parse_global_counters(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []
    for entry in result.iter():
        if _local_name(entry.tag).lower() != "entry":
            continue
        name_node = entry.find("name")
        value_node = entry.find("value")
        name = (entry.attrib.get("name") or (name_node.text if name_node is not None else "") or "").strip()
        normalized = name.lower().replace("-", "_")
        value = _number(value_node.text if value_node is not None else None)
        if normalized in COUNTER_ALLOWLIST and value is not None:
            points.append(({"counter": normalized}, {"value": value}))
    return points


def parse_interface_counters(result: ET.Element) -> list[tuple[dict, dict]]:
    """Return cumulative hardware interface counters used to derive rates."""
    aliases = {
        "ibytes": "in_octets",
        "obytes": "out_octets",
        "ipackets": "in_packets",
        "opackets": "out_packets",
        "ierrors": "in_errors",
        "idrops": "in_discards",
    }
    points = []
    for entry in result.findall("./hw/entry"):
        interface = (entry.findtext("name") or "").strip()
        if not interface:
            continue
        fields = {}
        for source, destination in aliases.items():
            value = _number(entry.findtext(source))
            if value is not None:
                fields[destination] = value
        if fields:
            points.append(({"interface": interface}, fields))
    return points


def parse_interface_status(result: ET.Element) -> list[tuple[dict, dict]]:
    interfaces = {}
    aliases = {
        "state": "state",
        "duplex": "duplex",
        "mode": "mode",
        "st": "link_summary",
        "zone": "zone",
        "vsys": "vsys",
        "fwd": "forwarding",
    }
    for section in ("hw", "ifnet"):
        for entry in result.findall(f"./{section}/entry"):
            interface = (entry.findtext("name") or "").strip()
            if not interface:
                continue
            fields = interfaces.setdefault(interface, {})
            speed = _number(entry.findtext("speed"))
            if speed is not None:
                fields["speed_mbps"] = speed
            for source, destination in aliases.items():
                value = (entry.findtext(source) or "").strip()
                if value:
                    fields[destination] = value
    return [({"interface": interface}, fields) for interface, fields in sorted(interfaces.items()) if fields]


def parse_ha_state(result: ET.Element) -> dict:
    enabled_text = (result.findtext(".//enabled") or "").strip().lower()
    enabled = enabled_text in {"yes", "true", "1", "enabled"}
    fields = {"enabled": enabled}
    if not enabled:
        fields["state"] = "standalone"
        return fields
    state = (result.findtext(".//local-info/state") or result.findtext(".//state") or "unknown").strip()
    mode = (result.findtext(".//local-info/mode") or result.findtext(".//mode") or "").strip()
    group = (result.findtext(".//group") or "").strip()
    fields["state"] = state
    if mode:
        fields["mode"] = mode
    if group:
        fields["group"] = group
    return fields


def _size_bytes(value: str):
    match = re.fullmatch(r"([\d.]+)([kmgtpe]?)", value.strip(), re.I)
    if not match:
        return None
    return (
        int(float(match.group(1)) * 1024 ** ("kmgtpe".index(match.group(2).lower()) + 1))
        if match.group(2)
        else int(float(match.group(1)))
    )


def parse_storage(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []
    pattern = re.compile(
        r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)%\s+(\S+)$"
    )
    for line in _result_text(result).splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        filesystem, total, used, available, used_pct, mount = match.groups()
        fields = {
            "total_bytes": _size_bytes(total),
            "used_bytes": _size_bytes(used),
            "available_bytes": _size_bytes(available),
            "used_pct": float(used_pct),
        }
        points.append(({"filesystem": filesystem, "mount": mount}, fields))
    return points


def parse_environmentals(result: ET.Element, sensor_type: str) -> list[tuple[dict, dict]]:
    numeric_fields = {
        "degreesc": "degrees_c",
        "rpm": "rpm",
        "rpms": "rpm",
        "watts": "watts",
        "volts": "volts",
        "amps": "amps",
        "value": "value",
        "min": "min",
        "max": "max",
    }
    points = []
    for entry in result.findall(".//entry"):
        description = (entry.findtext("description") or entry.findtext("name") or "unknown").strip()
        slot = (entry.findtext("slot") or "system").strip()
        fields = {}
        for child in entry:
            destination = numeric_fields.get(_local_name(child.tag).lower())
            if destination:
                value = _number(child.text)
                if value is not None:
                    fields[destination] = value
        alarm = (entry.findtext("alarm") or "").strip()
        if alarm:
            fields["alarm"] = alarm
        if fields:
            points.append(
                ({"sensor_type": sensor_type, "slot": slot, "description": description}, fields)
            )
    return points


def _escape(value: object, *, tag: bool = False) -> str:
    text = str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")
    if tag:
        text = text.replace("=", "\\=")
    return text


def line_protocol(measurement: str, tags: dict, fields: dict) -> str | None:
    clean = {name: value for name, value in fields.items() if value is not None}
    if not clean:
        return None
    tag_text = "".join(f",{_escape(name)}={_escape(value, tag=True)}" for name, value in sorted(tags.items()))
    encoded = []
    for name, value in sorted(clean.items()):
        if isinstance(value, bool):
            output = "true" if value else "false"
        elif isinstance(value, int):
            output = f"{value}i"
        elif isinstance(value, float):
            output = repr(value)
        else:
            output = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        encoded.append(f"{_escape(name)}={output}")
    return f"{_escape(measurement)}{tag_text} {','.join(encoded)}"


def collect_firewall(config: dict, due: set[str]) -> list[str]:
    hostname = config["hostname"]
    output = []
    commands = {
        "sessions": (SESSION_COMMAND, parse_sessions, "paloalto_api_sessions"),
        "management": (MANAGEMENT_COMMAND, parse_management_resources, "paloalto_api_management"),
        "interfaces": (INTERFACE_COMMAND, parse_interface_counters, "paloalto_api_interfaces"),
        "interface_status": (INTERFACE_STATUS_COMMAND, parse_interface_status, "paloalto_api_interfaces"),
        "ha": (HA_COMMAND, parse_ha_state, "paloalto_api_ha"),
        "storage": (STORAGE_COMMAND, parse_storage, "paloalto_api_storage"),
        "thermal": (THERMAL_COMMAND, lambda result: parse_environmentals(result, "thermal"), "paloalto_api_sensors"),
        "fans": (FAN_COMMAND, lambda result: parse_environmentals(result, "fan"), "paloalto_api_sensors"),
        "power": (POWER_COMMAND, lambda result: parse_environmentals(result, "power"), "paloalto_api_sensors"),
        "counters": (COUNTER_COMMAND, parse_global_counters, "paloalto_api_counters"),
        "system": (SYSTEM_INFO_COMMAND, parse_system_info, "paloalto_api_system"),
    }
    categories = (
        "sessions",
        "interfaces",
        "interface_status",
        "management",
        "dataplane",
        "counters",
        "ha",
        "storage",
        "thermal",
        "fans",
        "power",
        "system",
    )
    for category in categories:
        if category not in due:
            continue
        try:
            if category == "dataplane":
                result = request_xml(config, DATAPLANE_COMMAND)
                parsed_sets = (
                    ("paloalto_api_dataplane_cpu", parse_dataplane_resources(result)),
                    ("paloalto_api_dataplane_resources", parse_dataplane_utilization(result)),
                )
            else:
                command, parser, measurement = commands[category]
                parsed = parser(request_xml(config, command))
                parsed_sets = ((measurement, parsed if isinstance(parsed, list) else [({}, parsed)]),)
            for measurement, points in parsed_sets:
                for extra_tags, fields in points:
                    line = line_protocol(measurement, {"hostname": hostname, **extra_tags}, fields)
                    if line:
                        output.append(line)
        except Exception as exc:  # keep other categories and firewalls alive
            print(f"paloalto-api [{hostname}] {category}: {exc}", file=sys.stderr, flush=True)
    return output


def load_config(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("collector configuration must be a JSON list")
    return data


def load_environment_file(path: Path) -> None:
    """Load the simple NAME=VALUE file generated for the Telegraf runtime."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid environment variable name in {path}: {name!r}")
        os.environ.setdefault(name, value.strip())


def run_once(configs: list[dict], categories: set[str] | None = None) -> int:
    selected = categories or {
        "sessions", "interfaces", "interface_status", "management", "dataplane",
        "counters", "ha", "storage", "thermal", "fans", "power", "system",
    }
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(configs)))) as executor:
        futures = [executor.submit(collect_firewall, config, selected) for config in configs]
        for future in as_completed(futures):
            for line in future.result():
                print(line, flush=True)
    return 0


def run_daemon(configs: list[dict]) -> int:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopped.set())
    signal.signal(signal.SIGINT, lambda *_args: stopped.set())
    schedules = {
        "sessions": lambda cfg: int(cfg.get("interval", 20)),
        "interfaces": lambda cfg: int(cfg.get("interval", 20)),
        "interface_status": lambda cfg: int(cfg.get("resource_interval", 60)),
        "management": lambda cfg: int(cfg.get("resource_interval", 60)),
        "dataplane": lambda cfg: int(cfg.get("resource_interval", 60)),
        "counters": lambda cfg: int(cfg.get("counter_interval", 60)),
        "ha": lambda cfg: int(cfg.get("resource_interval", 60)),
        "thermal": lambda cfg: int(cfg.get("resource_interval", 60)),
        "fans": lambda cfg: int(cfg.get("resource_interval", 60)),
        "power": lambda cfg: int(cfg.get("resource_interval", 60)),
        "storage": lambda cfg: int(cfg.get("system_interval", 3600)),
        "system": lambda cfg: int(cfg.get("system_interval", 3600)),
    }
    next_due = {(index, category): 0.0 for index in range(len(configs)) for category in schedules}
    while not stopped.is_set():
        now = time.monotonic()
        jobs = []
        for index, config in enumerate(configs):
            due = {category for category in schedules if next_due[(index, category)] <= now}
            if due:
                jobs.append((config, due))
                for category in due:
                    next_due[(index, category)] = now + schedules[category](config)
        if jobs:
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as executor:
                futures = [executor.submit(collect_firewall, config, due) for config, due in jobs]
                for future in as_completed(futures):
                    for line in future.result():
                        print(line, flush=True)
        stopped.wait(1.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/telegraf/paloalto-api.json"))
    parser.add_argument("--env-file", type=Path, help="load API keys from a generated NAME=VALUE file")
    parser.add_argument("--once", action="store_true", help="collect every category once and exit")
    args = parser.parse_args(argv)
    if args.env_file:
        load_environment_file(args.env_file)
    configs = load_config(args.config)
    if not configs:
        return 0
    return run_once(configs) if args.once else run_daemon(configs)


if __name__ == "__main__":
    raise SystemExit(main())
