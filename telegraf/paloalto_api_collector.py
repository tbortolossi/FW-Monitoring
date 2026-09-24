#!/usr/bin/env python3
"""Collect basic Palo Alto performance metrics through the PAN-OS XML API.

The process is designed for Telegraf's ``inputs.execd`` plugin.  It keeps API
calls for a given firewall sequentially while polling different firewalls in
parallel, then emits InfluxDB line protocol on stdout.

Field typing: InfluxDB rejects a point whose field type differs from the type
already stored for that field, so every physical sensor value (temperature,
fan RPM, watts, volts, amps, sensor value/min/max and chassis power figures)
is always emitted as a float, even when PAN-OS prints it as "12". Counters and
other integer-natured values (sessions, CPS, octets, packets) stay integers.

Optional categories (VSYS, environmentals, ingress backlogs, log receiver,
GlobalProtect, software processes, RAID) are disabled for a firewall once
PAN-OS rejects their command, so they never affect other categories.
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
SESSION_METER_COMMAND = "<show><session><meter></meter></session></show>"
MANAGEMENT_COMMAND = "<show><system><resources></resources></system></show>"
INTERFACE_STATUS_COMMAND = "<show><interface>all</interface></show>"
HA_COMMAND = "<show><high-availability><state></state></high-availability></show>"
STORAGE_COMMAND = "<show><system><disk-space></disk-space></system></show>"
THERMAL_COMMAND = "<show><system><environmentals><thermal></thermal></environmentals></system></show>"
FAN_COMMAND = "<show><system><environmentals><fans></fans></environmentals></system></show>"
POWER_COMMAND = "<show><system><environmentals><power></power></environmentals></system></show>"
CHASSIS_INVENTORY_COMMAND = "<show><chassis><inventory></inventory></chassis></show>"
CHASSIS_STATUS_COMMAND = "<show><chassis><status></status></chassis></show>"
CHASSIS_POWER_COMMAND = "<show><chassis><power></power></chassis></show>"
# The last completed one-minute bucket gives a per-core average and a per-core
# maximum, which is more representative than a single one-second sample.
DATAPLANE_COMMAND = (
    "<show><running><resource-monitor><minute><last>1</last></minute>"
    "</resource-monitor></running></show>"
)
INGRESS_BACKLOGS_COMMAND = (
    "<show><running><resource-monitor><ingress-backlogs></ingress-backlogs>"
    "</resource-monitor></running></show>"
)
LOGGING_COMMAND = "<debug><log-receiver><statistics></statistics></log-receiver></debug>"
GLOBALPROTECT_COMMAND = (
    "<show><global-protect-gateway><statistics></statistics></global-protect-gateway></show>"
)
SOFTWARE_COMMAND = "<show><system><software><status></status></software></system></show>"
RAID_COMMAND = "<show><system><raid><detail></detail></raid></system></show>"
COUNTER_COMMAND = (
    "<show><counter><global><filter><severity>drop</severity></filter>"
    "</global></counter></show>"
)
# DoS/zone-protection counters include informational SYN-cookie counters and
# block-table gauges that the severity=drop filter does not return.
COUNTER_DOS_COMMAND = (
    "<show><counter><global><filter><aspect>dos</aspect></filter>"
    "</global></counter></show>"
)
INTERFACE_COMMAND = "<show><counter><interface>all</interface></counter></show>"

# Always retain these high-value counters when PAN-OS returns more active drop
# counters than the configured cardinality limit.
PRIORITY_COUNTERS = {
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

# Per-reason drop counters exposed for logical interfaces in the ifnet section
# of ``show counter interface all``.
LOGICAL_DROP_COUNTERS = (
    "flowstate",
    "noroute",
    "noarp",
    "noneigh",
    "neighpend",
    "nomac",
    "zonechange",
    "land",
    "pod",
    "teardrop",
    "ipspoof",
    "macspoof",
    "icmp_frag",
)

# PAN-OS messages for commands that a platform or release does not implement.
# Also matches authorization failures: an admin role that lacks the right for an
# optional command (for example the debug log-receiver statistics) should have
# that category disabled once instead of logging the refusal on every poll.
UNSUPPORTED_PATTERN = re.compile(
    r"(?i)invalid syntax|not supported|unsupported|unknown command|is unexpected|is not a valid"
    r"|not authorized|unauthorized|permission denied|insufficient privilege|forbidden|access denied"
)
# ``show session meter`` lists every VSYS slot the platform can host, including
# ones that are not configured; a VSYS-scoped command on such a slot fails with
# this message.
INVALID_VSYS_PATTERN = re.compile(r"(?i)valid vsys|invalid vsys|vsys .* does not exist")
# Categories that some platforms or releases do not implement. They are
# disabled for that firewall after PAN-OS rejects the command.
OPTIONAL_CATEGORIES = {
    "vsys",
    "thermal",
    "fans",
    "power",
    "ingress_backlogs",
    "logging",
    "globalprotect",
    "software",
    "raid",
}
# Extra messages that mean "feature not in use" for specific optional
# categories, e.g. GlobalProtect statistics on a firewall without a gateway.
OPTIONAL_DISABLE_PATTERNS = {
    "globalprotect": re.compile(r"(?i)not configured|no gateway|not enabled|not licensed|no such"),
}
DATAPLANE_NAME_PATTERN = re.compile(r"(?i)(?:^|[-_])(?:s(?:lot)?\d+[-_]?)?dp\d+$")


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


def _float(value: object):
    number = _number(value)
    return float(number) if number is not None else None


def _first_number(root: ET.Element, *names: str):
    """Return the value of the first alias present anywhere, in alias priority order.

    Document order does not matter: ``num-active`` wins over ``num-installed``
    even when PAN-OS lists the cumulative installed count first.
    """
    elements = [(_local_name(element.tag).lower().replace("_", "-"), element) for element in root.iter()]
    for alias in names:
        wanted = alias.lower().replace("_", "-")
        for name, element in elements:
            if name == wanted:
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


def request_xml(config: dict, command: str, vsys: str | None = None) -> ET.Element:
    """Run an operational command; ``vsys`` scopes it to one virtual system."""
    key_name = config["api_key_env"]
    api_key = os.environ.get(key_name)
    if not api_key:
        raise ApiError(f"environment variable {key_name} is not set")

    host = config["host"]
    port = int(config.get("port", 443))
    params = {"type": "op", "cmd": command}
    if vsys:
        params["vsys"] = vsys
    request = urllib.request.Request(
        f"https://{host}:{port}/api/",
        data=urllib.parse.urlencode(params).encode(),
        headers={"X-PAN-KEY": api_key, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    context = ssl.create_default_context()
    if not config.get("verify_tls", True):
        context = ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("timeout", 15)), context=context) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        # PAN-OS explains 4xx answers in the body ("You must specify a valid vsys").
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover - body already consumed or unreadable
            detail = ""
        raise ApiError(f"request failed: HTTP {exc.code}: {detail or exc.reason}") from exc
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
        "app-version": "app_version",
        "threat-version": "threat_version",
        "av-version": "av_version",
        "wildfire-version": "wildfire_version",
        "url-filtering-version": "url_filtering_version",
        "multi-vsys": "multi_vsys",
        "operational-mode": "operational_mode",
        "device-certificate-status": "device_certificate_status",
        "family": "family",
    }
    for element in system.iter():
        field = aliases.get(_local_name(element.tag))
        if field and element.text and element.text.strip():
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


def _vsys_name(value: object) -> str:
    """Normalize PAN-OS VSYS identifiers such as ``1`` to ``vsys1``."""
    text = str(value or "").strip().lower()
    if re.fullmatch(r"\d+", text):
        return f"vsys{text}"
    if re.fullmatch(r"vsys\d+", text):
        return text
    return ""


def parse_session_meter(result: ET.Element) -> list[tuple[dict, dict]]:
    """Return per-VSYS session counts summed across dataplanes."""
    per_vsys = {}
    for entry in result.iter():
        if _local_name(entry.tag).lower() != "entry":
            continue
        vsys = _vsys_name(_entry_text(entry, "vsys", "vsys-id", "id") or entry.attrib.get("name"))
        current = _number(_entry_text(entry, "current", "count", "num-active"))
        if not vsys or current is None:
            continue
        fields = per_vsys.setdefault(vsys, {"sessions_active": 0, "sessions_throttled": 0})
        fields["sessions_active"] += current
        throttled = _number(_entry_text(entry, "throttled"))
        if throttled is not None:
            fields["sessions_throttled"] += throttled
        # A zero maximum means that no VSYS session limit is configured.
        maximum = _number(_entry_text(entry, "maximum", "max", "limit"))
        if maximum:
            fields["sessions_max"] = max(fields.get("sessions_max", 0), maximum)
    points = []
    for vsys, fields in sorted(per_vsys.items()):
        if fields.get("sessions_max"):
            fields["session_utilization_pct"] = float(fields["sessions_active"]) / float(fields["sessions_max"]) * 100.0
        points.append(({"vsys": vsys}, fields))
    return points


def _result_text(result: ET.Element) -> str:
    return "\n".join(text for text in result.itertext() if text and text.strip())


_TOP_SUMMARY_UNITS = {"kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _top_summary_bytes(text: str, label: str):
    """Parse a procps-ng summary line such as `MiB Mem : 1000 total, 250 free, 750 used`.

    top appends `+` to a value wider than its column. When only decimals were dropped
    (`1031206.+total`, seen on PA-5580 management planes) the value is exact to one unit
    and is kept; when integer digits may be missing (`13184950+total`) the line is ignored.
    """
    line = re.search(rf"\b(KiB|MiB|GiB|TiB)\s+{label}\s*:(.*)", text, re.I)
    if not line:
        return None
    multiplier = _TOP_SUMMARY_UNITS[line.group(1).lower()]
    values = {}
    for number, truncated, name in re.findall(r"([\d.]+)(\+?)\s*(total|free|used)\b", line.group(2), re.I):
        if truncated and "." not in number:
            return None
        values[name.lower()] = float(number) * multiplier
    return values if len(values) == 3 else None


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
        iowait = re.search(r"([\d.]+)\s*%?\s*wa\b", cpu.group(1), re.I)
        if iowait:
            fields["cpu_iowait_pct"] = float(iowait.group(1))
    tasks = re.search(
        r"Tasks:\s*(\d+)\s+total,\s*(\d+)\s+running,\s*(\d+)\s+sleeping,"
        r"\s*(\d+)\s+stopped,\s*(\d+)\s+zombie",
        text,
        re.I,
    )
    if tasks:
        fields.update(
            tasks_total=int(tasks.group(1)),
            tasks_running=int(tasks.group(2)),
            tasks_sleeping=int(tasks.group(3)),
            tasks_stopped=int(tasks.group(4)),
            tasks_zombie=int(tasks.group(5)),
        )
    memory = _top_summary_bytes(text, "Mem")
    if memory:
        fields.update(
            memory_total_bytes=memory["total"],
            memory_free_bytes=memory["free"],
            memory_used_bytes=memory["used"],
        )
        if memory["total"]:
            fields["memory_used_pct"] = memory["used"] / memory["total"] * 100.0
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
    swap = _top_summary_bytes(text, "Swap")
    if swap:
        fields.update(swap_total_bytes=swap["total"], swap_free_bytes=swap["free"], swap_used_bytes=swap["used"])
        fields["swap_used_pct"] = swap["used"] / swap["total"] * 100.0 if swap["total"] else 0.0
    return fields


def _top_memory_bytes(value: str):
    match = re.fullmatch(r"([\d.]+)([kmgt]?)", value.strip(), re.I)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    return int(number * (1024 ** ({"": 1, "k": 1, "m": 2, "g": 3, "t": 4}[suffix])))


def parse_management_processes(result: ET.Element, limit: int = 32) -> list[tuple[dict, dict]]:
    """Aggregate the top snapshot by process name to keep cardinality bounded."""
    aggregated = {}
    pattern = re.compile(
        r"^\s*\d+\s+\S+\s+\S+\s+\S+\s+(\S+)\s+(\S+)\s+\S+\s+\S\s+"
        r"([\d.]+)\s+([\d.]+)\s+\S+\s+(.+?)\s*$"
    )
    for line in _result_text(result).splitlines():
        match = pattern.match(line)
        if not match:
            continue
        virtual, resident, cpu_pct, memory_pct, command = match.groups()
        process = command.split()[0].strip("[]")[:80]
        if not process:
            continue
        fields = aggregated.setdefault(
            process,
            {"cpu_pct": 0.0, "memory_pct": 0.0, "processes": 0, "resident_bytes": 0},
        )
        fields["cpu_pct"] += float(cpu_pct)
        fields["memory_pct"] += float(memory_pct)
        fields["processes"] += 1
        fields["resident_bytes"] += _top_memory_bytes(resident) or 0
        virtual_bytes = _top_memory_bytes(virtual)
        if virtual_bytes is not None:
            fields["virtual_bytes"] = fields.get("virtual_bytes", 0) + virtual_bytes
    ranked = sorted(
        aggregated.items(),
        key=lambda item: (item[1]["cpu_pct"], item[1]["memory_pct"], item[0]),
        reverse=True,
    )
    return [({"process": process}, fields) for process, fields in ranked[:limit]]


def parse_dataplane_resources(result: ET.Element) -> list[tuple[dict, dict]]:
    """Return per-core CPU values from the variable resource-monitor XML tree.

    ``cpu_pct`` comes from ``cpu-load-average`` and ``cpu_max_pct`` from
    ``cpu-load-maximum`` of the same bucket (the last completed minute).  A
    synthetic ``core="average"`` point per dataplane carries the mean of the
    per-core averages and the max of the per-core maxima.
    """
    tables = {"cpu-load-average": "cpu_pct", "cpu-load-maximum": "cpu_max_pct"}
    values = {}

    def walk(node: ET.Element, dataplane: str | None = None):
        local = _local_name(node.tag).lower()
        candidate = node.attrib.get("name", local).lower()
        if DATAPLANE_NAME_PATTERN.search(candidate):
            dataplane = candidate
        field = tables.get(local)
        if field:
            for entry in node.findall("./entry"):
                core = (entry.findtext("coreid") or entry.findtext("core") or "all").strip()
                value = _last_number(entry.findtext("value"))
                if value is not None:
                    values.setdefault((dataplane or "dp0", core), {})[field] = float(value)
        for child in node:
            walk(child, dataplane)

    walk(result)
    points = {key: ({"dataplane": key[0], "core": key[1]}, fields) for key, fields in values.items()}
    per_dataplane = {}
    for (dataplane, _core), fields in values.items():
        per_dataplane.setdefault(dataplane, []).append(fields)
    for dataplane, cores in per_dataplane.items():
        averages = [fields["cpu_pct"] for fields in cores if "cpu_pct" in fields]
        maxima = [fields["cpu_max_pct"] for fields in cores if "cpu_max_pct" in fields]
        summary = {}
        if averages:
            summary["cpu_pct"] = sum(averages) / len(averages)
            # PAN-OS lists every core of the dataplane, but only the pan task
            # cores process packets (80 of 128 on a PA-5580 DP); the others stay
            # at exactly 0% and halve the all-core average that SNMP reports.
            active = [
                fields["cpu_pct"]
                for fields in cores
                if "cpu_pct" in fields and fields.get("cpu_max_pct", fields["cpu_pct"]) > 0
            ]
            summary["cpu_active_pct"] = sum(active) / len(active) if active else 0.0
            summary["active_cores"] = len(active)
        if maxima:
            summary["cpu_max_pct"] = max(maxima)
        points[(dataplane, "average")] = ({"dataplane": dataplane, "core": "average"}, summary)
    return list(points.values())


def parse_dataplane_utilization(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []

    def walk(node: ET.Element, dataplane: str | None = None):
        local = _local_name(node.tag).lower()
        candidate = node.attrib.get("name", local).lower()
        if DATAPLANE_NAME_PATTERN.search(candidate):
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


def parse_global_counters(result: ET.Element, limit: int = 256) -> list[tuple[dict, dict]]:
    return rank_global_counters(_global_counter_points(result), limit)


def rank_global_counters(points: list[tuple[dict, dict]], limit: int = 256) -> list[tuple[dict, dict]]:
    """Deduplicate counters by name and keep priority, then busiest, counters."""
    unique = {}
    for tags, fields in points:
        unique.setdefault(tags["counter"], (tags, fields))
    ranked = sorted(
        unique.values(),
        key=lambda point: (
            point[0]["counter"] in PRIORITY_COUNTERS,
            float(point[1].get("rate", 0)),
            float(point[1]["value"]),
        ),
        reverse=True,
    )
    return ranked[:limit]


def _global_counter_points(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []
    for entry in result.iter():
        if _local_name(entry.tag).lower() != "entry":
            continue
        name_node = entry.find("name")
        name = (entry.attrib.get("name") or (name_node.text if name_node is not None else "") or "").strip()
        normalized = name.lower().replace("-", "_")
        value = _number(entry.findtext("value"))
        if not normalized or value is None or value <= 0:
            continue
        rate = _number(entry.findtext("rate"))
        tags = {"counter": normalized}
        for field in ("severity", "category", "aspect"):
            text = (entry.findtext(field) or "").strip().lower()
            tags[field] = text or "unknown"
        fields = {"value": value}
        if rate is not None:
            fields["rate"] = rate
        description = (entry.findtext("description") or entry.findtext("desc") or "").strip()
        if description:
            fields["description"] = description
        points.append((tags, fields))
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
        # MAC-level port statistics add egress errors and link flaps.
        for source, destination in (("tx-error", "out_errors"), ("link-down", "link_down_count")):
            value = _number(entry.findtext(f"port/{source}"))
            if value is not None:
                fields[destination] = value
        # ibytes/obytes and ipackets/opackets are maintained by the dataplane and
        # miss hardware-offloaded flows; the MAC-level port counters see every
        # frame on the wire, like SNMP ifHCInOctets. Prefer them when present.
        for direction, octets, packets in (("rx", "in_octets", "in_packets"), ("tx", "out_octets", "out_packets")):
            value = _number(entry.findtext(f"port/{direction}-bytes"))
            if value is not None:
                fields[octets] = value
            frames = [
                _number(entry.findtext(f"port/{direction}-{kind}"))
                for kind in ("unicast", "multicast", "broadcast")
            ]
            if all(frame is not None for frame in frames):
                fields[packets] = sum(frames)
        if fields:
            points.append(({"interface": interface}, fields))
    return points


def parse_logical_interface_counters(
    result: ET.Element, context: dict | None = None, limit: int = 512
) -> list[tuple[dict, dict]]:
    """Return cumulative ifnet counters for subinterfaces, tunnels and VLANs.

    ``context`` maps interface names to the zone/VSYS tags learned from
    ``show interface all`` so that Grafana can aggregate by zone and VSYS.
    """
    aliases = {
        "ibytes": "in_octets",
        "obytes": "out_octets",
        "ipackets": "in_packets",
        "opackets": "out_packets",
        "ierrors": "in_errors",
        "idrops": "in_discards",
        **{reason: f"drop_{reason}" for reason in LOGICAL_DROP_COUNTERS},
    }
    context = context or {}
    points = []
    # PAN-OS nests logical counters as ifnet/ifnet/entry; accept the flat form too.
    for entry in result.findall("./ifnet/ifnet/entry") or result.findall("./ifnet/entry"):
        interface = (entry.findtext("name") or "").strip()
        if not interface:
            continue
        fields = {}
        for source, destination in aliases.items():
            value = _number(entry.findtext(source))
            if value is not None:
                fields[destination] = value
        if fields:
            points.append(({"interface": interface, **context.get(interface, {})}, fields))
    if len(points) > limit:
        # Keep the busiest interfaces when a platform has very many logical interfaces.
        points.sort(
            key=lambda point: float(point[1].get("in_octets", 0)) + float(point[1].get("out_octets", 0)),
            reverse=True,
        )
        points = points[:limit]
    return points


def interface_context(status_points: list[tuple[dict, dict]]) -> dict:
    """Build zone/VSYS tags for logical counters from parsed interface status."""
    context = {}
    for tags, fields in status_points:
        item = {}
        zone = str(fields.get("zone", "")).strip()
        if zone and zone.lower() not in {"n/a", "none", "(none)", "unknown", "(unknown)"}:
            item["zone"] = zone
        vsys = _vsys_name(fields.get("vsys"))
        if vsys:
            item["vsys"] = vsys
        if item:
            context[tags["interface"]] = item
    return context


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

    def first_text(*paths: str) -> str:
        for path in paths:
            value = (result.findtext(path) or "").strip()
            if value:
                return value
        return ""

    state = first_text(".//local-info/state", ".//state") or "unknown"
    mode = first_text(".//local-info/mode", ".//mode")
    # <group> is a container element; its identifier lives in <group-id>.
    group = first_text(".//group/group-id", ".//group-id")
    fields["state"] = state
    if mode:
        fields["mode"] = mode
    if group:
        fields["group"] = group
    for destination, paths in (
        ("peer_state", (".//peer-info/state",)),
        ("peer_connection", (".//peer-info/conn-status",)),
        ("config_sync", (".//running-sync",)),
        ("state_sync", (".//local-info/state-sync",)),
        ("state_reason", (".//local-info/state-reason",)),
        ("state_duration", (".//local-info/state-duration",)),
        (
            "ha1_status",
            (".//local-info/ha1/conn-status", ".//ha1/conn-status", ".//peer-info/conn-ha1/conn-status"),
        ),
        (
            "ha2_status",
            (".//local-info/ha2/conn-status", ".//ha2/conn-status", ".//peer-info/conn-ha2/conn-status"),
        ),
        ("link_monitoring", (".//group/link-monitoring/enabled", ".//link-monitoring/enabled")),
        ("path_monitoring", (".//path-monitoring/enabled",)),
        ("preemptive", (".//local-info/preemptive", ".//preemptive")),
    ):
        value = first_text(*paths)
        if value:
            fields[destination] = value
    for destination, path in (
        ("local_priority", ".//local-info/priority"),
        ("peer_priority", ".//peer-info/priority"),
    ):
        value = _number(result.findtext(path))
        if value is not None:
            fields[destination] = int(value)
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
                # PAN-OS may format the same reading as "12" and later
                # "12.5"; physical values are always floats so the InfluxDB
                # field type never flips.
                value = _float(child.text)
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


def _entry_text(entry: ET.Element, *names: str) -> str:
    wanted = {name.lower().replace("_", "-") for name in names}
    for child in entry:
        name = _local_name(child.tag).lower().replace("_", "-")
        if name in wanted and child.text:
            return child.text.strip()
    return ""


def _chassis_card_type(component: str) -> str:
    normalized = component.upper()
    if re.search(r"(?:^|-)(?:SMC|MPC)(?:-|$)", normalized):
        return "supervisor"
    if re.search(r"(?:^|-)(?:NPC|DPC|LFC|SFC|NC)(?:-|$)", normalized):
        return "linecard"
    if "FAN" in normalized:
        return "fan"
    if re.search(r"(?:^|-)(?:PSU|PSA|PSB)(?:-|\d|$)", normalized):
        return "power_supply"
    return "other"


def parse_chassis_inventory(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []
    entries = result.findall(".//chassis/slots/entry") or result.findall(".//slots/entry")
    for entry in entries:
        slot = (entry.attrib.get("name") or _entry_text(entry, "slot", "slot-id", "name")).strip()
        component = _entry_text(entry, "component", "part-number", "model", "type", "name")
        if not slot or not component:
            continue
        fields = {"component": component}
        aliases = {
            "serial": ("serial", "serial-number"),
            "hardware_revision": ("hw-version", "hardware-version", "hardware-revision"),
            "software_version": ("sw-version", "software-version"),
            "status": ("operational-status", "card-status", "status"),
            "config_status": ("config-status",),
        }
        for destination, names in aliases.items():
            value = _entry_text(entry, *names)
            if value:
                fields[destination] = value
        points.append(
            (
                {"slot": slot.lower(), "card_type": _chassis_card_type(component)},
                fields,
            )
        )
    return points


def parse_chassis_status(result: ET.Element) -> list[tuple[dict, dict]]:
    """Parse the per-slot operational state returned by ``show chassis status``."""
    points = []
    entries = result.findall("./status/entry") or result.findall(".//status/entry")
    for entry in entries:
        slot = (entry.attrib.get("name") or _entry_text(entry, "slot", "slot-id", "name")).strip()
        component = _entry_text(entry, "component", "model", "type", "name") or "empty"
        if not slot:
            continue
        fields = {}
        aliases = {
            "type": ("type",),
            "status": ("status", "operational-status", "card-status"),
            "system_role": ("sysrole", "system-role"),
            "config": ("config",),
            "detail": ("detail",),
            "config_detail": ("config-detail", "config_detail"),
        }
        for destination, names in aliases.items():
            value = _entry_text(entry, *names)
            if value:
                fields[destination] = value
        disabled = _entry_text(entry, "disabled")
        if disabled:
            fields["disabled"] = disabled.lower() in {"yes", "true", "1", "disabled"}
        if fields:
            points.append(
                (
                    {"slot": slot.lower(), "card_type": _chassis_card_type(component)},
                    {"component": component, **fields},
                )
            )
    return points


def parse_chassis_power(result: ET.Element) -> list[tuple[dict, dict]]:
    points = []
    for entry in result.findall(".//entry"):
        slot = (entry.attrib.get("name") or _entry_text(entry, "slot", "name") or "chassis").strip()
        component = _entry_text(entry, "component", "description", "model", "type") or slot
        fields = {}
        status = _entry_text(entry, "card-status", "status", "state")
        if status:
            fields["status"] = status
        for source, destination in (
            (("power", "watts", "power-w"), "power_w"),
            (("provided", "provided-w"), "provided_w"),
            (("used", "used-w"), "used_w"),
            (("remaining", "remaining-w"), "remaining_w"),
        ):
            value = _float(_entry_text(entry, *source))
            if value is not None:
                fields[destination] = value
        if fields:
            points.append(({"slot": slot.lower(), "component": component[:120]}, fields))

    summary = {}
    summary_node = result.find(".//summary")
    for source, destination in (
        (("provided", "provided-w"), "provided_w"),
        (("used", "used-w"), "used_w"),
        (("remaining", "remaining-w"), "remaining_w"),
    ):
        value = _first_number(summary_node, *source) if summary_node is not None else None
        if value is not None:
            summary[destination] = float(value)
    if summary:
        points.append(({"slot": "chassis", "component": "power_summary"}, summary))
    return points


def _field_name(label: str, suffix: str = "") -> str:
    """Normalize a free-text label to a bounded ``[a-z0-9_]`` field name."""
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return (base[: 64 - len(suffix)].rstrip("_") + suffix) if base else ""


def _dataplane_label(slot: str | None, dataplane: str, known: set[str]) -> str:
    """Map a text header such as ``SLOT: s1, DP: dp0`` to resource-monitor names."""
    dataplane = dataplane.lower()
    if not slot:
        return dataplane
    slot = slot.lower()
    slot = slot if slot.startswith("s") else f"s{slot}"
    combined = f"{slot}{dataplane}"
    # Fixed platforms print a slot but resource-monitor names the DP plainly.
    if combined not in known and dataplane in known:
        return dataplane
    return combined


def parse_ingress_backlogs(result: ET.Element, known_dataplanes=()) -> list[tuple[dict, dict]]:
    """Parse ``show running resource-monitor ingress-backlogs`` per dataplane.

    Every known dataplane gets a point; a dataplane without backlog reports
    ``usage_pct=0.0`` and ``sessions=0`` so dashboards draw a flat zero.

    Accepted forms (only the text header/row layout is taken from real
    PAN-OS output; the other variants are defensive guesses):
      * text: ``-- SLOT: s1, DP: dp0 --`` / ``-- DP dp0 --`` / ``DP s1dp0``
        headers, an optional ``USAGE - ATOMIC: 92% TOTAL: 93%`` line, then
        session rows starting with a session id and a percentage
        (``6   92%   1   156``);
      * empty output or a line such as ``none`` (no backlog at all);
      * XML (guess): ``<dp name="dp0">`` or ``<entry name="s1dp0">`` nodes
        whose ``entry`` children carry a usage/pct value.
    """
    known = {str(name).lower() for name in known_dataplanes}
    per_dp: dict[str, dict] = {}

    def record(dataplane: str, usage: float | None = None, session: bool = False):
        fields = per_dp.setdefault(dataplane, {"usage_pct": 0.0, "sessions": 0})
        if usage is not None:
            fields["usage_pct"] = max(fields["usage_pct"], float(usage))
        if session:
            fields["sessions"] += 1

    usage_names = {"usage", "pct", "percent", "percentage", "usage-pct", "total"}
    xml_nodes = [
        node
        for node in result.iter()
        if node is not result
        and (
            _local_name(node.tag).lower() == "dp"
            or DATAPLANE_NAME_PATTERN.search(node.attrib.get("name", _local_name(node.tag)).lower())
        )
    ]
    for node in xml_nodes:
        name = (node.attrib.get("name") or node.findtext("name") or "").strip().lower()
        if not name:
            local = _local_name(node.tag).lower()
            name = local if local != "dp" else (node.text or "").strip().lower()
        if not DATAPLANE_NAME_PATTERN.search(name or ""):
            continue
        record(name)
        for entry in node.iter():
            if entry is node or _local_name(entry.tag).lower() != "entry":
                continue
            usage = None
            for child in entry:
                if _local_name(child.tag).lower() in usage_names:
                    usage = _number((child.text or "").replace("%", ""))
                    if usage is not None:
                        break
            if usage is not None:
                record(name, usage, session=True)

    if not xml_nodes:
        header = re.compile(
            r"(?i)^\s*(?:-+\s*)?(?:SLOT:?\s*(s?\d+)\s*,?\s*)?DP:?\s*(s?\d*[-_]?dp\d+)\s*(?:-+)?\s*$"
        )
        total = re.compile(r"(?i)(?:ATOMIC|TOTAL)\s*:\s*([\d.]+)\s*%")
        row = re.compile(r"^\s*(\d+)\s+([\d.]+)\s*%")
        current = None
        for line in _result_text(result).splitlines():
            match = header.match(line)
            if match:
                current = _dataplane_label(match.group(1), match.group(2), known)
                record(current)
                continue
            if current is None:
                continue
            match = row.match(line)
            if match:
                record(current, float(match.group(2)), session=True)
                continue
            for value in total.findall(line):
                record(current, float(value))

    for dataplane in known:
        record(dataplane)
    return [({"dataplane": dataplane}, fields) for dataplane, fields in sorted(per_dp.items())]


def parse_log_receiver(result: ET.Element) -> dict:
    """Parse ``debug log-receiver statistics`` into rates and cumulative counters."""
    fields = {}
    rate = re.compile(r"^\s*(.+?)\s+rate\s*:\s*([\d.]+)\s*/\s*sec", re.I)
    counter = re.compile(r"^\s*(.+?)\s*:\s*(\d+)\s*$")
    for line in _result_text(result).splitlines():
        match = rate.match(line)
        if match:
            name = _field_name(match.group(1), "_rate")
            if name:
                fields[name] = float(match.group(2))
            continue
        match = counter.match(line)
        if match and re.search(r"(?i)discard|dropped|total", match.group(1)):
            name = _field_name(match.group(1))
            if name:
                fields[name] = int(match.group(2))
    return fields


def parse_globalprotect(result: ET.Element) -> list[tuple[dict, dict]]:
    """Return GlobalProtect gateway user counts, overall and per gateway."""
    points = []
    totals = {}
    for destination, source in (("current_users", "TotalCurrentUsers"), ("previous_users", "TotalPreviousUsers")):
        value = _first_number(result, source)
        if value is not None:
            totals[destination] = int(value)
    if totals:
        points.append(({}, totals))
    for gateway in result.iter():
        if _local_name(gateway.tag).lower() != "gateway":
            continue
        for entry in gateway.findall("./entry"):
            name = (entry.findtext("name") or entry.attrib.get("name") or "").strip()
            if not name:
                continue
            fields = {}
            for destination, source in (("current_users", "CurrentUsers"), ("previous_users", "PreviousUsers")):
                value = _number(_entry_text(entry, source))
                if value is not None:
                    fields[destination] = int(value)
            if fields:
                points.append(({"gateway": name[:120]}, fields))
    return points


SOFTWARE_STATE_PATTERN = re.compile(
    r"(?i)^(running|active|inactive|stopped|exited|failed|dead|down|up|starting|stopping|"
    r"disabled|crashed|not\s+running)\b"
)


def parse_software_status(result: ET.Element, limit: int = 256) -> list[tuple[dict, dict]]:
    """Parse ``show system software status`` process lines.

    Accepts ``Process devsrvr   running   (pid: 1234)``, ``Process: devsrvr
    (pid: 1234)  running`` and ``mgmtsrvr: running``.  When a process is listed
    several times (chassis slots), it is reported running only if every
    instance runs.
    """
    processes: dict[str, dict] = {}
    labelled = re.compile(r"(?i)^\s*process:?\s+(\S+)\s*(.*)$")
    short = re.compile(r"^\s*([A-Za-z][\w.-]*)\s*:\s*(.+?)\s*$")
    for line in _result_text(result).splitlines():
        match = labelled.match(line)
        if match:
            process, rest = match.group(1).rstrip(":"), match.group(2)
        else:
            match = short.match(line)
            if not match:
                continue
            process, rest = match.groups()
        state_text = re.sub(r"\([^)]*\)", " ", rest).strip()
        state = SOFTWARE_STATE_PATTERN.match(state_text)
        if not state:
            continue
        status = re.sub(r"\s+", " ", state.group(1))
        running = status.lower() in {"running", "active"}
        process = process[:80]
        existing = processes.get(process)
        if existing is None:
            if len(processes) >= limit:
                continue
            processes[process] = {"running": running, "status": status}
        elif existing["running"] and not running:
            processes[process] = {"running": running, "status": status}
    return [({"process": process}, fields) for process, fields in sorted(processes.items())]


RAID_HEALTHY_PATTERN = re.compile(r"(?i)^(ok|online|active|clean|optimal|present|available)")


def parse_raid(result: ET.Element) -> list[tuple[dict, dict]]:
    """Parse ``show system raid detail`` text (or a simple XML entry form).

    Text lines such as ``Disk Pair A   Available``, ``  Disk id A1   Present``,
    ``Disk1: OK`` and a ``Status clean`` line (array state, reported as
    ``array`` or ``disk_pair_<x>_array`` inside a pair) are recognised.
    """
    disks: dict[str, str] = {}
    for entry in result.iter():
        if _local_name(entry.tag).lower() != "entry":
            continue
        name = (entry.attrib.get("name") or _entry_text(entry, "name", "disk")).strip()
        status = _entry_text(entry, "status", "state")
        if name and status:
            disks[_field_name(name)[:64] or name] = status
    if not disks:
        disk_line = re.compile(
            r"(?i)^\s*disk\s*(pair\s+|id\s+)?([a-z]?\d+[a-z]?|[a-z])\b\s*:?\s+(\S.*?)\s*$"
        )
        status_line = re.compile(r"(?i)^\s*(?:array\s+)?status\s*:?\s+(\S.*?)\s*$")
        pair = None
        # A Status line belongs to the array (or pair) only before the first
        # member disk line; per-disk detail lines are ignored.
        array_context = True
        for line in _result_text(result).splitlines():
            match = disk_line.match(line)
            if match:
                kind, token, status = match.groups()
                token = token.lower()
                if kind and kind.lower().startswith("pair"):
                    name = f"disk_pair_{token}"
                    pair = name
                    array_context = True
                else:
                    name = f"disk{token}" if token.isdigit() else f"disk_{token}"
                    array_context = False
                disks[name] = re.sub(r"\s+", " ", status)
                continue
            match = status_line.match(line)
            if match and array_context:
                disks[f"{pair}_array" if pair else "array"] = re.sub(r"\s+", " ", match.group(1))
    return [
        ({"disk": disk}, {"status": status, "healthy": bool(RAID_HEALTHY_PATTERN.match(status))})
        for disk, status in sorted(disks.items())
    ]


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


def _is_unsupported(exc: Exception, category: str | None = None) -> bool:
    if not isinstance(exc, ApiError):
        return False
    if UNSUPPORTED_PATTERN.search(str(exc)):
        return True
    extra = OPTIONAL_DISABLE_PATTERNS.get(category or "")
    return bool(extra and extra.search(str(exc)))


def _collect_counters(config: dict) -> list[tuple[dict, dict]]:
    """Merge severity=drop counters with DoS-aspect counters under one limit."""
    points = _global_counter_points(request_xml(config, COUNTER_COMMAND))
    unsupported = config.setdefault("_unsupported", set())
    if "counters_dos" not in unsupported:
        try:
            points.extend(_global_counter_points(request_xml(config, COUNTER_DOS_COMMAND)))
        except ApiError as exc:
            if not _is_unsupported(exc):
                raise
            unsupported.add("counters_dos")
            print(
                f"paloalto-api [{config['hostname']}] counters_dos: disabled, not supported: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return rank_global_counters(points, int(config.get("counter_limit", 256)))


# Fields of a VSYS-scoped ``show session info`` that are specific to that VSYS.
# ``sessions_active`` stays the dataplane-summed value of ``show session meter``
# and ``sessions_max`` stays the VSYS session limit of the meter (``num-max``
# in the scoped output is the platform limit).
VSYS_SESSION_FIELDS = ("cps", "packet_rate_pps", "sessions_tcp", "sessions_udp", "sessions_icmp")


def _collect_vsys(config: dict) -> list[tuple[dict, dict]]:
    """Per-VSYS sessions from ``show session meter`` plus CPS, packet rate and
    protocol counts from ``show session info`` scoped to each VSYS.

    Unconfigured meter slots (PAN-OS answers "You must specify a valid vsys")
    are dropped and probed again only every ``system_interval`` seconds.
    """
    points = parse_session_meter(request_xml(config, SESSION_METER_COMMAND))
    unsupported = config.setdefault("_unsupported", set())
    missing = config.setdefault("_missing_vsys", {})
    recheck = float(config.get("system_interval", 3600))
    now = time.monotonic()
    result = []
    for tags, fields in points:
        vsys = tags["vsys"]
        probed = missing.get(vsys)
        if probed is not None and now - probed < recheck:
            continue
        if "vsys_sessions" not in unsupported:
            try:
                scoped = parse_sessions(request_xml(config, SESSION_COMMAND, vsys=vsys))
            except ApiError as exc:
                if INVALID_VSYS_PATTERN.search(str(exc)):
                    missing[vsys] = now
                    continue
                if _is_unsupported(exc):
                    unsupported.add("vsys_sessions")
                    print(
                        f"paloalto-api [{config['hostname']}] vsys_sessions: disabled, not supported: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(f"paloalto-api [{config['hostname']}] vsys_sessions {vsys}: {exc}", file=sys.stderr, flush=True)
            else:
                fields.update({name: scoped[name] for name in VSYS_SESSION_FIELDS if name in scoped})
        missing.pop(vsys, None)
        result.append((tags, fields))
    return result


def collect_firewall(config: dict, due: set[str]) -> list[str]:
    hostname = config["hostname"]
    output = []
    commands = {
        "sessions": (SESSION_COMMAND, parse_sessions, "paloalto_api_sessions"),
        "interface_status": (INTERFACE_STATUS_COMMAND, parse_interface_status, "paloalto_api_interfaces"),
        "ha": (HA_COMMAND, parse_ha_state, "paloalto_api_ha"),
        "storage": (STORAGE_COMMAND, parse_storage, "paloalto_api_storage"),
        "thermal": (THERMAL_COMMAND, lambda result: parse_environmentals(result, "thermal"), "paloalto_api_sensors"),
        "fans": (FAN_COMMAND, lambda result: parse_environmentals(result, "fan"), "paloalto_api_sensors"),
        "power": (POWER_COMMAND, lambda result: parse_environmentals(result, "power"), "paloalto_api_sensors"),
        "chassis_inventory": (
            CHASSIS_INVENTORY_COMMAND,
            parse_chassis_inventory,
            "paloalto_api_chassis_inventory",
        ),
        "chassis_status": (
            CHASSIS_STATUS_COMMAND,
            parse_chassis_status,
            "paloalto_api_chassis_status",
        ),
        "chassis_power": (
            CHASSIS_POWER_COMMAND,
            parse_chassis_power,
            "paloalto_api_chassis_power",
        ),
        "system": (SYSTEM_INFO_COMMAND, parse_system_info, "paloalto_api_system"),
        "logging": (LOGGING_COMMAND, parse_log_receiver, "paloalto_api_logging"),
        "globalprotect": (GLOBALPROTECT_COMMAND, parse_globalprotect, "paloalto_api_globalprotect"),
        "software": (SOFTWARE_COMMAND, parse_software_status, "paloalto_api_software"),
        "raid": (RAID_COMMAND, parse_raid, "paloalto_api_raid"),
    }
    # interface_status runs before interfaces so logical counters get zone/VSYS
    # tags from the first polling cycle; system runs first so chassis/high-end
    # gating is known, and dataplane runs before ingress_backlogs so the DP
    # names are known for zero-filling.
    categories = (
        "system",
        "sessions",
        "vsys",
        "interface_status",
        "interfaces",
        "management",
        "dataplane",
        "ingress_backlogs",
        "counters",
        "ha",
        "storage",
        "thermal",
        "fans",
        "power",
        "logging",
        "globalprotect",
        "software",
        "raid",
        "chassis_inventory",
        "chassis_status",
        "chassis_power",
    )
    unsupported = config.setdefault("_unsupported", set())
    for category in categories:
        if category not in due or category in unsupported:
            continue
        if category.startswith("chassis_") and not config.get("_is_chassis", False):
            continue
        if category == "raid" and not (config.get("_is_chassis") or config.get("_is_highend")):
            continue
        try:
            if category == "interfaces":
                result = request_xml(config, INTERFACE_COMMAND)
                parsed_sets = (
                    ("paloalto_api_interfaces", parse_interface_counters(result)),
                    (
                        "paloalto_api_logical_interfaces",
                        parse_logical_interface_counters(result, config.get("_interface_context")),
                    ),
                )
            elif category == "counters":
                parsed_sets = (("paloalto_api_counters", _collect_counters(config)),)
            elif category == "vsys":
                parsed_sets = (("paloalto_api_vsys", _collect_vsys(config)),)
            elif category == "management":
                result = request_xml(config, MANAGEMENT_COMMAND)
                parsed_sets = (
                    ("paloalto_api_management", [({}, parse_management_resources(result))]),
                    ("paloalto_api_processes", parse_management_processes(result)),
                )
            elif category == "dataplane":
                result = request_xml(config, DATAPLANE_COMMAND)
                cpu_points = parse_dataplane_resources(result)
                utilization_points = parse_dataplane_utilization(result)
                dataplanes = sorted({tags["dataplane"] for tags, _ in cpu_points + utilization_points})
                if dataplanes:
                    config["_dataplanes"] = dataplanes
                parsed_sets = (
                    ("paloalto_api_dataplane_cpu", cpu_points),
                    ("paloalto_api_dataplane_resources", utilization_points),
                )
            elif category == "ingress_backlogs":
                result = request_xml(config, INGRESS_BACKLOGS_COMMAND)
                parsed_sets = (
                    (
                        "paloalto_api_ingress_backlogs",
                        parse_ingress_backlogs(result, config.get("_dataplanes", ())),
                    ),
                )
            else:
                command, parser, measurement = commands[category]
                parsed = parser(request_xml(config, command))
                if category == "system":
                    model = str(parsed.get("model", ""))
                    config["_is_chassis"] = bool(
                        re.search(r"(^|[^0-9])(5450|7050|7080|7500)([^0-9]|$)", model)
                    )
                    # High-end platforms with a RAID disk pair (PA-5200/5400/
                    # 5500/7000/7500 Series).
                    config["_is_highend"] = bool(
                        re.search(r"(?i)(?:^|[^0-9])(?:52|54|55|70|75)\d\d(?:[^0-9]|$)", model)
                    )
                elif category == "interface_status":
                    config["_interface_context"] = interface_context(parsed)
                parsed_sets = ((measurement, parsed if isinstance(parsed, list) else [({}, parsed)]),)
            for measurement, points in parsed_sets:
                for extra_tags, fields in points:
                    line = line_protocol(measurement, {"hostname": hostname, **extra_tags}, fields)
                    if line:
                        output.append(line)
        except Exception as exc:  # keep other categories and firewalls alive
            if category in OPTIONAL_CATEGORIES and _is_unsupported(exc, category):
                # Avoid logging the same unsupported command on every poll.
                unsupported.add(category)
                print(f"paloalto-api [{hostname}] {category}: disabled, not supported: {exc}", file=sys.stderr, flush=True)
                continue
            print(f"paloalto-api [{hostname}] {category}: {exc}", file=sys.stderr, flush=True)
    return output


def _every(key: str, default: int):
    return lambda cfg: int(cfg.get(key, default))


# Single source of truth for the polling interval of every category (seconds).
# ``dataplane`` reads the last completed one-minute resource-monitor bucket, so
# a ``resource_interval`` below 60 s re-reads the same minute.
CATEGORY_SCHEDULES = {
    "sessions": _every("interval", 20),
    "interfaces": _every("interval", 20),
    "vsys": _every("resource_interval", 60),
    "interface_status": _every("resource_interval", 60),
    "management": _every("resource_interval", 60),
    "dataplane": _every("resource_interval", 60),
    "ingress_backlogs": _every("resource_interval", 60),
    "counters": _every("counter_interval", 60),
    "ha": _every("resource_interval", 60),
    "thermal": _every("resource_interval", 60),
    "fans": _every("resource_interval", 60),
    "power": _every("resource_interval", 60),
    "logging": _every("resource_interval", 60),
    "globalprotect": _every("resource_interval", 60),
    "software": _every("resource_interval", 60),
    "raid": _every("system_interval", 3600),
    "storage": _every("system_interval", 3600),
    "system": _every("system_interval", 3600),
    "chassis_inventory": _every("system_interval", 3600),
    "chassis_status": _every("resource_interval", 60),
    "chassis_power": _every("resource_interval", 60),
}


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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            # Current generator format: double quotes, with backslash, double
            # quote and dollar sign each prefixed by one backslash (the same
            # rules Docker Compose applies to env_file values).
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            # Legacy single-quoted format written by generators before the
            # double-quote change; kept so old runtime files still load.
            encoded = value[1:-1]
            decoded = []
            index = 0
            while index < len(encoded):
                if encoded[index] == "\\" and index + 1 < len(encoded) and encoded[index + 1] in {"\\", "'"}:
                    index += 1
                decoded.append(encoded[index])
                index += 1
            value = "".join(decoded)
        os.environ.setdefault(name, value)


def run_once(configs: list[dict], categories: set[str] | None = None) -> int:
    selected = categories or set(CATEGORY_SCHEDULES)
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
    schedules = CATEGORY_SCHEDULES
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
