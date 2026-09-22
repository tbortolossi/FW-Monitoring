#!/usr/bin/env python3
"""Build the provisioned Palo Alto XML API Grafana dashboards.

Both dashboards share the same overview and troubleshooting sections so the
compact and chassis views keep feature parity with the SNMP dashboards. The
chassis dashboard adds slot inventory, live slot state and chassis power.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "grafana/provisioning/dashboards/Palo_API_Dashboard.json"
CHASSIS_OUTPUT = ROOT / "grafana/provisioning/dashboards/Palo_API_Chassis_Dashboard.json"
DATASOURCE = {"type": "influxdb", "uid": "P951FEA4DE68E13C5"}

# Physical front-panel ports only, so internal, VLAN, loopback, tunnel and
# subinterface counters are not double-counted in global throughput.
PHYSICAL = 'exists r.interface and r.interface =~ /(?i)^ethernet/ and r.interface !~ /\\./'
# Chassis panel IDs are offset so shared sections never collide with the
# dedicated chassis panels.
CHASSIS_ID_OFFSET = 10000


def target(query: str, ref_id: str = "A") -> dict:
    return {"datasource": DATASOURCE, "query": query.strip(), "refId": ref_id}


def thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"mode": "absolute", "steps": [{"color": color, "value": value} for color, value in steps]}


LOAD_THRESHOLDS = thresholds(("green", None), ("#EAB839", 70), ("red", 90))


def timeseries(panel_id: int, title: str, query: str, x: int, y: int, w: int, h: int, unit: str, description: str = "") -> dict:
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "fillOpacity": 8,
                    "spanNulls": False,
                    "showPoints": "never",
                },
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "legend": {"calcs": ["lastNotNull", "mean", "max"], "displayMode": "table", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "targets": [target(query)],
        "title": title,
        "type": "timeseries",
        "description": description,
    }


def percent_range(panel: dict) -> dict:
    panel["fieldConfig"]["defaults"].update(min=0, max=100)
    return panel


def stat(panel_id: int, title: str, query: str, x: int, y: int, w: int, unit: str, decimals: int | None = None) -> dict:
    defaults = {"unit": unit, "custom": {}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "wideLayout": True,
        },
        "targets": [target(query)],
        "title": title,
        "type": "stat",
    }


def kpi(panel_id: int, title: str, query: str, x: int, y: int, unit: str, *, levels: dict | None = None, description: str = "") -> dict:
    """Colored current-value tile for the at-a-glance load strip."""
    panel = stat(panel_id, title, query, x, y, 3, unit, 1 if unit == "percent" else None)
    panel["fieldConfig"]["defaults"]["thresholds"] = levels or thresholds(("text", None))
    panel["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    panel["options"]["colorMode"] = "background" if levels else "none"
    panel["description"] = description
    return panel


def text_value(panel_id: int, title: str, variable: str, x: int, y: int, w: int) -> dict:
    return {
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "mode": "html",
            "content": (
                '<div style="height:100%;display:flex;align-items:center;justify-content:center;'
                'text-align:center;padding:2px 10px;overflow:hidden;"><div style="font-size:'
                f'clamp(20px,1.85vw,38px);line-height:1.08;font-weight:600;overflow-wrap:anywhere;">${{{variable}:text}}</div></div>'
            ),
        },
        "title": title,
        "type": "text",
    }


def table(panel_id: int, title: str, query: str, x: int, y: int, w: int, h: int, description: str = "") -> dict:
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}}, "overrides": []},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        "targets": [target(query)],
        "title": title,
        "type": "table",
        "description": description,
    }


def override(name: str, **properties) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": key.replace("__", "."), "value": value} for key, value in properties.items()],
    }


def state_timeline(panel_id: int, title: str, query: str, y: int) -> dict:
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {"defaults": {"custom": {"fillOpacity": 70, "lineWidth": 0}, "color": {"mode": "thresholds"}}, "overrides": []},
        "gridPos": {"h": 5, "w": 24, "x": 0, "y": y},
        "id": panel_id,
        "options": {"alignValue": "left", "mergeValues": True, "rowHeight": 0.9, "showValue": "auto", "tooltip": {"mode": "single"}},
        "targets": [target(query)],
        "title": title,
        "type": "state-timeline",
    }


def status_history(panel_id: int, title: str, query: str, x: int, y: int, w: int, h: int, description: str = "") -> dict:
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {
            "defaults": {
                "unit": "percent",
                "min": 0,
                "max": 100,
                "decimals": 0,
                "color": {"mode": "continuous-GrYlRd"},
                "custom": {"fillOpacity": 85, "lineWidth": 0},
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {"rowHeight": 0.9, "showValue": "never", "colWidth": 0.95, "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        "targets": [target(query)],
        "title": title,
        "type": "status-history",
        "description": description,
    }


def row(panel_id: int, title: str, y: int, panels: list[dict], *, repeat: str | None = None) -> dict:
    result = {
        "collapsed": True,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": panel_id,
        "panels": panels,
        "title": title,
        "type": "row",
    }
    if repeat:
        result["repeat"] = repeat
        result["repeatDirection"] = "v"
    return result


def repeated_panel(panel: dict, variable_name: str, *, max_per_row: int) -> dict:
    panel["repeat"] = variable_name
    panel["repeatDirection"] = "h"
    panel["maxPerRow"] = max_per_row
    return panel


def variable(name: str, label: str, query: str, *, hidden: bool = False, multi: bool = False, include_all: bool = False) -> dict:
    item = {
        "current": {},
        "datasource": DATASOURCE,
        "definition": query.strip(),
        "hide": 2 if hidden else 0,
        "includeAll": include_all,
        "label": label,
        "multi": multi,
        "name": name,
        "options": [],
        "query": query.strip(),
        "refresh": 2 if hidden or name != "hostname" else 1,
        "regex": "",
        "skipUrlSync": hidden,
        "sort": 1,
        "type": "query",
    }
    if include_all:
        item["current"] = {"selected": True, "text": "All", "value": "$__all"}
    return item


def stack_rows(panels: list[dict]) -> list[dict]:
    """Place collapsed rows after the overview and keep their panels in order."""
    cursor = max(
        (panel["gridPos"]["y"] + panel["gridPos"]["h"] for panel in panels if panel["type"] != "row"),
        default=0,
    )
    for panel in panels:
        if panel["type"] != "row":
            continue
        panel["gridPos"]["y"] = cursor
        inner = panel["panels"]
        base = min((child["gridPos"]["y"] for child in inner), default=0)
        for child in inner:
            child["gridPos"]["y"] = cursor + 1 + child["gridPos"]["y"] - base
        cursor += 1
    return panels


def offset_ids(panels: list[dict], offset: int) -> list[dict]:
    for panel in panels:
        panel["id"] += offset
        offset_ids(panel.get("panels", []), offset)
    return panels


# ---------------------------------------------------------------------------
# Shared sections
# ---------------------------------------------------------------------------

def header_panels() -> list[dict]:
    return [
        stat(1001, "Uptime", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_system" and r.hostname == "${hostname}" and r._field == "uptime_seconds")
  |> group()
  |> last()
  |> map(fn: (r) => ({ _time: r._time, _field: "Uptime", _value: float(v: r._value) / 86400.0 }))
''', 0, 0, 4, "suffix: days", 1),
        text_value(1002, "Version", "info_version", 4, 0, 6),
        text_value(1003, "Platform", "info_platform", 10, 0, 6),
        text_value(1004, "HA Status", "info_ha", 16, 0, 8),
    ]


def current_value(measurement: str, field: str, label: str, extra: str = "") -> str:
    return f'''
from(bucket: "firewalls")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.hostname == "${{hostname}}" and r._field == "{field}"{extra})
  |> last()
  |> group()
  |> max()
  |> map(fn: (r) => ({{ _time: now(), _field: "{label}", _value: float(v: r._value) }}))
'''


def kpi_panels(y: int) -> list[dict]:
    return [
        kpi(3001, "DP CPU (avg)", current_value("paloalto_api_dataplane_cpu", "cpu_pct", "DP CPU", ' and r.core == "average"'), 0, y, "percent",
            levels=LOAD_THRESHOLDS, description="Highest per-dataplane average across all cores. This is the value SNMP reports."),
        kpi(3002, "Hottest DP Core", current_value("paloalto_api_dataplane_cpu", "cpu_pct", "Hottest core", ' and r.core != "average"'), 3, y, "percent",
            levels=LOAD_THRESHOLDS, description="Busiest individual dataplane core. A high value with a lower average reveals imbalance or saturated cores hidden by the average."),
        kpi(3003, "MP CPU", current_value("paloalto_api_management", "mp_cpu_pct", "MP CPU"), 6, y, "percent", levels=LOAD_THRESHOLDS),
        kpi(3004, "MP RAM", current_value("paloalto_api_management", "memory_used_pct", "MP RAM"), 9, y, "percent",
            levels=thresholds(("green", None), ("#EAB839", 85), ("red", 95))),
        kpi(3005, "Active Sessions", current_value("paloalto_api_sessions", "sessions_active", "Sessions"), 12, y, "short"),
        kpi(3006, "Session Table", current_value("paloalto_api_sessions", "session_utilization_pct", "Session table"), 15, y, "percent",
            levels=thresholds(("green", None), ("#EAB839", 80), ("red", 90))),
        kpi(3007, "CPS", current_value("paloalto_api_sessions", "cps", "CPS"), 18, y, "cps"),
        kpi(3008, "Throughput (In + Out)", f'''
from(bucket: "firewalls")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> last()
  |> group()
  |> sum()
  |> map(fn: (r) => ({{ _time: now(), _field: "Throughput", _value: r._value * 8.0 }}))
''', 21, y, "bps", description="Sum of physical Ethernet In and Out rates from hardware octet counters."),
    ]


def overview_panels() -> list[dict]:
    interface_load = table(3010, "Interface Load (last 5 minutes)", f'''
speed = from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field == "speed_mbps")
  |> last()
  |> map(fn: (r) => ({{ interface: r.interface, speed_bps: float(v: r._value) * 1000000.0 }}))
  |> group()
rates = from(bucket: "firewalls")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> mean()
  |> map(fn: (r) => ({{ interface: r.interface, _field: if r._field == "in_octets" then "in_bps" else "out_bps", _value: r._value * 8.0 }}))
  |> group()
  |> pivot(rowKey: ["interface"], columnKey: ["_field"], valueColumn: "_value")
join(tables: {{rate: rates, link: speed}}, on: ["interface"])
  |> filter(fn: (r) => r.speed_bps > 0.0)
  |> map(fn: (r) => ({{
    interface: r.interface,
    speed_bps: r.speed_bps,
    in_bps: r.in_bps,
    out_bps: r.out_bps,
    in_pct: r.in_bps / r.speed_bps * 100.0,
    out_pct: r.out_bps / r.speed_bps * 100.0,
    peak_pct: if r.in_bps > r.out_bps then r.in_bps / r.speed_bps * 100.0 else r.out_bps / r.speed_bps * 100.0,
  }}))
  |> sort(columns: ["peak_pct"], desc: true)
''', 14, 25, 10, 10, "Current link utilization of every physical port with a negotiated speed, busiest first. Calculated from hardware octet counters and the speed reported by show interface all.")
    gauge = {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}
    interface_load["fieldConfig"]["overrides"] = [
        override("interface", displayName="Interface"),
        override("speed_bps", displayName="Speed", unit="bps"),
        override("in_bps", displayName="In", unit="bps", decimals=1),
        override("out_bps", displayName="Out", unit="bps", decimals=1),
        override("in_pct", displayName="In %", unit="percent", decimals=1, min=0, max=100, custom__cellOptions=gauge, thresholds=LOAD_THRESHOLDS, color={"mode": "thresholds"}),
        override("out_pct", displayName="Out %", unit="percent", decimals=1, min=0, max=100, custom__cellOptions=gauge, thresholds=LOAD_THRESHOLDS, color={"mode": "thresholds"}),
        override("peak_pct", custom__hidden=True),
    ]
    return [
        percent_range(timeseries(2, "CPU MP / DP", '''
management = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "mp_cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Management plane" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
averages = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct" and r.core == "average")
  |> map(fn: (r) => ({ r with _field: r.dataplane + " average" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
hottest = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct" and r.core != "average")
  |> map(fn: (r) => ({ r with _field: r.dataplane + " hottest core" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
union(tables: [management, averages, hottest])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 8, 12, 10, "percent", "Management-plane CPU, the all-core average of every dataplane (equivalent to SNMP) and the busiest core of every dataplane. Per-core curves are in the repeated Dataplane rows.")),
        percent_range(timeseries(3, "MP RAM Usage", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "memory_used_pct")
  |> map(fn: (r) => ({ r with _field: "MP RAM" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 8, 12, 10, "percent")),
        timeseries(4, "Sessions", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and (r._field == "sessions_active" or r._field == "sessions_max"))
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_active" then "Active" else "Maximum" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 18, 8, 7, "short"),
        timeseries(5, "Global CPS", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "cps")
  |> map(fn: (r) => ({ r with _field: "CPS" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 8, 18, 8, 7, "cps"),
        percent_range(timeseries(6, "Session Utilization", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "session_utilization_pct")
  |> map(fn: (r) => ({ r with _field: "Session utilization" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 16, 18, 8, 7, "percent")),
        timeseries(7, "Throughput Global Interfaces", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _value: r._value * 8.0, _field: if r._field == "in_octets" then "In" else "Out" }}))
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
''', 0, 25, 14, 10, "bps", "Total throughput calculated from PAN-OS hardware interface octet counters."),
        interface_load,
        percent_range(timeseries(3011, "Dataplane Resource Pressure", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r._field == "utilization_pct")
  |> map(fn: (r) => ({ r with _field: r.resource }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 35, 12, 8, "percent", "Worst dataplane for each resource-monitor resource: session table, packet buffers, packet descriptors and software tags. Buffer or descriptor pressure precedes packet drops.")),
        timeseries(3012, "Global Drop Rate by Category", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.severity == "drop")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: r.category }))
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
''', 12, 35, 12, 8, "pps", "Packets dropped per second by the dataplane, summed by PAN-OS counter category. Details are in the drop counter sections."),
    ]


def ha_row() -> dict:
    timeline = state_timeline(1006, "HA Role Changes", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${hostname}" and (r._field == "state" or r._field == "peer_state"))
  |> map(fn: (r) => ({ r with _field: if r._field == "state" then "Local" else "Peer" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0)
    timeline["gridPos"]["h"] = 6
    timeline["fieldConfig"]["defaults"]["mappings"] = [
        {"type": "regex", "options": {"pattern": "(?i)^active.*", "result": {"color": "green", "index": 0}}},
        {"type": "regex", "options": {"pattern": "(?i)^passive$", "result": {"color": "blue", "index": 1}}},
        {"type": "regex", "options": {"pattern": "(?i)^standalone$", "result": {"color": "text", "index": 2}}},
        {"type": "regex", "options": {"pattern": "(?i)^(non-functional|suspended|tentative|initial|unknown)$", "result": {"color": "red", "index": 3}}},
    ]
    return row(9006, "HA Role Changes", 0, [
        timeline,
        table(1007, "HA Synchronization", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${hostname}")
  |> last()
  |> map(fn: (r) => ({ _field: r._field, _value: string(v: r._value), row: "HA" }))
  |> group()
  |> pivot(rowKey: ["row"], columnKey: ["_field"], valueColumn: "_value")
  |> drop(columns: ["row"])
''', 0, 6, 24, 4, "Local and peer state, peer connection, running-configuration and session-state synchronization from show high-availability state."),
    ])


def interface_rows() -> list[dict]:
    return [
        row(9001, "Interfaces", 0, [
            repeated_panel(timeseries(8, "Throughput ${interface}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface == "${interface}" and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({ r with _value: r._value * 8.0, _field: if r._field == "in_octets" then "In" else "Out" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 12, 7, "bps", "Repeated automatically for every active physical Ethernet interface returned by the XML API."), "interface", max_per_row=2),
        ]),
        row(9008, "API Interface Details", 0, [
            timeseries(9, "Packets per Second by Interface", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface =~ /^${interface:regex}$/ and r._field =~ /^(in|out)_packets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: r.interface + (if r._field == "in_packets" then " In" else " Out") }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 12, 10, "pps"),
            table(10, "Interface Status", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r._field =~ /^(state|speed_mbps|duplex|mode|zone|vsys|forwarding|link_down_count)$/)
  |> map(fn: (r) => ({ r with _value: string(v: r._value) }))
  |> group(columns: ["interface", "_field"])
  |> last()
  |> group()
  |> pivot(rowKey: ["interface"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["interface", "state", "speed_mbps", "duplex", "mode", "zone", "vsys", "forwarding", "link_down_count"])
  |> sort(columns: ["interface"])
''', 12, 0, 12, 10, "Operational state, negotiated speed, duplex, mode, zone, VSYS and forwarding instance from show interface all, plus the cumulative link-down count."),
        ]),
        row(9004, "Interface Errors / Discards", 0, [
            repeated_panel(timeseries(11, "Errors / Discards ${interface}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface == "${interface}" and r._field =~ /^(in_errors|in_discards|out_errors)$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: if r._field == "in_errors" then "In Errors" else if r._field == "in_discards" then "In Discards" else "Out Errors" }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 10, "ops", "Hardware ingress errors and discards plus MAC-level transmit errors from show counter interface all."), "interface", max_per_row=1),
        ]),
    ]


def zone_rows() -> list[dict]:
    logical = 'r._measurement == "paloalto_api_logical_interfaces" and r.hostname == "${hostname}"'
    zone_throughput = f'''
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _value: r._value * 8.0, _field: r.zone + (if r._field == "in_octets" then " In" else " Out") }}))
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
'''
    return [
        row(9009, "VSYS ${vsys}", 0, [
            timeseries(3101, "VSYS Sessions", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_vsys" and r.hostname == "${hostname}" and r.vsys == "${vsys}" and r._field =~ /^sessions_(active|max)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_active" then "Active" else "VSYS limit" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 8, 8, "short", "Sessions per VSYS summed across dataplanes from show session meter. The limit appears when a VSYS session resource limit is configured."),
            timeseries(3102, "VSYS Throughput by Zone", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and r.vsys == "${{vsys}}" and exists r.zone and r._field =~ /^(in|out)_octets$/)
{zone_throughput}''', 8, 0, 16, 8, "bps", "Logical interface octet counters of this VSYS summed by zone. In is traffic received from the zone."),
        ], repeat="vsys"),
        row(9010, "Zones, Logical Interfaces and Drop Reasons", 0, [
            timeseries(3111, "Throughput by Zone", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and exists r.zone and r._field =~ /^(in|out)_octets$/)
{zone_throughput}''', 0, 0, 12, 10, "bps", "Logical interface octet counters summed by zone. In is traffic received from the zone."),
            timeseries(3114, "Logical Drops by Reason", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and r._field =~ /^drop_/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
''', 12, 0, 12, 10, "pps", "Per-reason logical interface drops: no route, no ARP/neighbor, flow state, zone change, spoofing, LAND, ping of death, teardrop and ICMP fragments."),
            timeseries(3113, "Subinterface / Tunnel / VLAN Throughput", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and r.interface =~ /\\.|^(tunnel|vlan|ae)/ and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _value: r._value * 8.0, _field: r.interface + (if r._field == "in_octets" then " In" else " Out") }}))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 10, 24, 10, "bps", "Logical interface counters for subinterfaces, IPsec/GRE tunnels, VLAN and aggregate interfaces."),
            table(3115, "Logical Drops by Interface (selected range)", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and r._field =~ /^(drop_.+|in_errors|in_discards)$/)
  |> difference(nonNegative: true)
  |> sum()
  |> filter(fn: (r) => r._value > 0)
  |> map(fn: (r) => ({{ interface: r.interface, reason: r._field, drops: r._value }}))
  |> group()
  |> sort(columns: ["drops"], desc: true)
''', 0, 20, 24, 9, "Packets dropped during the selected time range, by logical interface and reason."),
        ]),
    ]


def dataplane_row() -> dict:
    return row(9002, "Dataplane ${dataplane}", 0, [
        percent_range(timeseries(12, "CPU per Core - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r.core != "average" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Core " + r.core }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 12, 10, "percent", "One repeated row is created for every dataplane returned by PAN-OS.")),
        percent_range(timeseries(13, "Resource Utilization - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r._field == "utilization_pct")
  |> map(fn: (r) => ({ r with _field: r.resource }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 0, 12, 10, "percent", "Session, packet-buffer, packet-descriptor and software-tag pressure reported by resource-monitor.")),
        percent_range(timeseries(3201, "CPU Summary - ${dataplane}", '''
cores = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r._field == "cpu_pct")
average = cores
  |> filter(fn: (r) => r.core == "average")
  |> map(fn: (r) => ({ r with _field: "Average (all cores)" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
active = cores
  |> filter(fn: (r) => r.core != "average" and r._value > 0.0)
  |> map(fn: (r) => ({ r with _field: "Average (active cores)" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
hottest = cores
  |> filter(fn: (r) => r.core != "average")
  |> map(fn: (r) => ({ r with _field: "Hottest core" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
union(tables: [average, active, hottest])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 10, 12, 12, "percent", "The all-core average matches SNMP. Cores reporting 0% (not used for packet processing) are excluded from the active-core average, which shows the real load of the packet-processing cores.")),
        status_history(3202, "Core Load Map - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r.core != "average" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Core " + (if strings.strlen(v: r.core) == 1 then "00" else if strings.strlen(v: r.core) == 2 then "0" else "") + r.core }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 10, 12, 12, "One line per core colored by load, readable even on 64+ core dataplanes."),
    ], repeat="dataplane")


def session_row() -> dict:
    return row(9007, "API Session Details", 0, [
        timeseries(14, "Sessions by Protocol", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field =~ /^sessions_(tcp|udp|icmp)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_tcp" then "TCP" else if r._field == "sessions_udp" then "UDP" else "ICMP" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 0, 12, 9, "short"),
        timeseries(15, "Packet Rate", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "packet_rate_pps")
  |> map(fn: (r) => ({ r with _field: "Packet rate" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 0, 12, 9, "pps"),
    ])


def counter_rate(panel_id: int, title: str, pattern: str, x: int, y: int, w: int, description: str) -> dict:
    return timeseries(panel_id, title, f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${{hostname}}" and r._field == "value" and r.counter =~ /{pattern}/)
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({{ r with _field: r.counter }}))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', x, y, w, 9, "pps", description)


def counter_rows() -> list[dict]:
    return [
        row(9003, "Data Plane Pressure and Key Drops", 0, [
            counter_rate(3301, "Policy Deny / DoS Drop Rate", "^(flow_policy_deny|flow_dos_(rule_(drop|deny).*|drop_ip_blocked|pbp_drop|(ag|cl)_max_sess_limit))$", 0, 0, 12,
                         "Policy denies and DoS protection policy drops, equivalent to the SNMP slowpath / DoS drop panel."),
            counter_rate(3302, "Zone Protection RED / Flood Drops", "^flow_dos_(red_|pf_|syn|udp|icmp|ip_)", 12, 0, 12,
                         "Zone protection flood, random early drop and packet-based attack drops."),
            counter_rate(3303, "SYN Cookie Counters", "syncookie", 0, 9, 12,
                         "SYN cookie activity, collected through the DoS aspect filter because these counters are not all drops."),
            timeseries(3304, "DoS Block Table Entries", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.counter =~ /^flow_dos_blk_/)
  |> map(fn: (r) => ({ r with _field: r.counter }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 9, 12, 9, "short", "Current hardware and software DoS block-table entries. These counters are gauges, not rates."),
        ]),
        row(9011, "Filtered Global Drop Counters", 0, [
            timeseries(16, "Selected Drop / Failure Counters", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.category =~ /^${counter_category:regex}$/ and r.aspect =~ /^${counter_aspect:regex}$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({ r with _field: r.counter }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 12, "ops", "PAN-OS severity=drop and DoS-aspect counters; use the Category and Aspect selectors above. Cardinality is bounded by counter_limit."),
            table(3311, "Top Counters (selected range)", '''
descriptions = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "description")
  |> last()
  |> group()
  |> keep(columns: ["counter", "_value"])
  |> rename(columns: {_value: "description"})
increases = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.category =~ /^${counter_category:regex}$/ and r.aspect =~ /^${counter_aspect:regex}$/)
  |> difference(nonNegative: true)
  |> sum()
  |> filter(fn: (r) => r._value > 0)
  |> group()
  |> keep(columns: ["counter", "severity", "category", "aspect", "_value"])
  |> rename(columns: {_value: "packets"})
join(tables: {i: increases, d: descriptions}, on: ["counter"])
  |> sort(columns: ["packets"], desc: true)
  |> limit(n: 30)
''', 0, 12, 24, 10, "Counters that increased the most during the selected range, with the PAN-OS description."),
        ]),
    ]


def management_row(*, sensors: bool) -> list[dict]:
    rows = [
        row(9005, "Advanced Resource Troubleshooting - Management Plane", 0, [
            timeseries(17, "Management Plane Load Average", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field =~ /^load_(1m|5m|15m)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "load_1m" then "1 minute" else if r._field == "load_5m" then "5 minutes" else "15 minutes" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 0, 12, 10, "short"),
            timeseries(18, "Management Plane Memory", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field =~ /^memory_(used|free|total)_bytes$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "memory_used_bytes" then "Used" else if r._field == "memory_free_bytes" then "Free" else "Total" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 0, 12, 10, "bytes"),
            percent_range(timeseries(19, "Storage Usage", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_storage" and r.hostname == "${hostname}" and r._field == "used_pct")
  |> map(fn: (r) => ({ r with _field: r.mount }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
''', 0, 10, 12, 10, "percent")),
            table(3401, "Storage Partitions", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_storage" and r.hostname == "${hostname}" and r._field =~ /^(total_bytes|used_bytes|available_bytes|used_pct)$/)
  |> last()
  |> map(fn: (r) => ({ mount: r.mount, filesystem: r.filesystem, _field: r._field, _value: float(v: r._value) }))
  |> group()
  |> pivot(rowKey: ["mount", "filesystem"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["used_pct"], desc: true)
''', 12, 10, 12, 10, "Partitions from show system disk-space."),
            percent_range(timeseries(22, "Management Plane Swap", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "swap_used_pct")
  |> map(fn: (r) => ({ r with _field: "Swap used" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 20, 12, 9, "percent")),
            timeseries(23, "Top Management Plane Processes", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_processes" and r.hostname == "${hostname}" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: r.process }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 20, 12, 9, "percent", "Aggregated by process name to avoid PID cardinality."),
        ]),
    ]
    storage_table = rows[0]["panels"][3]
    storage_table["fieldConfig"]["overrides"] = [
        override("total_bytes", displayName="Size", unit="bytes"),
        override("used_bytes", displayName="Used", unit="bytes"),
        override("available_bytes", displayName="Available", unit="bytes"),
        override("used_pct", displayName="Used %", unit="percent", min=0, max=100,
                 custom__cellOptions={"type": "gauge", "mode": "basic", "valueDisplayMode": "text"},
                 thresholds=thresholds(("green", None), ("#EAB839", 80), ("red", 90)), color={"mode": "thresholds"}),
    ]
    if sensors:
        rows.append(row(9100, "Chassis and Environmental Sensors", 0, [
            timeseries(20, "Environmental Sensor Values", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field =~ /^(degrees_c|rpm|watts|volts|amps|value)$/)
  |> map(fn: (r) => ({ r with _field: r.sensor_type + " " + r.slot + " " + r.description + " " + r._field }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 0, 24, 11, "short", "Thermal, fan and power values appear when the platform exposes them."),
            table(21, "Environmental Alarms", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field == "alarm")
  |> group(columns: ["sensor_type", "slot", "description"])
  |> last()
  |> group()
  |> keep(columns: ["sensor_type", "slot", "description", "_value"])
  |> rename(columns: {_value: "alarm"})
''', 0, 11, 24, 9),
        ]))
    return rows


def chassis_rows() -> list[dict]:
    return [
        row(9201, "Chassis Slot Inventory", 0, [
            table(2010, "Installed Cards", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_inventory" and r.hostname == "${hostname}")
  |> group(columns: ["slot", "card_type", "_field"])
  |> last()
  |> group()
  |> pivot(rowKey: ["slot", "card_type"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["slot"])
''', 0, 0, 12, 11, "Inventory from show chassis inventory. Serial numbers remain in the local monitoring database."),
            table(2024, "Live Slot State", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_status" and r.hostname == "${hostname}")
  |> group(columns: ["slot", "card_type", "_field"])
  |> last()
  |> group()
  |> pivot(rowKey: ["slot", "card_type"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["slot"])
''', 12, 0, 12, 11, "Operational state, role and configuration state from show chassis status."),
        ]),
        row(9202, "Chassis Power", 0, [
            timeseries(2011, "Power by Component", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_power" and r.hostname == "${hostname}" and r._field == "power_w")
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.component }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
''', 0, 0, 14, 10, "watt"),
            table(2012, "Power / Card Status", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_power" and r.hostname == "${hostname}" and r._field == "status")
  |> group(columns: ["slot", "component"])
  |> last()
  |> group()
  |> keep(columns: ["slot", "component", "_value"])
  |> rename(columns: {_value: "status"})
  |> sort(columns: ["slot"])
''', 14, 0, 10, 10),
            timeseries(2025, "Chassis Power Budget", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_power" and r.hostname == "${hostname}" and r.component == "power_summary" and r._field =~ /^(provided|used|remaining)_w$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "provided_w" then "Provided" else if r._field == "used_w" then "Used" else "Remaining" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 10, 24, 8, "watt", "Power provided, used and remaining, equivalent to the SNMP chassis power panel."),
        ]),
        row(9204, "Thermal, Fans and Power Sensors", 0, [
            timeseries(2015, "Temperatures by Slot", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "thermal" and r._field == "degrees_c")
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 0, 12, 10, "celsius"),
            timeseries(2016, "Fan Speed by Slot", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "fan" and r._field == "rpm")
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 0, 12, 10, "rpm"),
            timeseries(2017, "Power Sensor Values", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "power" and r._field =~ /^(watts|volts|amps|value)$/)
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description + " " + r._field }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 10, 12, 10, "short"),
            table(2018, "Environmental Alarms", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field == "alarm")
  |> group(columns: ["sensor_type", "slot", "description"])
  |> last()
  |> group()
  |> keep(columns: ["sensor_type", "slot", "description", "_value"])
  |> rename(columns: {_value: "alarm"})
''', 12, 10, 12, 10),
        ]),
        row(9205, "Interfaces by Slot", 0, [
            timeseries(2019, "Throughput by Interface", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _value: r._value * 8.0, _field: r.interface + (if r._field == "in_octets" then " In" else " Out") }}))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 11, "bps", "Every physical port in one view; ports are named ethernet<slot>/<port>."),
        ]),
    ]


# ---------------------------------------------------------------------------
# Template variables
# ---------------------------------------------------------------------------

INFO_QUERY = '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_system" and r.hostname == "${{hostname}}" and r._field == "{field}")
  |> group()
  |> last()
  |> keep(columns: ["_value"])
'''
HA_QUERY = '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${hostname}" and r._field == "state")
  |> group()
  |> last()
  |> keep(columns: ["_value"])
'''
INTERFACE_QUERY = '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r._field == "state" and r.interface =~ /(?i)^ethernet/ and r.interface !~ /\\./)
  |> group(columns: ["interface"])
  |> last()
  |> filter(fn: (r) => r._value == "up")
  |> map(fn: (r) => ({ _value: r.interface }))
  |> group()
  |> distinct(column: "_value")
  |> sort(columns: ["_value"])
'''
DATAPLANE_QUERY = '''
from(bucket: "firewalls")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ _value: r.dataplane }))
  |> group()
  |> distinct(column: "_value")
  |> sort(columns: ["_value"])
'''
VSYS_QUERY = '''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "firewalls", tag: "vsys", predicate: (r) => (r._measurement == "paloalto_api_vsys" or r._measurement == "paloalto_api_logical_interfaces") and r.hostname == "${hostname}", start: -7d)
'''
COUNTER_TAG_QUERY = '''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "firewalls", tag: "{tag}", predicate: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${{hostname}}", start: -7d)
'''


def variables(hostname_query: str) -> list[dict]:
    return [
        variable("hostname", "Firewall", hostname_query),
        variable("counter_category", "Counter category", COUNTER_TAG_QUERY.format(tag="category"), multi=True, include_all=True),
        variable("counter_aspect", "Counter aspect", COUNTER_TAG_QUERY.format(tag="aspect"), multi=True, include_all=True),
        variable("info_version", "Version", INFO_QUERY.format(field="panos_version"), hidden=True),
        variable("info_platform", "Platform", INFO_QUERY.format(field="model"), hidden=True),
        variable("info_ha", "HA", HA_QUERY, hidden=True),
        variable("interface", "Interface", INTERFACE_QUERY, hidden=True, multi=True, include_all=True),
        variable("dataplane", "Dataplane", DATAPLANE_QUERY, hidden=True, multi=True, include_all=True),
        variable("vsys", "VSYS", VSYS_QUERY, hidden=True, multi=True, include_all=True),
    ]


def dashboard(*, title: str, uid: str, description: str, tags: list[str], version: int, panels: list[dict], hostname_query: str) -> dict:
    return {
        "annotations": {"list": []},
        "description": description,
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "liveNow": False,
        "panels": panels,
        "refresh": "20s",
        "schemaVersion": 39,
        "tags": tags,
        "templating": {"list": variables(hostname_query)},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": version,
        "weekStart": "",
    }


def shared_body(*, sensors: bool) -> list[dict]:
    return [
        ha_row(),
        *interface_rows(),
        *zone_rows(),
        dataplane_row(),
        session_row(),
        *counter_rows(),
        *management_row(sensors=sensors),
    ]


def add_imports(panels: list[dict]) -> list[dict]:
    """Prepend Flux imports required by functions used in panel queries."""
    for panel in panels:
        for item in panel.get("targets", []):
            if "strings." in item["query"] and 'import "strings"' not in item["query"]:
                item["query"] = 'import "strings"\n' + item["query"]
        add_imports(panel.get("panels", []))
    return panels


def build_dashboard() -> dict:
    panels = [*header_panels(), *kpi_panels(4), *overview_panels(), *shared_body(sensors=True)]
    hostname_query = '''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "firewalls", tag: "hostname", predicate: (r) => r._measurement == "paloalto_api_sessions", start: -30d)
'''
    return dashboard(
        title="Palo Alto API Performance Monitoring",
        uid="paloalto-api-performance",
        description=(
            "Palo Alto performance monitoring through the PAN-OS XML API with SNMP-dashboard parity, "
            "current-load tiles, hottest-core and link-utilization views, per-VSYS sessions, per-zone throughput, "
            "per-dataplane drill-down and API-only resource metrics."
        ),
        tags=["paloalto", "xml-api", "firewall", "performance"],
        version=4,
        panels=add_imports(stack_rows(panels)),
        hostname_query=hostname_query,
    )


def build_chassis_dashboard() -> dict:
    shared = offset_ids(
        copy.deepcopy([*header_panels(), *kpi_panels(4), *overview_panels(), *shared_body(sensors=False)]),
        CHASSIS_ID_OFFSET,
    )
    overview = [panel for panel in shared if panel["type"] != "row"]
    body = [panel for panel in shared if panel["type"] == "row"]
    cpu = next(panel for panel in overview if panel["title"] == "CPU MP / DP")
    cpu["title"] = "CPU MP / DP by Slot"
    # Chassis-specific sections come first because they are the reason to use
    # this dashboard; the shared sections follow in the standard order.
    panels = [*overview, *chassis_rows(), *body]
    hostname_query = '''
from(bucket: "firewalls")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "paloalto_api_system" and r._field == "model" and r._value =~ /^PA-(52|54|55|70|75)[0-9]+/)
  |> map(fn: (r) => ({ _value: r.hostname }))
  |> group()
  |> distinct(column: "_value")
  |> sort(columns: ["_value"])
'''
    return dashboard(
        title="Palo Alto API Chassis Monitoring",
        uid="paloalto-api-chassis",
        description=(
            "API-only Palo Alto high-end and modular platform monitoring with the full API performance view "
            "plus chassis slot inventory, live slot state and power when supported."
        ),
        tags=["paloalto", "xml-api", "chassis", "performance"],
        version=2,
        panels=add_imports(stack_rows(panels)),
        hostname_query=hostname_query,
    )


def main() -> int:
    OUTPUT.write_text(json.dumps(build_dashboard(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    CHASSIS_OUTPUT.write_text(json.dumps(build_chassis_dashboard(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {CHASSIS_OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
