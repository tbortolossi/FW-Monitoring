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


def pipelines(query):
    """Split a Flux query into pipelines: a pipeline starts on a line without '|>'."""
    blocks = []
    for line in query.split("\n"):
        if not line.lstrip().startswith("|>"):
            blocks.append([])
        blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


class PaloAltoSnmpThroughputWindowTests(unittest.TestCase):
    """PAN-OS refreshes IF-MIB octet counters in steps (about every 12 s on a PA-5580),
    out of phase with the 20 s poll. A 20 s rate swings between ~0.5x and ~1.2x the real
    load, so every SNMP throughput rate is averaged over at least 2 minutes."""

    DASHBOARDS = ("Palo_Dashboard.json", "Palo_Chassis_Dashboard.json")
    WINDOWED_PANELS = (6, 7, 1301, 1302, 1311)

    def load(self, name):
        path = DASHBOARD_PATH.parent / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_throughput_rates_use_a_window_of_at_least_two_minutes(self):
        for name in self.DASHBOARDS:
            dashboard = self.load(name)
            for panel_id in self.WINDOWED_PANELS:
                with self.subTest(dashboard=name, panel=panel_id):
                    query = find_panel(dashboard["panels"], panel_id)["targets"][0]["query"]
                    self.assertIn(
                        "throughputWindow = if int(v: v.windowPeriod) > int(v: 2m) then v.windowPeriod else 2m",
                        query,
                    )
                    octet_pipelines = [p for p in pipelines(query) if "Octets" in p and "derivative" in p]
                    self.assertTrue(octet_pipelines)
                    for pipeline in octet_pipelines:
                        self.assertIn("aggregateWindow(every: throughputWindow, fn: mean", pipeline)
                        self.assertNotIn("every: v.windowPeriod", pipeline)

    def test_cpu_series_of_the_load_ramp_keep_their_native_resolution(self):
        for name in self.DASHBOARDS:
            with self.subTest(dashboard=name):
                query = find_panel(self.load(name)["panels"], 1311)["targets"][0]["query"]
                cpu = [p for p in pipelines(query) if "processor_load_pct" in p or "dp_cpu_pct" in p]
                self.assertEqual(len(cpu), 2)
                for pipeline in cpu:
                    self.assertIn("aggregateWindow(every: v.windowPeriod", pipeline)

    def test_cpu_vs_throughput_scatter_uses_two_minute_points(self):
        for name in self.DASHBOARDS:
            with self.subTest(dashboard=name):
                query = find_panel(self.load(name)["panels"], 1312)["targets"][0]["query"]
                self.assertEqual(query.count("aggregateWindow(every: 2m,"), 3)
                self.assertNotIn("aggregateWindow(every: 1m,", query)


if __name__ == "__main__":
    unittest.main()
