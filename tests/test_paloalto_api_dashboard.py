import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "grafana/provisioning/dashboards/Palo_API_Dashboard.json"


class PaloAltoApiDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
