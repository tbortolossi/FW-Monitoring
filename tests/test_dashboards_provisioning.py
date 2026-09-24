"""Structural checks shared by every provisioned Grafana dashboard.

These tests read the JSON files only; they never contact Grafana or InfluxDB.
"""

import json
import re
import unittest
from itertools import combinations
from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
PROVISIONING_DIR = PROJECT_DIR / "grafana" / "provisioning"
DASHBOARD_DIR = PROVISIONING_DIR / "dashboards"
DATASOURCE_FILE = PROVISIONING_DIR / "datasources" / "influxdb.yaml"
DATASOURCE_UID = "P951FEA4DE68E13C5"
HARDCODED_HOSTNAME = re.compile(r'hostname\s*==\s*"(?!\$\{hostname\})')


def iter_panels(panels):
    for panel in panels or []:
        yield panel
        yield from iter_panels(panel.get("panels"))


def iter_datasources(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "datasource":
                yield f"{path}.{key}", value
            yield from iter_datasources(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_datasources(value, f"{path}[{index}]")


def overlaps(first, second):
    a, b = first["gridPos"], second["gridPos"]
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def sibling_groups(dashboard):
    yield "top level", dashboard.get("panels", [])
    for panel in iter_panels(dashboard.get("panels")):
        if panel.get("panels"):
            yield f"row {panel.get('title')!r}", panel["panels"]


class DashboardProvisioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = sorted(DASHBOARD_DIR.glob("*.json"))
        cls.dashboards = {}
        for path in cls.paths:
            cls.dashboards[path.name] = json.loads(path.read_text(encoding="utf-8"))

    def test_dashboards_exist(self):
        self.assertGreaterEqual(len(self.paths), 1)

    def test_uid_and_title_present_and_unique(self):
        uids, titles = {}, {}
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                self.assertTrue(dashboard.get("uid"), "missing uid")
                self.assertTrue(dashboard.get("title"), "missing title")
                self.assertNotIn(dashboard["uid"], uids, f"uid also used by {uids.get(dashboard['uid'])}")
                self.assertNotIn(dashboard["title"], titles, f"title also used by {titles.get(dashboard['title'])}")
                uids[dashboard["uid"]] = name
                titles[dashboard["title"]] = name

    def test_datasource_provisioning_uses_the_dashboard_uid(self):
        config = yaml.safe_load(DATASOURCE_FILE.read_text(encoding="utf-8"))
        datasources = config["datasources"]
        self.assertEqual([source["uid"] for source in datasources], [DATASOURCE_UID])
        self.assertEqual(datasources[0]["url"], "http://influxdb:8086")
        self.assertEqual(datasources[0]["jsonData"]["version"], "Flux")

    def test_every_datasource_reference_uses_the_provisioned_uid(self):
        for name, dashboard in self.dashboards.items():
            references = list(iter_datasources(dashboard))
            with self.subTest(dashboard=name):
                self.assertTrue(references)
            for path, value in references:
                with self.subTest(dashboard=name, path=path):
                    self.assertIsInstance(value, dict, "datasource must be an object with a uid")
                    self.assertEqual(value.get("uid"), DATASOURCE_UID)

    def test_panel_ids_are_unique_per_dashboard(self):
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                ids = [panel.get("id") for panel in iter_panels(dashboard.get("panels"))]
                self.assertNotIn(None, ids)
                duplicates = sorted({panel_id for panel_id in ids if ids.count(panel_id) > 1})
                self.assertEqual(duplicates, [])

    def test_templating_declares_hostname_variable(self):
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                variables = {item["name"]: item for item in dashboard.get("templating", {}).get("list", [])}
                self.assertIn("hostname", variables)
                self.assertEqual(variables["hostname"].get("type"), "query")

    def test_panel_queries_are_scoped_to_the_selected_hostname(self):
        for name, dashboard in self.dashboards.items():
            for panel in iter_panels(dashboard.get("panels")):
                for index, target in enumerate(panel.get("targets") or []):
                    with self.subTest(dashboard=name, panel=panel.get("title"), target=index):
                        query = target.get("query", "")
                        self.assertTrue(query, "panel target without a Flux query")
                        self.assertIn("${hostname}", query)

    def test_no_hardcoded_hostname_literal(self):
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                text = json.dumps(dashboard)
                self.assertEqual(HARDCODED_HOSTNAME.findall(text), [])

    def test_sibling_panels_do_not_overlap(self):
        for name, dashboard in self.dashboards.items():
            for group, panels in sibling_groups(dashboard):
                placed = [panel for panel in panels if "gridPos" in panel]
                for first, second in combinations(placed, 2):
                    with self.subTest(dashboard=name, group=group, first=first.get("id"), second=second.get("id")):
                        self.assertFalse(overlaps(first, second))

    def test_every_dashboard_has_a_load_test_section(self):
        """Each dashboard carries the same collapsed load-test section: eight peak
        tiles reduced over the selected time range, a throughput-versus-CPU ramp
        with the CPU on a right axis, a CPU-versus-throughput scatter plot and
        three supporting time series."""
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                rows = [panel for panel in dashboard["panels"] if panel["type"] == "row"]
                row = next(panel for panel in rows if panel["title"] == "Load Test")
                self.assertTrue(row["collapsed"])
                ha = next(panel for panel in rows if panel["title"] == "HA Role Changes")
                self.assertLess(row["gridPos"]["y"], ha["gridPos"]["y"])
                panels = row["panels"]
                tiles = [panel for panel in panels if panel["type"] == "stat"]
                self.assertEqual(len(tiles), 8)
                self.assertEqual(sorted(panel["gridPos"]["x"] for panel in tiles), list(range(0, 24, 3)))
                for tile in tiles:
                    self.assertIn("v.timeRangeStart", tile["targets"][0]["query"], tile["title"])
                    calc = "lastNotNull" if tile["title"] == "Drops in Range" else "max"
                    self.assertEqual(tile["options"]["reduceOptions"]["calcs"], [calc], tile["title"])
                self.assertTrue(tiles[0]["title"].startswith("Peak Throughput"))
                self.assertEqual(tiles[-1]["title"], "Drops in Range")
                self.assertIn("difference(nonNegative: true)", tiles[-1]["targets"][0]["query"])
                ramp = next(panel for panel in panels if panel["title"].startswith("Throughput vs"))
                self.assertEqual(ramp["gridPos"]["w"], 24)
                self.assertEqual(ramp["fieldConfig"]["defaults"]["unit"], "bps")
                self.assertTrue(any(
                    item["matcher"]["id"] == "byRegexp"
                    and {"id": "custom.axisPlacement", "value": "right"} in item["properties"]
                    for item in ramp["fieldConfig"]["overrides"]
                ))
                scatter = next(panel for panel in panels if panel["type"] == "xychart")
                self.assertEqual(scatter["title"], "CPU vs Throughput")
                self.assertEqual(scatter["options"]["mapping"], "manual")
                self.assertTrue(all(series["x"]["matcher"]["options"] == "throughput_bps" for series in scatter["options"]["series"]))
                self.assertEqual(len([panel for panel in panels if panel["type"] == "timeseries"]), 4)

    def test_dashboard_provider_points_at_provisioning_directory(self):
        config = yaml.safe_load((DASHBOARD_DIR / "dashboards.yaml").read_text(encoding="utf-8"))
        paths = [provider["options"]["path"] for provider in config["providers"]]
        self.assertEqual(paths, ["/etc/grafana/provisioning/dashboards"])
        compose = (PROJECT_DIR / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("./grafana/provisioning:/etc/grafana/provisioning", compose)


if __name__ == "__main__":
    unittest.main()
