import json
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = PROJECT_DIR / "grafana" / "provisioning" / "dashboards" / "Palo_Dashboard.json"


def find_panel(panels, panel_id):
    for panel in panels:
        if panel.get("id") == panel_id:
            return panel
        nested = find_panel(panel.get("panels", []), panel_id)
        if nested is not None:
            return nested
    return None


class PaloAltoSnmpDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    def test_ha_timeline_keeps_standalone_firewalls(self):
        panel = find_panel(self.dashboard["panels"], 1006)
        self.assertIsNotNone(panel)
        query = panel["targets"][0]["query"]
        self.assertIn('r._value == "standalone" then "Standalone"', query)
        self.assertNotIn('r._value == "Active" or r._value == "Passive"', query)
        self.assertIn('keep(columns: ["_time", "_field", "_value"])', query)


if __name__ == "__main__":
    unittest.main()
