import json
import re
import unittest
from pathlib import Path

from scripts.build_paloalto_api_dashboard import (
    MEASUREMENT_SOURCES,
    build_chassis_dashboard,
    build_dashboard,
    panel_sources,
    source_note,
)
from telegraf import paloalto_api_collector as collector


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "grafana/provisioning/dashboards/Palo_API_Dashboard.json"
CHASSIS_DASHBOARD = ROOT / "grafana/provisioning/dashboards/Palo_API_Chassis_Dashboard.json"


class PaloAltoApiDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        cls.chassis_dashboard = json.loads(CHASSIS_DASHBOARD.read_text(encoding="utf-8"))

    def test_inventory_values_use_text_panels(self):
        panels = {panel["title"]: panel for panel in self.dashboard["panels"]}
        self.assertEqual(panels["Version"]["type"], "text")
        self.assertEqual(panels["Platform"]["type"], "text")
        self.assertIn("${info_version:text}", panels["Version"]["options"]["content"])
        self.assertIn("${info_platform:text}", panels["Platform"]["options"]["content"])

    def test_operational_sections_are_collapsible(self):
        rows = {
            panel["title"]: panel
            for panel in self.dashboard["panels"]
            if panel.get("type") == "row"
        }
        expected = {
            "Interfaces",
            "API Interface Details",
            "Interface Errors / Discards",
            "Dataplanes",
            "API Session Details",
            "Data Plane Pressure and Key Drops",
            "Advanced Resource Troubleshooting - Management Plane",
            "Chassis and Environmental Sensors",
        }
        self.assertTrue(expected.issubset(rows))
        self.assertTrue(all(rows[title]["collapsed"] for title in expected))
        self.assertNotIn("repeat", rows["Dataplanes"])
        for panel in rows["Dataplanes"]["panels"]:
            self.assertEqual(panel["repeat"], "dataplane")
            self.assertEqual(panel["repeatDirection"], "h")
            self.assertEqual(panel["gridPos"]["w"], 24)

    def test_active_physical_interfaces_get_repeated_api_panels(self):
        rows = {
            panel["title"]: panel
            for panel in self.dashboard["panels"]
            if panel.get("type") == "row"
        }
        throughput = rows["Interfaces"]["panels"][0]
        self.assertEqual(throughput["title"], "Throughput ${interface}")
        self.assertEqual(throughput["repeat"], "interface")
        self.assertEqual(throughput["maxPerRow"], 2)
        self.assertIn('r.interface == "${interface}"', throughput["targets"][0]["query"])
        self.assertIn("paloalto_api_interfaces", throughput["targets"][0]["query"])
        # Subinterfaces, tunnels, VLAN and loopback only have ifnet counters.
        self.assertIn("paloalto_api_logical_interfaces", throughput["targets"][0]["query"])

        errors = rows["Interface Errors / Discards"]["panels"][0]
        self.assertEqual(errors["title"], "Errors / Discards ${interface}")
        self.assertEqual(errors["repeat"], "interface")
        self.assertIn("in_errors|in_discards", errors["targets"][0]["query"])
        self.assertIn("paloalto_api_logical_interfaces", errors["targets"][0]["query"])

        interface = next(item for item in self.dashboard["templating"]["list"] if item["name"] == "interface")
        self.assertTrue(interface["includeAll"])
        self.assertIn('r._field == "state"', interface["query"])
        self.assertIn('r._value == "up"', interface["query"])
        self.assertIn("^ethernet", interface["query"])
        self.assertIn("r.interface !~ /\\./", interface["query"])
        self.assertIn("paloalto_api_logical_interfaces", interface["query"])
        self.assertIn("union(tables: [physical, logical])", interface["query"])
        self.assertIn("^(internal|hsci|ha[0-9]*|mgmt|management)", interface["query"])

        global_throughput = next(panel for panel in self.dashboard["panels"] if panel.get("title") == "Throughput Global Interfaces")
        self.assertIn("^ethernet", global_throughput["targets"][0]["query"])
        self.assertIn("r.interface !~ /\\./", global_throughput["targets"][0]["query"])

    def test_both_dashboards_share_snmp_parity_sections(self):
        expected = {
            "HA Role Changes",
            "Logging and Management Health",
            "Interfaces",
            "Interface Errors / Discards",
            "VSYS",
            "Data Plane Pressure and Key Drops",
            "Filtered Global Drop Counters",
            "Dataplanes",
            "API Session Details",
            "Zones, Logical Interfaces and Drop Reasons",
            "Advanced Resource Troubleshooting - Management Plane",
        }
        for dashboard in (self.dashboard, self.chassis_dashboard):
            titles = {panel["title"] for panel in dashboard["panels"]}
            self.assertTrue(expected.issubset(titles), expected - titles)
            for title in ("Sessions", "Global CPS", "Session Utilization", "Throughput Global Interfaces",
                          "Hottest DP Core", "Interface Load (last 5 minutes)"):
                self.assertIn(title, titles)
            rows = {panel["title"]: panel for panel in dashboard["panels"] if panel["type"] == "row"}
            self.assertNotIn("repeat", rows["VSYS"])
            vsys_titles = [panel["title"] for panel in rows["VSYS"]["panels"]]
            self.assertEqual(vsys_titles, ["VSYS Sessions - ${vsys}", "VSYS CPS - ${vsys}", "VSYS Throughput by Zone - ${vsys}"])
            for panel in rows["VSYS"]["panels"]:
                self.assertEqual((panel["repeat"], panel["repeatDirection"], panel["gridPos"]["w"]), ("vsys", "h", 24))
            self.assertFalse([panel for panel in dashboard["panels"] if panel["type"] == "row" and "repeat" in panel])
            cps = rows["VSYS"]["panels"][1]["targets"][0]["query"]
            self.assertIn('r._measurement == "paloalto_api_vsys"', cps)
            self.assertIn("cps|packet_rate_pps", cps)
            drop_titles = [panel["title"] for panel in rows["Data Plane Pressure and Key Drops"]["panels"]]
            self.assertIn("Scan / Packet-Based Drops", drop_titles)
            names = {item["name"] for item in dashboard["templating"]["list"]}
            self.assertTrue({"interface", "dataplane", "vsys"}.issubset(names))

    def test_load_test_row_summarizes_a_capacity_ramp(self):
        """The first collapsed section holds the figures of a performance test report."""
        for dashboard in (self.dashboard, self.chassis_dashboard):
            rows = [panel for panel in dashboard["panels"] if panel["type"] == "row"]
            titles = [panel["title"] for panel in rows]
            # First shared section; the chassis dashboard keeps its slot rows first.
            self.assertEqual(titles.index("Load Test") + 1, titles.index("HA Role Changes"))
            load_test = rows[titles.index("Load Test")]
            panels = {panel["title"]: panel for panel in load_test["panels"]}
            tiles = ["Peak Throughput Received", "Peak Throughput Sent", "Peak Packets/s", "Peak CPS", "Peak Sessions",
                     "Peak DP Core", "Peak Packet Buffer", "Drops in Range"]
            for title in tiles:
                self.assertEqual(panels[title]["type"], "stat", title)
                self.assertEqual(panels[title]["gridPos"]["w"], 3, title)
                self.assertIn("v.timeRangeStart", panels[title]["targets"][0]["query"], title)
            self.assertEqual([panels[t]["options"]["reduceOptions"]["calcs"] for t in tiles[:7]], [["max"]] * 7)
            self.assertEqual(panels["Drops in Range"]["options"]["reduceOptions"]["calcs"], ["lastNotNull"])
            self.assertIn('r.severity == "drop"', panels["Drops in Range"]["targets"][0]["query"])
            self.assertIn("difference(nonNegative: true)", panels["Drops in Range"]["targets"][0]["query"])
            self.assertIn("r.resource =~ /^packet_buffer/", panels["Peak Packet Buffer"]["targets"][0]["query"])
            ramp = panels["Throughput vs Dataplane CPU"]
            self.assertEqual(ramp["fieldConfig"]["defaults"]["unit"], "bps")
            axis = ramp["fieldConfig"]["overrides"][0]
            self.assertEqual(axis["matcher"], {"id": "byRegexp", "options": "/CPU|core/"})
            properties = {item["id"]: item["value"] for item in axis["properties"]}
            self.assertEqual((properties["unit"], properties["custom.axisPlacement"], properties["max"]), ("percent", "right", 100))
            for label in ("Received", "Sent", "DP CPU (average)", "DP CPU (active cores)", "Hottest DP core"):
                self.assertIn(f'_field: "{label}"', ramp["targets"][0]["query"])
            curve = panels["CPU vs Throughput"]
            self.assertEqual(curve["type"], "xychart")
            self.assertEqual(curve["options"]["mapping"], "manual")
            # Matchers name the display names set by the overrides: Grafana
            # matches fields by display name, so the raw column names find nothing.
            cpu_series = ["DP CPU (average)", "DP CPU (active cores)", "Hottest DP core"]
            self.assertEqual([s["x"]["matcher"]["options"] for s in curve["options"]["series"]], ["Throughput received"] * 3)
            self.assertEqual([s["y"]["matcher"]["options"] for s in curve["options"]["series"]], cpu_series)
            self.assertEqual([s["name"]["fixed"] for s in curve["options"]["series"]], cpu_series)
            self.assertIn("aggregateWindow(every: 1m", curve["targets"][0]["query"])
            self.assertIn('pivot(rowKey: ["_time"], columnKey: ["_field"]', curve["targets"][0]["query"])
            for title in ("Packet Rate and Connection Rate", "Sessions and Session Table", "Drops and Interface Errors"):
                self.assertEqual(panels[title]["type"], "timeseries", title)
            self.assertTrue(load_test["collapsed"])

    def test_kpi_strip_shows_all_core_and_active_core_dataplane_cpu(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            strip = sorted(
                (panel for panel in dashboard["panels"] if panel["type"] == "stat" and panel["gridPos"]["y"] == 4),
                key=lambda panel: panel["gridPos"]["x"],
            )
            self.assertEqual([panel["title"] for panel in strip[:3]], ["DP CPU (avg)", "DP CPU (active cores)", "Hottest DP Core"])
            # The tiles fill the 24-column row edge to edge without gaps.
            edges = [(panel["gridPos"]["x"], panel["gridPos"]["x"] + panel["gridPos"]["w"]) for panel in strip]
            self.assertEqual(edges[0][0], 0)
            self.assertEqual(edges[-1][1], 24)
            self.assertTrue(all(end == start for (_, end), (start, _) in zip(edges, edges[1:])))
            query = strip[1]["targets"][0]["query"]
            self.assertIn('r._field == "cpu_active_pct"', query)
            self.assertIn('r.core == "average"', query)

    def test_panel_ids_are_unique_and_rows_are_ordered(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            ids = []
            stack = list(dashboard["panels"])
            while stack:
                panel = stack.pop()
                ids.append(panel["id"])
                stack.extend(panel.get("panels", []))
            self.assertEqual(len(ids), len(set(ids)))
            row_positions = [panel["gridPos"]["y"] for panel in dashboard["panels"] if panel["type"] == "row"]
            self.assertEqual(row_positions, sorted(row_positions))
            self.assertEqual(len(row_positions), len(set(row_positions)))

    def test_cpu_overview_shows_dataplane_averages_only(self):
        for dashboard, title in ((self.dashboard, "CPU MP / DP"), (self.chassis_dashboard, "CPU MP / DP by Slot")):
            query = self._panel(dashboard, title)["targets"][0]["query"]
            self.assertIn('r._field == "mp_cpu_pct"', query)
            self.assertIn('r.core == "average"', query)
            self.assertIn("All dataplanes (average)", query)
            self.assertIn("r.count > 1", query)
            self.assertNotIn('r.core != "average"', query)
            self.assertNotIn("cpu_max_pct", query)

    def test_interface_load_table_fits_without_scrolling(self):
        panel = self._panel(self.dashboard, "Interface Load (last 5 minutes)")
        order = panel["transformations"][0]["options"]["indexByName"]
        self.assertEqual(sorted(order, key=order.get), ["interface", "in_pct", "out_pct", "in_bps", "out_bps", "speed_bps"])
        widths = {
            item["matcher"]["options"]: prop["value"]
            for item in panel["fieldConfig"]["overrides"]
            for prop in item["properties"]
            if prop["id"] == "custom.width"
        }
        self.assertEqual(set(widths), set(order))

    @staticmethod
    def _all_panels(dashboard):
        stack = list(dashboard["panels"])
        while stack:
            panel = stack.pop()
            stack.extend(panel.get("panels", []))
            yield panel

    def _panel(self, dashboard, title):
        return next(panel for panel in self._all_panels(dashboard) if panel.get("title") == title)

    def test_hottest_core_prefers_peak_with_average_fallback(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            tile = self._panel(dashboard, "Hottest DP Core")["targets"][0]["query"]
            self.assertIn("r._field =~ /^cpu_(max_)?pct$/", tile)
            self.assertIn("fn: max", tile)
            query = self._panel(dashboard, "CPU Summary - ${dataplane}")["targets"][0]["query"]
            self.assertIn('r._field == "cpu_max_pct"', query)
            self.assertIn("(peak)", query)
            self.assertIn('r._field == "cpu_pct"', query)

    def test_overview_shows_ingress_backlog(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            panel = next(p for p in dashboard["panels"] if p.get("title") == "Ingress Backlog by Dataplane")
            query = panel["targets"][0]["query"]
            self.assertIn("paloalto_api_ingress_backlogs", query)
            self.assertIn('r._field == "usage_pct"', query)
            steps = [step["value"] for step in panel["fieldConfig"]["defaults"]["thresholds"]["steps"]]
            self.assertEqual(steps, [None, 50, 80])

    def test_logging_and_management_health_row(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            rows = {panel["title"]: panel for panel in dashboard["panels"] if panel["type"] == "row"}
            self.assertIn("Logging and Management Health", rows)
            logging = rows["Logging and Management Health"]
            self.assertTrue(logging["collapsed"])
            panels = {panel["title"]: panel for panel in logging["panels"]}
            expected = {
                "Log Rate": ("paloalto_api_logging", "/_rate$/"),
                "Logs Discarded (rate)": ("paloalto_api_logging", "derivative(unit: 1s, nonNegative: true)"),
                "Processes Not Running": ("paloalto_api_software", 'r._field == "running"'),
                "Management Processes Not Running": ("paloalto_api_software", 'r.running != "true"'),
                "Content Versions": ("paloalto_api_system", "url_filtering_version"),
                "GlobalProtect Users": ("paloalto_api_globalprotect", "r.gateway"),
                "GP Users": ("paloalto_api_globalprotect", "current_users"),
            }
            for title, needles in expected.items():
                self.assertIn(title, panels)
                for needle in needles:
                    self.assertIn(needle, panels[title]["targets"][0]["query"])
            self.assertEqual(panels["Log Rate"]["fieldConfig"]["defaults"]["custom"]["axisLabel"], "logs/s")
            self.assertTrue(panels["Log Rate"]["targets"][0]["query"].startswith('import "strings"'))
            self.assertIn("/discard|dropped/", panels["Logs Discarded (rate)"]["targets"][0]["query"])
        standard = {panel["title"] for panel in next(
            p for p in self.dashboard["panels"] if p.get("title") == "Logging and Management Health")["panels"]}
        chassis = {panel["title"] for panel in next(
            p for p in self.chassis_dashboard["panels"] if p.get("title") == "Logging and Management Health")["panels"]}
        self.assertIn("RAID", standard)
        self.assertNotIn("RAID", chassis)

    def test_chassis_raid_sits_in_slot_inventory(self):
        rows = {panel["title"]: panel for panel in self.chassis_dashboard["panels"] if panel["type"] == "row"}
        raid = next(panel for panel in rows["Chassis Slot Inventory"]["panels"] if panel["title"] == "RAID")
        self.assertIn("paloalto_api_raid", raid["targets"][0]["query"])
        healthy = next(item for item in raid["fieldConfig"]["overrides"] if item["matcher"]["options"] == "healthy")
        self.assertIn("mappings", {prop["id"] for prop in healthy["properties"]})

    def test_ha_row_shows_sync_and_link_monitoring(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            rows = {panel["title"]: panel for panel in dashboard["panels"] if panel["type"] == "row"}
            panels = {panel["title"]: panel for panel in rows["HA Role Changes"]["panels"]}
            sync = panels["HA Synchronization"]["targets"][0]["query"]
            links = panels["HA Links and Monitoring"]["targets"][0]["query"]
            self.assertIn('pivot(rowKey: ["row"], columnKey: ["_field"]', sync)
            self.assertIn("r._field !~ /^(ha1_status|", sync)
            self.assertIn("r._field =~ /^(ha1_status|", links)
            for field in ("ha2_status", "link_monitoring", "path_monitoring", "state_reason", "state_duration",
                          "local_priority", "peer_priority", "preemptive"):
                self.assertIn(field, links)

    def test_top_level_panels_do_not_overlap(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            groups = [dashboard["panels"]] + [panel["panels"] for panel in dashboard["panels"] if panel["type"] == "row"]
            for panels in groups:
                cells = set()
                for panel in panels:
                    grid = panel["gridPos"]
                    self.assertLessEqual(grid["x"] + grid["w"], 24, panel["title"])
                    area = {(x, y) for x in range(grid["x"], grid["x"] + grid["w"]) for y in range(grid["y"], grid["y"] + grid["h"])}
                    self.assertFalse(cells & area, panel["title"])
                    cells |= area

    def test_flux_queries_import_strings_when_used(self):
        stack = list(self.dashboard["panels"])
        while stack:
            panel = stack.pop()
            stack.extend(panel.get("panels", []))
            for item in panel.get("targets", []):
                if "strings." in item["query"]:
                    self.assertTrue(item["query"].startswith('import "strings"'))

    def test_dashboard_is_api_only(self):
        for dashboard in (self.dashboard, self.chassis_dashboard):
            serialized = json.dumps(dashboard)
            self.assertNotIn("pan_system", serialized)
            self.assertNotIn("ifHCInOctets", serialized)
        serialized = json.dumps(self.dashboard)
        self.assertIn("paloalto_api_interfaces", serialized)
        self.assertIn("paloalto_api_dataplane_resources", serialized)
        self.assertNotIn("pan_system", serialized)
        self.assertNotIn("pan_entity", serialized)
        self.assertNotIn("ifHCInOctets", serialized)
        self.assertNotIn("ifHCOutOctets", serialized)

    def test_every_queried_panel_describes_its_pan_os_command(self):
        """The (i) tooltip of each panel names the CLI command behind the data."""
        for dashboard in (self.dashboard, self.chassis_dashboard):
            stack = list(dashboard["panels"])
            while stack:
                panel = stack.pop()
                stack.extend(panel.get("panels", []))
                if panel.get("targets"):
                    self.assertRegex(panel["description"], r"Sources?:? .*PAN-OS XML API", panel["title"])
                    self.assertIn("`show ", panel["description"].replace("`debug ", "`show "), panel["title"])
                elif panel["type"] in ("row", "text"):
                    self.assertNotIn("PAN-OS XML API", panel.get("description", ""))
        uptime = next(panel for panel in self.dashboard["panels"] if panel["title"] == "Uptime")
        self.assertIn("`show system info`", uptime["description"])
        self.assertIn("every hour", uptime["description"])
        sessions = next(panel for panel in self.dashboard["panels"] if panel["title"] == "Active Sessions")
        self.assertTrue(sessions["description"].startswith("Source: `show session info`"))
        self.assertIn("every 20 s", sessions["description"])

    def test_measurement_sources_match_the_collector(self):
        """Every measurement the collector writes has a CLI command, and each
        listed command is the CLI spelling of an XML op command it sends."""
        source = collector.__file__
        written = set(re.findall(r'"(paloalto_api_[a-z_]+)"', Path(source).read_text(encoding="utf-8")))
        self.assertEqual(written, set(MEASUREMENT_SOURCES))
        # Opening tag names and text nodes, in order, spell the CLI command.
        xml_commands = {
            " ".join(filter(None, (word for pair in re.findall(r"<([a-z-]+)>([^<]*)", value) for word in pair)))
            for name, value in vars(collector).items()
            if name.endswith("_COMMAND")
        }
        for sources in MEASUREMENT_SOURCES.values():
            for command, seconds in sources:
                self.assertIn(seconds, (20, 60, 3600), command)
                cli = command.replace(" (per VSYS)", "").split()
                self.assertIn(" ".join(cli), xml_commands, command)

    def test_panel_sources_follow_the_query_fields(self):
        counters = 'r._measurement == "paloalto_api_interfaces" and r._field =~ /^(in|out)_octets$/'
        status = 'r._measurement == "paloalto_api_interfaces" and r._field == "speed_mbps"'
        both = 'r._measurement == "paloalto_api_interfaces" and r._field =~ /^(state|link_down_count)$/'
        self.assertEqual([c for c, _ in panel_sources([counters])], ["show counter interface all"])
        self.assertEqual([c for c, _ in panel_sources([status])], ["show interface all"])
        self.assertEqual([c for c, _ in panel_sources([both])], ["show counter interface all", "show interface all"])
        self.assertEqual([c for c, _ in panel_sources([counters, status])], ["show counter interface all", "show interface all"])
        fans = 'r._measurement == "paloalto_api_sensors" and r.sensor_type == "fan" and r._field == "rpm"'
        self.assertEqual([c for c, _ in panel_sources([fans])], ["show system environmentals fans"])
        alarms = 'r._measurement == "paloalto_api_sensors" and r._field == "alarm"'
        self.assertEqual(len(panel_sources([alarms])), 3)
        self.assertEqual(panel_sources(['r._measurement == "pan_system"']), [])
        note = source_note([("show a", 20), ("show b", 7200)])
        self.assertIn("- `show a` every 20 s", note)
        self.assertIn("- `show b` every 2 hours", note)

    def test_datasource_uid_is_stable(self):
        serialized = json.dumps(self.dashboard)
        self.assertIn("P951FEA4DE68E13C5", serialized)

    def test_provisioned_dashboards_match_the_builder(self):
        self.assertEqual(self.dashboard, build_dashboard())
        self.assertEqual(self.chassis_dashboard, build_chassis_dashboard())

    def test_chassis_dashboard_has_dedicated_inventory_and_power_sections(self):
        self.assertEqual(self.chassis_dashboard["uid"], "paloalto-api-chassis")
        rows = {
            panel["title"]: panel
            for panel in self.chassis_dashboard["panels"]
            if panel.get("type") == "row"
        }
        self.assertIn("Chassis Slot Inventory", rows)
        self.assertIn("Chassis Power", rows)
        self.assertIn("Thermal, Fans and Power Sensors", rows)
        self.assertIn("Filtered Global Drop Counters", rows)
        self.assertIn("Dataplanes", rows)
        serialized = json.dumps(self.chassis_dashboard)
        self.assertIn("paloalto_api_chassis_inventory", serialized)
        self.assertIn("paloalto_api_chassis_status", serialized)
        self.assertIn("paloalto_api_chassis_power", serialized)
        self.assertIn("^PA-(52|54|55|70|75)[0-9]+", serialized)
        self.assertIn("counter_category", serialized)
        self.assertIn("counter_aspect", serialized)
        self.assertNotIn("pan_system", serialized)


if __name__ == "__main__":
    unittest.main()
