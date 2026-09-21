#!/usr/bin/env python3
"""Build the provisioned Palo Alto XML API Grafana dashboard."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "grafana/provisioning/dashboards/Palo_API_Dashboard.json"
DATASOURCE = {"type": "influxdb", "uid": "P951FEA4DE68E13C5"}


def target(query: str, ref_id: str = "A") -> dict:
    return {"datasource": DATASOURCE, "query": query.strip(), "refId": ref_id}


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


def build_dashboard() -> dict:
    panels = [
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
        timeseries(2, "CPU MP / DP", '''
management = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "mp_cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Management plane" }))
dataplanes = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct" and r.core == "average")
  |> map(fn: (r) => ({ r with _field: r.dataplane + " average" }))
union(tables: [management, dataplanes])
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 4, 12, 10, "percent", "Management-plane CPU and one average line per dataplane from the XML API."),
        timeseries(3, "MP RAM Usage", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "memory_used_pct")
  |> map(fn: (r) => ({ r with _field: "MP RAM" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 4, 12, 10, "percent"),
        timeseries(4, "Sessions", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and (r._field == "sessions_active" or r._field == "sessions_max"))
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_active" then "Active" else "Maximum" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 14, 8, 7, "short"),
        timeseries(5, "Global CPS", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "cps")
  |> map(fn: (r) => ({ r with _field: "CPS" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 8, 14, 8, 7, "cps"),
        timeseries(6, "Session Utilization", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "session_utilization_pct")
  |> map(fn: (r) => ({ r with _field: "Session utilization" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 16, 14, 8, 7, "percent"),
        timeseries(7, "Throughput Global Interfaces", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and exists r.interface and r.interface !~ /(?i)^(mgmt|management|aux|hsci|ha($|[0-9-]))/ and r.interface !~ /\\./ and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _value: r._value * 8.0, _field: if r._field == "in_octets" then "In" else "Out" }))
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
''', 0, 21, 24, 10, "bps", "Total throughput calculated from PAN-OS hardware interface octet counters."),
    ]

    panels.extend([
        row(9006, "HA Role Changes", 31, [
            state_timeline(1006, "HA Role Changes", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${hostname}" and r._field == "state")
  |> map(fn: (r) => ({ r with _field: "HA role" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 32),
        ]),
        row(9001, "Interfaces", 32, [
            timeseries(8, "Throughput by Interface", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface =~ /^${interface:regex}$/ and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _value: r._value * 8.0, _field: r.interface + (if r._field == "in_octets" then " In" else " Out") }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 33, 12, 10, "bps"),
            timeseries(9, "Packets per Second by Interface", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface =~ /^${interface:regex}$/ and r._field =~ /^(in|out)_packets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: r.interface + (if r._field == "in_packets" then " In" else " Out") }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 33, 12, 10, "pps"),
            table(10, "Interface Status", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r._field =~ /^(state|speed_mbps|duplex|mode|zone|vsys|forwarding)$/)
  |> map(fn: (r) => ({ r with _value: string(v: r._value) }))
  |> group(columns: ["interface", "_field"])
  |> last()
  |> group()
  |> pivot(rowKey: ["interface"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["interface", "state", "speed_mbps", "duplex", "mode", "zone", "vsys", "forwarding"])
  |> sort(columns: ["interface"])
''', 0, 43, 24, 10, "Operational state, negotiated speed, duplex, mode, zone, VSYS and forwarding instance from show interface all."),
        ]),
        row(9004, "Interface Errors / Discards", 33, [
            timeseries(11, "Errors / Discards by Interface", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r.interface =~ /^${interface:regex}$/ and r._field =~ /^(in_errors|in_discards)$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: r.interface + (if r._field == "in_errors" then " Errors" else " Discards") }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 34, 24, 10, "ops"),
        ]),
        row(9002, "Dataplane ${dataplane}", 34, [
            timeseries(12, "CPU per Core - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r.core != "average" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Core " + r.core }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 35, 12, 10, "percent", "One repeated row is created for every dataplane returned by PAN-OS."),
            timeseries(13, "Resource Utilization - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r._field == "utilization_pct")
  |> map(fn: (r) => ({ r with _field: r.resource }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 35, 12, 10, "percent", "Session, packet-buffer, packet-descriptor and software-tag pressure reported by resource-monitor."),
        ], repeat="dataplane"),
        row(9007, "API Session Details", 35, [
            timeseries(14, "Sessions by Protocol", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field =~ /^sessions_(tcp|udp|icmp)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_tcp" then "TCP" else if r._field == "sessions_udp" then "UDP" else "ICMP" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 36, 12, 9, "short"),
            timeseries(15, "Packet Rate", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "packet_rate_pps")
  |> map(fn: (r) => ({ r with _field: "Packet rate" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 36, 12, 9, "pps"),
        ]),
        row(9003, "Data Plane Pressure and Key Drops", 36, [
            timeseries(16, "Selected Drop / Failure Counters", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value")
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({ r with _field: r.counter }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 37, 24, 12, "ops", "A bounded allowlist prevents uncontrolled time-series cardinality."),
        ]),
        row(9005, "Advanced Resource Troubleshooting - Management Plane", 37, [
            timeseries(17, "Management Plane Load Average", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field =~ /^load_(1m|5m|15m)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "load_1m" then "1 minute" else if r._field == "load_5m" then "5 minutes" else "15 minutes" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 38, 12, 10, "short"),
            timeseries(18, "Management Plane Memory", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field =~ /^memory_(used|free|total)_bytes$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "memory_used_bytes" then "Used" else if r._field == "memory_free_bytes" then "Free" else "Total" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 38, 12, 10, "bytes"),
            timeseries(19, "Storage Usage", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_storage" and r.hostname == "${hostname}" and r._field == "used_pct")
  |> map(fn: (r) => ({ r with _field: r.mount }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
''', 0, 48, 24, 10, "percent"),
        ]),
        row(9100, "Chassis and Environmental Sensors", 38, [
            timeseries(20, "Environmental Sensor Values", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field =~ /^(degrees_c|rpm|watts|volts|amps|value)$/)
  |> map(fn: (r) => ({ r with _field: r.sensor_type + " " + r.slot + " " + r.description + " " + r._field }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 39, 24, 11, "short", "Thermal, fan and power values appear when the platform exposes them."),
            table(21, "Environmental Alarms", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field == "alarm")
  |> group(columns: ["sensor_type", "slot", "description"])
  |> last()
  |> group()
  |> keep(columns: ["sensor_type", "slot", "description", "_value"])
  |> rename(columns: {_value: "alarm"})
''', 0, 50, 24, 9),
        ]),
    ])

    hostname_query = '''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "firewalls", tag: "hostname", predicate: (r) => r._measurement == "paloalto_api_sessions", start: -30d)
'''
    info_query = lambda field: f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_system" and r.hostname == "${{hostname}}" and r._field == "{field}")
  |> group()
  |> last()
  |> keep(columns: ["_value"])
'''
    ha_query = '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${hostname}" and r._field == "state")
  |> group()
  |> last()
  |> keep(columns: ["_value"])
'''
    interface_query = '''
from(bucket: "firewalls")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r._field == "in_octets")
  |> map(fn: (r) => ({ _value: r.interface }))
  |> group()
  |> distinct(column: "_value")
  |> sort(columns: ["_value"])
'''
    dataplane_query = '''
from(bucket: "firewalls")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ _value: r.dataplane }))
  |> group()
  |> distinct(column: "_value")
  |> sort(columns: ["_value"])
'''
    return {
        "annotations": {"list": []},
        "description": "Palo Alto performance monitoring through the PAN-OS XML API with SNMP-dashboard layout, per-dataplane drill-down, interface detail and API-only resource metrics.",
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "liveNow": False,
        "panels": panels,
        "refresh": "20s",
        "schemaVersion": 39,
        "tags": ["paloalto", "xml-api", "firewall", "performance"],
        "templating": {"list": [
            variable("hostname", "Firewall", hostname_query),
            variable("info_version", "Version", info_query("panos_version"), hidden=True),
            variable("info_platform", "Platform", info_query("model"), hidden=True),
            variable("info_ha", "HA", ha_query, hidden=True),
            variable("interface", "Interface", interface_query, hidden=True, multi=True, include_all=True),
            variable("dataplane", "Dataplane", dataplane_query, hidden=True, multi=True, include_all=True),
        ]},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": "Palo Alto API Performance Monitoring",
        "uid": "paloalto-api-performance",
        "version": 2,
        "weekStart": "",
    }


def main() -> int:
    OUTPUT.write_text(json.dumps(build_dashboard(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
