import json
import unittest
from pathlib import Path


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
