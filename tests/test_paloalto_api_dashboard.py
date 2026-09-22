import json
import unittest
from pathlib import Path

from scripts.build_paloalto_api_dashboard import build_chassis_dashboard, build_dashboard


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
            "Dataplane ${dataplane}",
            "API Session Details",
            "Data Plane Pressure and Key Drops",
            "Advanced Resource Troubleshooting - Management Plane",
            "Chassis and Environmental Sensors",
        }
        self.assertTrue(expected.issubset(rows))
        self.assertTrue(all(rows[title]["collapsed"] for title in expected))
        self.assertEqual(rows["Dataplane ${dataplane}"]["repeat"], "dataplane")

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

        errors = rows["Interface Errors / Discards"]["panels"][0]
        self.assertEqual(errors["title"], "Errors / Discards ${interface}")
        self.assertEqual(errors["repeat"], "interface")
        self.assertIn("in_errors|in_discards", errors["targets"][0]["query"])

        interface = next(item for item in self.dashboard["templating"]["list"] if item["name"] == "interface")
        self.assertTrue(interface["includeAll"])
        self.assertIn('r._field == "state"', interface["query"])
        self.assertIn('r._value == "up"', interface["query"])
        self.assertIn("^ethernet", interface["query"])
        self.assertIn("r.interface !~ /\\./", interface["query"])

        global_throughput = next(panel for panel in self.dashboard["panels"] if panel.get("title") == "Throughput Global Interfaces")
        self.assertIn("^ethernet", global_throughput["targets"][0]["query"])
        self.assertIn("r.interface !~ /\\./", global_throughput["targets"][0]["query"])

    def test_both_dashboards_share_snmp_parity_sections(self):
        expected = {
            "HA Role Changes",
            "Logging and Management Health",
            "Interfaces",
            "Interface Errors / Discards",
            "VSYS ${vsys}",
            "Data Plane Pressure and Key Drops",
            "Filtered Global Drop Counters",
            "Dataplane ${dataplane}",
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
            self.assertEqual(rows["VSYS ${vsys}"]["repeat"], "vsys")
            names = {item["name"] for item in dashboard["templating"]["list"]}
            self.assertTrue({"interface", "dataplane", "vsys"}.issubset(names))

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

    def test_cpu_overview_exposes_hottest_core(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel.get("title") == "CPU MP / DP")
        query = panel["targets"][0]["query"]
        self.assertIn('r.core == "average"', query)
        self.assertIn('r.core != "average"', query)
        self.assertIn("fn: max", query)

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
            cpu_title = "CPU MP / DP" if dashboard is self.dashboard else "CPU MP / DP by Slot"
            for title in (cpu_title, "CPU Summary - ${dataplane}"):
                query = self._panel(dashboard, title)["targets"][0]["query"]
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
        self.assertEqual(rows["Dataplane ${dataplane}"]["repeat"], "dataplane")
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
