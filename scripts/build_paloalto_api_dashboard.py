#!/usr/bin/env python3
"""Build the provisioned Palo Alto XML API Grafana dashboards.

Both dashboards share the same overview and troubleshooting sections so the
compact and chassis views keep feature parity with the SNMP dashboards. The
chassis dashboard adds slot inventory, live slot state and chassis power.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "grafana/provisioning/dashboards/Palo_API_Dashboard.json"
CHASSIS_OUTPUT = ROOT / "grafana/provisioning/dashboards/Palo_API_Chassis_Dashboard.json"
DATASOURCE = {"type": "influxdb", "uid": "P951FEA4DE68E13C5"}

# Physical front-panel ports only, so internal, VLAN, loopback, tunnel and
# subinterface counters are not double-counted in global throughput.
PHYSICAL = 'exists r.interface and r.interface =~ /(?i)^ethernet/ and r.interface !~ /\\./'
# Per-interface panels: physical Ethernet ports use the hardware counters of
# paloalto_api_interfaces; subinterfaces, tunnels, VLAN, loopback and aggregate
# interfaces only exist as ifnet (logical) counters, like ifXTable rows in SNMP.
INTERFACE_SOURCE = (
    '((r._measurement == "paloalto_api_interfaces" and r.interface =~ /(?i)^ethernet/ and r.interface !~ /\\./)'
    ' or (r._measurement == "paloalto_api_logical_interfaces" and (r.interface !~ /(?i)^ethernet/ or r.interface =~ /\\./)))'
)
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


def kpi(panel_id: int, title: str, query: str, x: int, y: int, unit: str, *, levels: dict | None = None,
        description: str = "", sparkline: bool = True, w: int = 3) -> dict:
    """Colored current-value tile for the at-a-glance load strip.

    The query returns a short history so the tile shows a 30-minute sparkline
    behind the current value; the displayed number is always the latest point.
    """
    panel = stat(panel_id, title, query, x, y, w, unit, 1 if unit == "percent" else None)
    panel["fieldConfig"]["defaults"]["thresholds"] = levels or thresholds(("text", None))
    panel["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    panel["options"]["colorMode"] = "background" if levels else "none"
    panel["options"]["graphMode"] = "area" if sparkline else "none"
    panel["description"] = description
    return panel


def guide_lines(panel: dict, levels: dict | None = None) -> dict:
    """Draw dashed warning/critical guide lines on a percentage time series."""
    panel["fieldConfig"]["defaults"]["thresholds"] = levels or LOAD_THRESHOLDS
    panel["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return panel


def stacked(panel: dict) -> dict:
    panel["fieldConfig"]["defaults"]["custom"]["stacking"] = {"mode": "normal", "group": "A"}
    panel["fieldConfig"]["defaults"]["custom"]["fillOpacity"] = 25
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


def regex_override(pattern: str, **properties) -> dict:
    item = override(pattern, **properties)
    item["matcher"]["id"] = "byRegexp"
    return item


def right_axis(panel: dict, pattern: str, unit: str, *, percent: bool = False) -> dict:
    """Plot the series matching ``pattern`` on a second Y axis on the right."""
    properties = {"unit": unit, "custom__axisPlacement": "right", "custom__lineWidth": 2, "custom__fillOpacity": 0}
    if percent:
        properties.update(min=0, max=100)
    panel["fieldConfig"]["overrides"].append(regex_override(pattern, **properties))
    return panel


def peak(panel_id: int, title: str, query: str, x: int, y: int, unit: str, *, levels: dict | None = None,
         description: str = "", calc: str = "max", w: int = 3) -> dict:
    """Tile reduced over the selected time range (peak by default).

    A load test is read against the time range of the test: the tile shows
    the maximum reached during the range, the figure a test report quotes.
    """
    panel = stat(panel_id, title, query, x, y, w, unit, 1 if unit == "percent" else None)
    panel["options"]["reduceOptions"]["calcs"] = [calc]
    panel["fieldConfig"]["defaults"]["thresholds"] = levels or thresholds(("text", None))
    panel["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    panel["options"]["colorMode"] = "background" if levels else "none"
    panel["description"] = description
    return panel


def xychart(panel_id: int, title: str, query: str, x: int, y: int, w: int, h: int, x_field: str,
            series: list[tuple[str, str]], description: str = "", *, x_label: str | None = None) -> dict:
    """Scatter plot of ``series`` (field, label) against ``x_field``.

    ``x_field`` and ``series`` name the Flux columns; every column gets an
    override that sets its display name, and the manual series mapping refers
    to those display names because Grafana matches fields by display name
    once overrides are applied (a matcher on the raw column name finds
    nothing and the panel shows "No data").

    ``pluginVersion`` is required: without it Grafana runs the pre-11.1
    XY Chart migration, which expects the old string-based series format,
    wraps the matcher objects into byName matchers and empties the panel.
    """
    x_label = x_label or x_field
    return {
        "datasource": DATASOURCE,
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "show": "points",
                    "pointSize": {"fixed": 5},
                    "pointShape": "circle",
                    "pointStrokeWidth": 1,
                    "fillOpacity": 60,
                    "axisPlacement": "auto",
                    "axisLabel": "",
                    "lineWidth": 1,
                    "lineStyle": {"fill": "solid"},
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "options": {
            "mapping": "manual",
            "series": [
                {
                    "frame": {"matcher": {"id": "byIndex", "options": 0}},
                    "x": {"matcher": {"id": "byName", "options": x_label}},
                    "y": {"matcher": {"id": "byName", "options": label}},
                    "name": {"fixed": label},
                }
                for field, label in series
            ],
            "legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "pluginVersion": "11.1.0",
        "targets": [target(query)],
        "title": title,
        "type": "xychart",
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
    """Last 30 minutes of a metric, one point per minute, worst series per minute.

    The stat tile reduces this to the latest point and draws the rest as a
    sparkline, so a spike a few minutes ago is still visible at a glance.
    ``field`` may be a Flux regex literal such as ``/^(a|b)$/`` to take the
    worst value across several fields.
    """
    field_filter = f"r._field =~ {field}" if field.startswith("/") else f'r._field == "{field}"'
    return f'''
from(bucket: "firewalls")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.hostname == "${{hostname}}" and {field_filter}{extra})
  |> map(fn: (r) => ({{ r with _value: float(v: r._value) }}))
  |> aggregateWindow(every: 1m, fn: max, createEmpty: false)
  |> group(columns: ["_time"])
  |> max()
  |> group()
  |> sort(columns: ["_time"])
  |> map(fn: (r) => ({{ _time: r._time, _field: "{label}", _value: float(v: r._value) }}))
'''


def kpi_panels(y: int) -> list[dict]:
    return [
        kpi(3001, "DP CPU (avg)", current_value("paloalto_api_dataplane_cpu", "cpu_pct", "DP CPU", ' and r.core == "average"'), 0, y, "percent",
            levels=LOAD_THRESHOLDS, description="Highest per-dataplane average across all cores. This is the value SNMP reports."),
        kpi(3002, "Hottest DP Core", current_value("paloalto_api_dataplane_cpu", "/^cpu_(max_)?pct$/", "Hottest core", ' and r.core != "average"'), 3, y, "percent",
            levels=LOAD_THRESHOLDS, description=(
                "Busiest individual dataplane core. Uses the per-core peak within each minute (cpu_max_pct) when the collector "
                "provides it and falls back to the per-core one-minute average (cpu_pct): both fields are merged and the highest "
                "value per minute wins. A high value with a lower average reveals imbalance or saturated cores hidden by the average."
            )),
        kpi(3003, "MP CPU", current_value("paloalto_api_management", "mp_cpu_pct", "MP CPU"), 6, y, "percent", levels=LOAD_THRESHOLDS),
        kpi(3004, "MP RAM", current_value("paloalto_api_management", "memory_used_pct", "MP RAM"), 9, y, "percent",
            levels=thresholds(("green", None), ("#EAB839", 85), ("red", 95))),
        kpi(3005, "Active Sessions", current_value("paloalto_api_sessions", "sessions_active", "Sessions"), 12, y, "short"),
        kpi(3006, "Session Table", current_value("paloalto_api_sessions", "session_utilization_pct", "Session table"), 15, y, "percent",
            levels=thresholds(("green", None), ("#EAB839", 80), ("red", 90))),
        kpi(3007, "CPS", current_value("paloalto_api_sessions", "cps", "CPS"), 18, y, "cps"),
        kpi(3008, "Throughput (In + Out)", f'''
from(bucket: "firewalls")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field =~ /^(in|out)_octets$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> group(columns: ["_time"])
  |> sum()
  |> group()
  |> sort(columns: ["_time"])
  |> map(fn: (r) => ({{ _time: r._time, _field: "Throughput", _value: r._value * 8.0 }}))
''', 21, y, "bps", description="Sum of physical Ethernet In and Out rates from hardware octet counters, with a 30-minute sparkline."),
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
    # Explicit widths keep every column visible in the 10-unit-wide panel
    # without a horizontal scrollbar; the organize step puts the interface
    # name first and the load bars right next to it.
    interface_load["fieldConfig"]["overrides"] = [
        override("interface", displayName="Interface", custom__width=110),
        override("speed_bps", displayName="Speed", unit="bps", decimals=0, custom__width=75),
        override("in_bps", displayName="In", unit="bps", decimals=1, custom__width=85),
        override("out_bps", displayName="Out", unit="bps", decimals=1, custom__width=85),
        override("in_pct", displayName="In %", unit="percent", decimals=1, min=0, max=100, custom__cellOptions=gauge, custom__width=100, thresholds=LOAD_THRESHOLDS, color={"mode": "thresholds"}),
        override("out_pct", displayName="Out %", unit="percent", decimals=1, min=0, max=100, custom__cellOptions=gauge, custom__width=100, thresholds=LOAD_THRESHOLDS, color={"mode": "thresholds"}),
    ]
    interface_load["transformations"] = [{
        "id": "organize",
        "options": {
            "excludeByName": {"peak_pct": True},
            "indexByName": {"interface": 0, "in_pct": 1, "out_pct": 2, "in_bps": 3, "out_bps": 4, "speed_bps": 5},
        },
    }]
    return [
        guide_lines(percent_range(timeseries(2, "CPU MP / DP", '''
management = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "mp_cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Management plane" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
dataplanes = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r._field == "cpu_pct" and r.core == "average")
  |> map(fn: (r) => ({ r with _field: r.dataplane }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
overall = dataplanes
  |> group(columns: ["_time"])
  |> reduce(identity: {total: 0.0, count: 0}, fn: (r, accumulator) => ({ total: accumulator.total + r._value, count: accumulator.count + 1 }))
  |> filter(fn: (r) => r.count > 1)
  |> map(fn: (r) => ({ _time: r._time, _field: "All dataplanes (average)", _value: r.total / float(v: r.count) }))
  |> group(columns: ["_field"])
  |> sort(columns: ["_time"])
union(tables: [management, overall, dataplanes])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 8, 12, 10, "percent", "Management-plane CPU, the average of all dataplanes (only when the firewall has more than one) and the all-core average of each dataplane (equivalent to SNMP). Dashed lines mark 70% and 90%. The Hottest DP Core tile and the Dataplanes row show per-core load and imbalance."))),
        guide_lines(percent_range(timeseries(3, "MP RAM Usage", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_management" and r.hostname == "${hostname}" and r._field == "memory_used_pct")
  |> map(fn: (r) => ({ r with _field: "MP RAM" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 8, 12, 10, "percent")), thresholds(("green", None), ("#EAB839", 85), ("red", 95))),
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
        guide_lines(percent_range(timeseries(6, "Session Utilization", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${hostname}" and r._field == "session_utilization_pct")
  |> map(fn: (r) => ({ r with _field: "Session utilization" }))
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 16, 18, 8, 7, "percent")), thresholds(("green", None), ("#EAB839", 80), ("red", 90))),
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
''', 0, 25, 14, 10, "bps", "Total throughput of the physical Ethernet ports, calculated from the MAC-level port octet counters of show counter interface all. These include hardware-offloaded flows, which the dataplane ibytes/obytes counters and the session throughput summary miss."),
        interface_load,
        guide_lines(percent_range(timeseries(3011, "Dataplane Resource Pressure", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r._field == "utilization_pct")
  |> map(fn: (r) => ({ r with _field: r.resource }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 35, 8, 8, "percent", "Worst dataplane for each resource-monitor resource: session table, packet buffers, packet descriptors and software tags. Buffer or descriptor pressure precedes packet drops."))),
        guide_lines(percent_range(timeseries(3013, "Ingress Backlog by Dataplane", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ingress_backlogs" and r.hostname == "${hostname}" and r._field == "usage_pct")
  |> map(fn: (r) => ({ r with _field: r.dataplane, _value: float(v: r._value) }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 8, 35, 8, 8, "percent", "Packet-processing ingress queue usage per dataplane from show running resource-monitor ingress-backlogs. 0% means no backlog; a sustained backlog means the dataplane cannot keep up and precedes buffer exhaustion and drops. Dashed lines mark 50% and 80%.")),
            thresholds(("green", None), ("#EAB839", 50), ("red", 80))),
        stacked(timeseries(3012, "Global Drop Rate by Category", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.severity == "drop")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: r.category }))
  |> group(columns: ["_time", "_field"])
  |> sum()
  |> group(columns: ["_field"])
''', 16, 35, 8, 8, "pps", "Packets dropped per second by the dataplane, stacked by PAN-OS counter category so the total drop rate is the top of the stack. Details are in the drop counter sections.")),
    ]


# Load-test section: the figures a performance test report is built from
# (throughput, connection rate, packet rate, sessions, dataplane CPU, buffer
# pressure and drops) on one screen, so a ramp driven by a traffic generator can
# be followed live and summarized over the time range of the test.

def physical_rate(field: str, label: str, window: str = "v.windowPeriod") -> str:
    """Sum of a hardware counter rate over every physical Ethernet port."""
    return f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${{hostname}}" and {PHYSICAL} and r._field == "{field}")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> group(columns: ["_time"])
  |> sum()
  |> group()
  |> map(fn: (r) => ({{ _time: r._time, _field: "{label}", _value: r._value{" * 8.0" if field.endswith("_octets") else ""} }}))
  |> group(columns: ["_field"])
'''


def dataplane_cpu(label: str, window: str = "v.windowPeriod", *, hottest: bool = False) -> str:
    """Average of the dataplane all-core averages, or the hottest core of any dataplane."""
    if hottest:
        selector = 'r._field =~ /^cpu_(max_)?pct$/ and r.core != "average"'
        fn = "max"
    else:
        selector = 'r._field == "cpu_pct" and r.core == "average"'
        fn = "mean"
    return f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${{hostname}}" and {selector})
  |> map(fn: (r) => ({{ r with _value: float(v: r._value) }}))
  |> group()
  |> aggregateWindow(every: {window}, fn: {fn}, createEmpty: false)
  |> map(fn: (r) => ({{ _time: r._time, _field: "{label}", _value: r._value }}))
  |> group(columns: ["_field"])
'''


def session_series(field: str, label: str, fn: str = "mean") -> str:
    return f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sessions" and r.hostname == "${{hostname}}" and r._field == "{field}")
  |> map(fn: (r) => ({{ r with _value: float(v: r._value) }}))
  |> group()
  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)
  |> map(fn: (r) => ({{ _time: r._time, _field: "{label}", _value: r._value }}))
  |> group(columns: ["_field"])
'''


def named(query: str, name: str) -> str:
    """Bind a query to a Flux variable so several can be combined with union."""
    return f"{name} = " + query.strip() + "\n"


def load_test_row() -> dict:
    received = physical_rate("in_octets", "Received")
    sent = physical_rate("out_octets", "Sent")
    packets = physical_rate("in_packets", "Packets/s received")
    drops_in_range = '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.severity == "drop")
  |> difference(nonNegative: true)
  |> group()
  |> sum()
  |> map(fn: (r) => ({ _time: now(), _field: "Drops", _value: r._value }))
'''
    tiles = [
        peak(3601, "Peak Throughput Received", received, 0, 0, "bps",
             description="Highest total receive rate of the physical Ethernet ports during the selected range. For traffic that transits the firewall this is the offered load, the throughput figure of a test report."),
        peak(3602, "Peak Throughput Sent", sent, 3, 0, "bps",
             description="Highest total transmit rate of the physical Ethernet ports during the selected range. Lower than the received peak when the firewall drops traffic."),
        peak(3603, "Peak Packets/s", session_series("packet_rate_pps", "Packets/s"), 6, 0, "pps",
             description="Highest dataplane packet rate reported by show session info during the selected range."),
        peak(3604, "Peak CPS", session_series("cps", "CPS"), 9, 0, "cps",
             description="Highest new-connection rate during the selected range."),
        peak(3605, "Peak Sessions", session_series("sessions_active", "Sessions", "max"), 12, 0, "short",
             description="Highest number of active sessions during the selected range."),
        peak(3606, "Peak DP Core", dataplane_cpu("Hottest core", hottest=True), 15, 0, "percent", levels=LOAD_THRESHOLDS,
             description="Busiest dataplane core during the selected range (one-minute peak when PAN-OS provides it). 100% on the packet-processing cores is the ceiling of the platform: throughput stops growing there."),
        peak(3607, "Peak Packet Buffer", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r._field == "utilization_pct" and r.resource =~ /^packet_buffer/)
  |> group()
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
  |> map(fn: (r) => ({ _time: r._time, _field: "Packet buffer", _value: r._value }))
''', 18, 0, "percent", levels=LOAD_THRESHOLDS,
             description="Highest packet-buffer utilization of any dataplane during the selected range. Buffers fill before the dataplane starts dropping packets."),
        peak(3608, "Drops in Range", drops_in_range, 21, 0, "short", levels=thresholds(("green", None), ("red", 1)), calc="lastNotNull",
             description="Packets dropped by the dataplane during the selected range: sum of the increase of every severity=drop global counter. A clean test run shows 0."),
    ]
    ramp = timeseries(3611, "Throughput vs Dataplane CPU", (
        named(received, "received") + named(sent, "sent")
        + named(dataplane_cpu("DP CPU (average)"), "cpu") + named(dataplane_cpu("Hottest DP core", hottest=True), "hottest")
        + '''union(tables: [received, sent, cpu, hottest])
  |> keep(columns: ["_time", "_field", "_value"])'''
    ), 0, 4, 24, 10, "bps", (
        "The load ramp: throughput of the physical ports (left axis) against dataplane CPU (right axis). "
        "CPU that climbs faster than throughput, or throughput that flattens while CPU keeps rising, shows where the platform saturates. "
        "The dataplane CPU is the one-minute resource-monitor average, so it lags the 20-second throughput by up to a minute."
    ))
    right_axis(ramp, "/CPU|core/", "percent", percent=True)
    curve = xychart(3612, "CPU vs Throughput", (
        named(physical_rate("in_octets", "throughput_bps", "1m"), "throughput")
        + named(dataplane_cpu("dp_cpu_pct", "1m"), "cpu") + named(dataplane_cpu("hottest_core_pct", "1m", hottest=True), "hottest")
        + '''union(tables: [throughput, cpu, hottest])
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.throughput_bps and exists r.dp_cpu_pct)
  |> keep(columns: ["_time", "throughput_bps", "dp_cpu_pct", "hottest_core_pct"])
  |> sort(columns: ["_time"])'''
    ), 0, 14, 12, 10, "throughput_bps", [("dp_cpu_pct", "DP CPU (average)"), ("hottest_core_pct", "Hottest DP core")],
        "Dataplane CPU as a function of the received throughput, one point per minute of the selected range: the CPU-versus-throughput curve of a performance test report. Points that pile up at 100% CPU mark the maximum throughput of the platform for this traffic mix.",
        x_label="Throughput received")
    curve["fieldConfig"]["overrides"] = [
        override("throughput_bps", displayName="Throughput received", unit="bps"),
        override("dp_cpu_pct", displayName="DP CPU (average)", unit="percent", min=0, max=100),
        override("hottest_core_pct", displayName="Hottest DP core", unit="percent", min=0, max=100),
    ]
    rates = timeseries(3613, "Packet Rate and Connection Rate", (
        named(session_series("packet_rate_pps", "Packets/s"), "packets") + named(session_series("cps", "New sessions/s"), "cps")
        + '''union(tables: [packets, cps])
  |> keep(columns: ["_time", "_field", "_value"])'''
    ), 12, 14, 12, 10, "pps", "Dataplane packet rate (left axis) and new connections per second (right axis) from show session info. Connection rate is the limiting figure for small-transaction traffic mixes.")
    right_axis(rates, "/sessions/", "cps")
    sessions = timeseries(3614, "Sessions and Session Table", (
        named(session_series("sessions_active", "Active"), "active") + named(session_series("sessions_tcp", "TCP"), "tcp")
        + named(session_series("sessions_udp", "UDP"), "udp") + named(session_series("session_utilization_pct", "Session table used"), "table")
        + '''union(tables: [active, tcp, udp, table])
  |> keep(columns: ["_time", "_field", "_value"])'''
    ), 0, 24, 12, 9, "short", "Active sessions by protocol (left axis) and session table utilization (right axis).")
    right_axis(sessions, "/table/", "percent", percent=True)
    drops = timeseries(3615, "Drops and Interface Errors", (
        '''drops = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_counters" and r.hostname == "${hostname}" and r._field == "value" and r.severity == "drop")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> group(columns: ["_time"])
  |> sum()
  |> group()
  |> map(fn: (r) => ({ _time: r._time, _field: "Dataplane drops/s", _value: r._value }))
  |> group(columns: ["_field"])
'''
        + named(physical_rate("in_errors", "Ingress errors/s"), "in_errors") + named(physical_rate("in_discards", "Ingress discards/s"), "in_discards")
        + named(physical_rate("out_errors", "Egress errors/s"), "out_errors")
        + '''union(tables: [drops, in_errors, in_discards, out_errors])
  |> keep(columns: ["_time", "_field", "_value"])'''
    ), 12, 24, 12, 9, "pps", "Total dataplane drop rate (severity=drop global counters) and hardware errors and discards of the physical ports. Any sustained value during a ramp means the offered load exceeds what the platform forwards cleanly.")
    return row(9013, "Load Test", 0, [*tiles, ramp, curve, rates, sessions, drops])


HA_LINK_FIELDS = "/^(ha1_status|ha2_status|link_monitoring|path_monitoring|state_reason|state_duration|local_priority|peer_priority|preemptive)$/"


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
    sync = table(1007, "HA Synchronization", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${{hostname}}" and r._field !~ {HA_LINK_FIELDS})
  |> last()
  |> map(fn: (r) => ({{ _field: r._field, _value: string(v: r._value), row: "HA" }}))
  |> group()
  |> pivot(rowKey: ["row"], columnKey: ["_field"], valueColumn: "_value")
  |> drop(columns: ["row"])
''', 0, 6, 24, 4, "Local and peer state, peer connection, running-configuration and session-state synchronization from show high-availability state. Every other HA field the collector adds appears here automatically.")
    sync["fieldConfig"]["overrides"] = [
        override("enabled", displayName="HA enabled"),
        override("mode", displayName="Mode"),
        override("state", displayName="Local state"),
        override("peer_state", displayName="Peer state"),
        override("peer_connection", displayName="Peer connection"),
        override("config_sync", displayName="Config sync"),
        override("state_sync", displayName="Session sync"),
    ]
    links = table(1008, "HA Links and Monitoring", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_ha" and r.hostname == "${{hostname}}" and r._field =~ {HA_LINK_FIELDS})
  |> last()
  |> map(fn: (r) => ({{ _field: r._field, _value: string(v: r._value), row: "HA" }}))
  |> group()
  |> pivot(rowKey: ["row"], columnKey: ["_field"], valueColumn: "_value")
  |> drop(columns: ["row"])
''', 0, 10, 24, 4, "HA1 control link and HA2 data link status, link and path monitoring, the reason and duration of the current state, and the election priorities and preemption from show high-availability all.")
    links["fieldConfig"]["overrides"] = [
        override("ha1_status", displayName="HA1 link"),
        override("ha2_status", displayName="HA2 link"),
        override("link_monitoring", displayName="Link monitoring"),
        override("path_monitoring", displayName="Path monitoring"),
        override("state_reason", displayName="State reason"),
        override("state_duration", displayName="In state since"),
        override("local_priority", displayName="Local priority"),
        override("peer_priority", displayName="Peer priority"),
        override("preemptive", displayName="Preemptive"),
    ]
    return row(9006, "HA Role Changes", 0, [timeline, sync, links])


def interface_rows() -> list[dict]:
    return [
        row(9001, "Interfaces", 0, [
            repeated_panel(timeseries(8, "Throughput ${interface}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r.hostname == "${hostname}" and r.interface == "${interface}" and r._field =~ /^(in|out)_octets$/ and ''' + INTERFACE_SOURCE + ''')
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({ r with _value: r._value * 8.0, _field: if r._field == "in_octets" then "In" else "Out" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 12, 7, "bps", "Repeated for every active physical Ethernet port (MAC-level port rx-bytes/tx-bytes, including offloaded flows) and every logical interface with counters: subinterfaces, tunnels, VLAN, loopback and aggregate interfaces (ifnet counters of show counter interface all)."), "interface", max_per_row=2),
        ]),
        row(9008, "API Interface Details", 0, [
            timeseries(9, "Packets per Second by Interface", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r.hostname == "${hostname}" and r.interface =~ /^${interface:regex}$/ and r._field =~ /^(in|out)_packets$/ and ''' + INTERFACE_SOURCE + ''')
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
  |> filter(fn: (r) => r.hostname == "${hostname}" and r.interface == "${interface}" and r._field =~ /^(in_errors|in_discards|out_errors)$/ and ''' + INTERFACE_SOURCE + ''')
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ r with _field: if r._field == "in_errors" then "In Errors" else if r._field == "in_discards" then "In Discards" else "Out Errors" }))
  |> group(columns: ["_field"])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 10, "pps", "Ingress errors and discards, plus MAC-level transmit errors on physical ports, from show counter interface all (hardware counters for Ethernet ports, ifnet counters for logical interfaces)."), "interface", max_per_row=1),
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
        # Panels repeat per VSYS instead of the row, like the Dataplanes row:
        # cloned rows get a different title font and side-by-side VSYS compare better.
        row(9009, "VSYS", 0, [
            repeated_panel(timeseries(3101, "VSYS Sessions - ${vsys}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_vsys" and r.hostname == "${hostname}" and r.vsys == "${vsys}" and r._field =~ /^sessions_(active|max)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "sessions_active" then "Active" else "VSYS limit" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 8, "short", "Sessions per VSYS summed across dataplanes from show session meter. The limit appears when a VSYS session resource limit is configured."), "vsys", max_per_row=4),
            repeated_panel(timeseries(3103, "VSYS CPS - ${vsys}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_vsys" and r.hostname == "${hostname}" and r.vsys == "${vsys}" and r._field =~ /^(cps|packet_rate_pps)$/)
  |> map(fn: (r) => ({ r with _field: if r._field == "cps" then "CPS" else "Packets/s" }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 8, 24, 8, "short", "New sessions per second and packet rate of this VSYS from show session info scoped to the VSYS (SNMP panVsysTotalCps equivalent). PAN-OS exposes no per-zone CPS through the XML API."), "vsys", max_per_row=4),
            repeated_panel(timeseries(3102, "VSYS Throughput by Zone - ${vsys}", f'''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => {logical} and r.vsys == "${{vsys}}" and exists r.zone and r._field =~ /^(in|out)_octets$/)
{zone_throughput}''', 0, 16, 24, 8, "bps", "Logical interface octet counters of this VSYS summed by zone. In is traffic received from the zone."), "vsys", max_per_row=2),
        ]),
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
    """One row for every dataplane, with panels repeated side by side.

    Panels repeat horizontally instead of repeating the whole row: Grafana
    renders the titles of cloned rows in a different font, and side-by-side
    dataplanes are easier to compare.
    """
    return row(9002, "Dataplanes", 0, [
        repeated_panel(guide_lines(percent_range(timeseries(3201, "CPU Summary - ${dataplane}", '''
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
peak = from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r._field == "cpu_max_pct" and r.core != "average")
  |> map(fn: (r) => ({ r with _field: "Hottest core (peak)", _value: float(v: r._value) }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
union(tables: [average, active, hottest, peak])
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 24, 9, "percent", "The all-core average matches SNMP. Cores reporting 0% (not used for packet processing) are excluded from the active-core average, which shows the real load of the packet-processing cores. Hottest core is the busiest one-minute average; the peak series is the highest per-core value sampled within each minute."))), "dataplane", max_per_row=4),
        repeated_panel(status_history(3202, "Core Load Map - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r.core != "average" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Core " + (if strings.strlen(v: r.core) == 1 then "00" else if strings.strlen(v: r.core) == 2 then "0" else "") + r.core }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 9, 24, 10, "One line per core colored by load, readable even on 64+ core dataplanes."), "dataplane", max_per_row=2),
        repeated_panel(guide_lines(percent_range(timeseries(12, "CPU per Core - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_cpu" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r.core != "average" and r._field == "cpu_pct")
  |> map(fn: (r) => ({ r with _field: "Core " + r.core }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 19, 24, 10, "percent", "One panel is created for every dataplane returned by PAN-OS."))), "dataplane", max_per_row=2),
        repeated_panel(percent_range(timeseries(13, "Resource Utilization - ${dataplane}", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_dataplane_resources" and r.hostname == "${hostname}" and r.dataplane == "${dataplane}" and r._field == "utilization_pct")
  |> map(fn: (r) => ({ r with _field: r.resource }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 29, 24, 9, "percent", "Session, packet-buffer, packet-descriptor and software-tag pressure reported by resource-monitor.")), "dataplane", max_per_row=4),
    ])


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
            counter_rate(3305, "Scan / Packet-Based Drops", "^(flow_scan_drop|flow_dos_pf_.*|flow_dos_ip6.*|flow_dos_curr_sess_(incr|decr)_failed)$", 0, 18, 24,
                         "Scan, packet-based attack (spoofing, fragments, malformed options, IPv6) and DoS session-accounting failures, equivalent to the SNMP scan / packet-based drops panel."),
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


def raid_table(panel_id: int, x: int, y: int, w: int, h: int) -> dict:
    panel = table(panel_id, "RAID", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_raid" and r.hostname == "${hostname}" and (r._field == "status" or r._field == "healthy"))
  |> group(columns: ["disk", "_field"])
  |> last()
  |> map(fn: (r) => ({ disk: r.disk, _field: r._field, _value: string(v: r._value) }))
  |> group()
  |> pivot(rowKey: ["disk"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["disk", "status", "healthy"])
  |> sort(columns: ["disk"])
''', x, y, w, h, "Disk pair state from show system raid detail, on platforms with a RAID log disk (PA-5200/5400/5500/7000 Series).")
    panel["fieldConfig"]["overrides"] = [
        override("disk", displayName="Disk"),
        override("status", displayName="Status"),
        override("healthy", displayName="Healthy", custom__cellOptions={"type": "color-background"}, mappings=[
            {"type": "value", "options": {"true": {"text": "Healthy", "color": "green", "index": 0},
                                          "false": {"text": "Degraded", "color": "red", "index": 1}}},
        ]),
    ]
    return panel


def logging_row(*, raid: bool) -> dict:
    """Log pipeline, management daemons, content versions, RAID and GlobalProtect users."""
    rate_axis = {"axisLabel": "logs/s"}
    log_rate = timeseries(3501, "Log Rate", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_logging" and r.hostname == "${hostname}" and r._field =~ /_rate$/)
  |> map(fn: (r) => ({ r with _field: strings.replaceAll(v: strings.trimSuffix(v: r._field, suffix: "_rate"), t: "_", u: " "), _value: float(v: r._value) }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 0, 12, 9, "short", "Logs per second reported by the management-plane log receiver (debug log-receiver statistics): incoming, written and forwarded.")
    log_rate["fieldConfig"]["defaults"]["custom"].update(rate_axis)
    discards = timeseries(3502, "Logs Discarded (rate)", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_logging" and r.hostname == "${hostname}" and r._field =~ /discard|dropped/ and r._field !~ /_rate$/)
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({ r with _field: strings.replaceAll(v: r._field, t: "_", u: " ") }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 12, 0, 12, 9, "short", "Rate of logs the firewall discarded or dropped, from cumulative logging counters. Any sustained value means logs are lost before reaching disk or the log collector.")
    discards["fieldConfig"]["defaults"]["custom"].update(rate_axis)
    versions = table(3503, "Content Versions", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_system" and r.hostname == "${hostname}" and r._field =~ /^(app_version|threat_version|av_version|wildfire_version|url_filtering_version|device_certificate_status|operational_mode|multi_vsys)$/)
  |> group(columns: ["_field"])
  |> last()
  |> map(fn: (r) => ({ _field: r._field, _value: string(v: r._value), row: "System" }))
  |> group()
  |> pivot(rowKey: ["row"], columnKey: ["_field"], valueColumn: "_value")
  |> drop(columns: ["row"])
''', 0, 9, 24, 4, "Installed content and signature versions, device certificate status, operational mode and multi-VSYS state from show system info.")
    versions["fieldConfig"]["overrides"] = [
        override("app_version", displayName="App-ID"),
        override("threat_version", displayName="Threat"),
        override("av_version", displayName="Antivirus"),
        override("wildfire_version", displayName="WildFire"),
        override("url_filtering_version", displayName="URL filtering"),
        override("device_certificate_status", displayName="Device certificate"),
        override("operational_mode", displayName="Operational mode"),
        override("multi_vsys", displayName="Multi-VSYS"),
    ]
    software = 'r._measurement == "paloalto_api_software" and r.hostname == "${hostname}"'
    not_running = kpi(3504, "Processes Not Running", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => {software} and r._field == "running")
  |> group(columns: ["process"])
  |> last()
  |> group()
  |> map(fn: (r) => ({{ _time: now(), _field: "Not running", _value: if string(v: r._value) == "true" or string(v: r._value) == "1" then 0 else 1 }}))
  |> sum()
''', 0, 13, "short", levels=thresholds(("green", None), ("red", 1)),
        description="Management-plane daemons from show system software status that are not running.", sparkline=False, w=4)
    processes = table(3505, "Management Processes Not Running", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => {software} and (r._field == "running" or r._field == "status"))
  |> group(columns: ["process", "_field"])
  |> last()
  |> map(fn: (r) => ({{ process: r.process, _field: r._field, _value: string(v: r._value) }}))
  |> group()
  |> pivot(rowKey: ["process"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.running and r.running != "true" and r.running != "1")
  |> keep(columns: ["process", "status"])
  |> sort(columns: ["process"])
''', 4, 13, 10 if raid else 20, 8, "Only daemons that are not running are listed; an empty table means every monitored process is up.")
    processes["fieldConfig"]["overrides"] = [
        override("process", displayName="Process"),
        override("status", displayName="Status", custom__cellOptions={"type": "color-text"}, color={"mode": "fixed", "fixedColor": "red"}),
    ]
    gp_users = kpi(3506, "GP Users", current_value("paloalto_api_globalprotect", "current_users", "GP users", " and not exists r.gateway"), 0, 17, "short",
                   description="Current GlobalProtect users on the firewall (total without the per-gateway breakdown).", w=4)
    gp_series = timeseries(3507, "GlobalProtect Users", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_globalprotect" and r.hostname == "${hostname}" and r._field == "current_users")
  |> map(fn: (r) => ({ r with _field: if exists r.gateway then "Gateway " + r.gateway else "Total", _value: float(v: r._value) }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', 0, 21, 24, 8, "short", "Current GlobalProtect users: firewall total plus one series per gateway when the collector reports gateways.")
    panels = [log_rate, discards, versions, not_running, processes]
    if raid:
        panels.append(raid_table(3508, 14, 13, 10, 8))
    return row(9012, "Logging and Management Health", 0, [*panels, gp_users, gp_series])


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
        rows.append(sensor_row(9100, "Chassis and Environmental Sensors", (20, 21, 24, 25)))
    return rows


def sensor_row(row_id: int, title: str, ids: tuple[int, int, int, int]) -> dict:
    """Temperatures, fan speeds, power sensor values and alarms, one panel each."""
    temperatures, fans, power, alarms = ids
    return row(row_id, title, 0, [
        timeseries(temperatures, "Temperatures by Slot", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "thermal" and r._field == "degrees_c")
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 0, 12, 10, "celsius", "Thermal sensors from show system environmentals thermal."),
        timeseries(fans, "Fan Speed by Slot", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "fan" and r._field == "rpm")
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 12, 0, 12, 10, "rpm", "Fan speeds from show system environmentals fans."),
        timeseries(power, "Power Sensor Values", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "power" and r._field =~ /^(watts|volts|amps|value)$/)
  |> map(fn: (r) => ({ r with _field: r.slot + " " + r.description + " " + r._field }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
''', 0, 10, 12, 10, "short", "Power supply voltages, currents and wattage from show system environmentals power."),
        table(alarms, "Environmental Alarms", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field == "alarm")
  |> group(columns: ["sensor_type", "slot", "description"])
  |> last()
  |> group()
  |> keep(columns: ["sensor_type", "slot", "description", "_value"])
  |> rename(columns: {_value: "alarm"})
  |> sort(columns: ["alarm", "sensor_type", "slot"], desc: true)
''', 12, 10, 12, 10, "Latest alarm flag of every sensor; alarmed sensors sort first."),
    ])


def chassis_health_panels(y: int) -> list[dict]:
    """Uncollapsed chassis strip: card counts, power budget, hottest sensor, alarms and a slot-state timeline."""
    status = 'r._measurement == "paloalto_api_chassis_status" and r.hostname == "${hostname}" and r._field == "status"'
    cards_up = kpi(2030, "Cards Up", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => {status})
  |> group(columns: ["slot"])
  |> last()
  |> group()
  |> map(fn: (r) => ({{ _time: now(), _field: "Cards up", _value: if r._value =~ /(?i)^up/ then 1 else 0 }}))
  |> sum()
''', 0, y, "short", levels=thresholds(("green", None)), description="Slots whose card reports an Up status in show chassis status.", sparkline=False, w=4)
    cards_issue = kpi(2031, "Cards Not Up", f'''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => {status})
  |> group(columns: ["slot"])
  |> last()
  |> group()
  |> map(fn: (r) => ({{ _time: now(), _field: "Cards not up", _value: if r._value =~ /(?i)^(up|empty|absent|not present)/ then 0 else 1 }}))
  |> sum()
''', 4, y, "short", levels=thresholds(("green", None), ("red", 1)), description="Installed cards whose status is not Up (down, booting, failed, disabled). Empty slots are ignored.", sparkline=False, w=4)
    power_pct = kpi(2032, "Power Budget Used", '''
from(bucket: "firewalls")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_power" and r.hostname == "${hostname}" and r.component == "power_summary" and (r._field == "used_w" or r._field == "provided_w"))
  |> last()
  |> group()
  |> pivot(rowKey: ["hostname"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.provided_w and exists r.used_w and float(v: r.provided_w) > 0.0)
  |> map(fn: (r) => ({ _time: now(), _field: "Power used", _value: float(v: r.used_w) / float(v: r.provided_w) * 100.0 }))
''', 8, y, "percent", levels=thresholds(("green", None), ("#EAB839", 80), ("red", 90)), description="Power used divided by power provided from show chassis power.", sparkline=False, w=4)
    hottest = kpi(2033, "Hottest Sensor", '''
from(bucket: "firewalls")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "thermal" and r._field == "degrees_c")
  |> aggregateWindow(every: 1m, fn: max, createEmpty: false)
  |> group(columns: ["_time"])
  |> max()
  |> group()
  |> sort(columns: ["_time"])
  |> map(fn: (r) => ({ _time: r._time, _field: "Hottest sensor", _value: float(v: r._value) }))
''', 12, y, "celsius", levels=thresholds(("green", None), ("#EAB839", 65), ("red", 80)), description="Highest thermal sensor reading across every slot, with a 30-minute sparkline.", w=4)
    alarms = kpi(2034, "Sensor Alarms", '''
from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r._field == "alarm")
  |> group(columns: ["sensor_type", "slot", "description"])
  |> last()
  |> group()
  |> map(fn: (r) => ({ _time: now(), _field: "Sensor alarms", _value: if r._value =~ /(?i)^(true|yes|1|on|alarm)/ then 1 else 0 }))
  |> sum()
''', 16, y, "short", levels=thresholds(("green", None), ("red", 1)), description="Thermal, fan and power sensors currently reporting an alarm.", sparkline=False, w=4)
    fans_min = kpi(2035, "Slowest Fan", '''
from(bucket: "firewalls")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "paloalto_api_sensors" and r.hostname == "${hostname}" and r.sensor_type == "fan" and r._field == "rpm")
  |> aggregateWindow(every: 1m, fn: min, createEmpty: false)
  |> group(columns: ["_time"])
  |> min()
  |> group()
  |> sort(columns: ["_time"])
  |> map(fn: (r) => ({ _time: r._time, _field: "Slowest fan", _value: float(v: r._value) }))
''', 20, y, "rpm", description="Lowest fan speed across every slot; a fan reporting 0 rpm has stopped.", w=4)
    timeline = state_timeline(2036, "Slot State", '''
from(bucket: "firewalls")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "paloalto_api_chassis_status" and r.hostname == "${hostname}" and r._field == "status")
  |> map(fn: (r) => ({ r with _field: "Slot " + r.slot }))
  |> group(columns: ["_field"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
''', y)
    timeline["gridPos"] = {"h": 5, "w": 24, "x": 0, "y": y + 4}
    timeline["description"] = "Operational state of every slot from show chassis status. Green is Up, grey is empty, blue is disabled, red is any other state."
    timeline["fieldConfig"]["defaults"]["mappings"] = [
        {"type": "regex", "options": {"pattern": "(?i)^up.*", "result": {"color": "green", "index": 0}}},
        {"type": "regex", "options": {"pattern": "(?i)^(empty|absent|not present).*", "result": {"color": "text", "index": 1}}},
        {"type": "regex", "options": {"pattern": "(?i)^disabled.*", "result": {"color": "blue", "index": 2}}},
        {"type": "regex", "options": {"pattern": "(?i)^(?!(up|empty|absent|not present|disabled)).+", "result": {"color": "red", "index": 3}}},
    ]
    return [cards_up, cards_issue, power_pct, hottest, alarms, fans_min, timeline]


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
            raid_table(2040, 0, 11, 24, 6),
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
        sensor_row(9204, "Thermal, Fans and Power Sensors", (2015, 2016, 2017, 2018)),
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
physical = from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_interfaces" and r.hostname == "${hostname}" and r._field == "state" and r.interface =~ /(?i)^ethernet/ and r.interface !~ /\\./)
  |> group(columns: ["interface"])
  |> last()
  |> filter(fn: (r) => r._value == "up")
  |> map(fn: (r) => ({ _value: r.interface }))
logical = from(bucket: "firewalls")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "paloalto_api_logical_interfaces" and r.hostname == "${hostname}" and r._field == "in_octets" and (r.interface !~ /(?i)^ethernet/ or r.interface =~ /\\./) and r.interface !~ /(?i)^(internal|hsci|ha[0-9]*|mgmt|management)/)
  |> group(columns: ["interface"])
  |> last()
  |> map(fn: (r) => ({ _value: r.interface }))
union(tables: [physical, logical])
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


def shared_body(*, sensors: bool, raid: bool) -> list[dict]:
    """Collapsed sections common to both API dashboards.

    RAID lives in the shared logging row on the standard dashboard (fixed
    high-end appliances such as the PA-5500 Series also have a RAID log disk)
    and in the Chassis Slot Inventory row on the chassis dashboard.
    """
    return [
        load_test_row(),
        ha_row(),
        *interface_rows(),
        *zone_rows(),
        dataplane_row(),
        session_row(),
        *counter_rows(),
        logging_row(raid=raid),
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

# PAN-OS CLI equivalent of the XML API op command the collector runs for each
# measurement, with the default polling interval from CATEGORY_SCHEDULES in
# telegraf/paloalto_api_collector.py. Keep both in sync when adding a category.
# The description of every panel gets a "Source" line built from this table so
# the (i) tooltip in Grafana tells the operator which command backs the data.
MEASUREMENT_SOURCES = {
    "paloalto_api_sessions": [("show session info", 20)],
    "paloalto_api_vsys": [("show session meter", 60), ("show session info (per VSYS)", 60)],
    "paloalto_api_interfaces": [("show counter interface all", 20), ("show interface all", 60)],
    "paloalto_api_logical_interfaces": [("show counter interface all", 20)],
    "paloalto_api_management": [("show system resources", 60)],
    "paloalto_api_processes": [("show system resources", 60)],
    "paloalto_api_dataplane_cpu": [("show running resource-monitor minute last 1", 60)],
    "paloalto_api_dataplane_resources": [("show running resource-monitor minute last 1", 60)],
    "paloalto_api_ingress_backlogs": [("show running resource-monitor ingress-backlogs", 60)],
    "paloalto_api_counters": [
        ("show counter global filter severity drop", 60),
        ("show counter global filter aspect dos", 60),
    ],
    "paloalto_api_ha": [("show high-availability state", 60)],
    "paloalto_api_sensors": [
        ("show system environmentals thermal", 60),
        ("show system environmentals fans", 60),
        ("show system environmentals power", 60),
    ],
    "paloalto_api_logging": [("debug log-receiver statistics", 60)],
    "paloalto_api_globalprotect": [("show global-protect-gateway statistics", 60)],
    "paloalto_api_software": [("show system software status", 60)],
    "paloalto_api_raid": [("show system raid detail", 3600)],
    "paloalto_api_storage": [("show system disk-space", 3600)],
    "paloalto_api_system": [("show system info", 3600)],
    "paloalto_api_chassis_inventory": [("show chassis inventory", 3600)],
    "paloalto_api_chassis_status": [("show chassis status", 60)],
    "paloalto_api_chassis_power": [("show chassis power", 60)],
}
# paloalto_api_interfaces mixes hardware counters (show counter interface all)
# with link state (show interface all); the field filter tells which one a
# panel reads. paloalto_api_sensors is split by the sensor_type tag.
INTERFACE_STATUS_FIELDS = ("state", "speed_mbps", "duplex", "mode", "zone", "vsys", "forwarding", "enabled")
SENSOR_COMMANDS = {"thermal": "thermal", "fan": "fans", "power": "power"}
MEASUREMENT_PATTERN = re.compile(r'_measurement == "(paloalto_api_[a-z_]+)"')
SENSOR_TYPE_PATTERN = re.compile(r'sensor_type == "([a-z]+)"')
FIELD_PATTERN = re.compile(r'_field (?:==|=~) (?:"([a-z_]+)"|/([^/]+)/)')


def panel_sources(queries: list[str]) -> list[tuple[str, int]]:
    """Ordered, de-duplicated CLI commands backing a panel's Flux queries."""
    sources: list[tuple[str, int]] = []
    for query in queries:
        fields = {name for match in FIELD_PATTERN.finditer(query) for name in re.findall(r"[a-z_]+", "".join(g for g in match.groups() if g))}
        sensor_types = set(SENSOR_TYPE_PATTERN.findall(query))
        for measurement in MEASUREMENT_PATTERN.findall(query):
            candidates = MEASUREMENT_SOURCES.get(measurement, [])
            if measurement == "paloalto_api_interfaces" and fields:
                status = fields & set(INTERFACE_STATUS_FIELDS)
                counters = fields - set(INTERFACE_STATUS_FIELDS)
                candidates = [
                    source for source in candidates
                    if (source[0].startswith("show counter") and counters) or (source[0] == "show interface all" and status)
                ]
            elif measurement == "paloalto_api_sensors" and sensor_types:
                wanted = {SENSOR_COMMANDS[kind] for kind in sensor_types if kind in SENSOR_COMMANDS}
                candidates = [source for source in candidates if source[0].rsplit(" ", 1)[1] in wanted]
            for source in candidates:
                if source not in sources:
                    sources.append(source)
    return sources


def source_note(sources: list[tuple[str, int]]) -> str:
    def interval(seconds: int) -> str:
        if seconds >= 3600:
            return "hour" if seconds == 3600 else f"{seconds // 3600} hours"
        return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"

    if len(sources) == 1:
        command, seconds = sources[0]
        return f"Source: `{command}` via the PAN-OS XML API, polled every {interval(seconds)} by default."
    lines = "\n".join(f"- `{command}` every {interval(seconds)}" for command, seconds in sources)
    return f"Sources via the PAN-OS XML API, polled by default:\n\n{lines}"


def add_sources(panels: list[dict]) -> list[dict]:
    """Append the backing PAN-OS command to every panel description.

    Grafana shows the description behind an (i) icon in the panel header, so
    operators can see which CLI command produced the data without opening the
    collector. Rows and text panels have no query and are left untouched.
    """
    for panel in panels:
        sources = panel_sources([item["query"] for item in panel.get("targets", [])])
        if sources:
            note = source_note(sources)
            description = panel.get("description", "").rstrip()
            panel["description"] = f"{description}\n\n{note}" if description else note
        add_sources(panel.get("panels", []))
    return panels


def build_dashboard() -> dict:
    panels = [*header_panels(), *kpi_panels(4), *overview_panels(), *shared_body(sensors=True, raid=True)]
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
            "per-dataplane drill-down, a load-test section for capacity ramps and API-only resource metrics."
        ),
        tags=["paloalto", "xml-api", "firewall", "performance"],
        version=6,
        panels=add_sources(add_imports(stack_rows(panels))),
        hostname_query=hostname_query,
    )


def build_chassis_dashboard() -> dict:
    shared = offset_ids(
        copy.deepcopy([*header_panels(), *kpi_panels(4), *overview_panels(), *shared_body(sensors=False, raid=False)]),
        CHASSIS_ID_OFFSET,
    )
    overview = [panel for panel in shared if panel["type"] != "row"]
    body = [panel for panel in shared if panel["type"] == "row"]
    cpu = next(panel for panel in overview if panel["title"] == "CPU MP / DP")
    cpu["title"] = "CPU MP / DP by Slot"
    # The chassis health strip sits directly under the load strip; everything
    # below it moves down by its height.
    health = chassis_health_panels(8)
    health_height = max(panel["gridPos"]["y"] + panel["gridPos"]["h"] for panel in health) - 8
    for panel in overview:
        if panel["gridPos"]["y"] >= 8:
            panel["gridPos"]["y"] += health_height
    # Chassis-specific sections come first because they are the reason to use
    # this dashboard; the shared sections follow in the standard order.
    panels = [*overview, *health, *chassis_rows(), *body]
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
        version=4,
        panels=add_sources(add_imports(stack_rows(panels))),
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
