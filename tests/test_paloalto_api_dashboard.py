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

    def test_dashboard_is_api_only(self):
        serialized = json.dumps(self.dashboard)
        self.assertIn("paloalto_api_interfaces", serialized)
        self.assertIn("paloalto_api_dataplane_resources", serialized)
        self.assertNotIn("pan_system", serialized)
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
