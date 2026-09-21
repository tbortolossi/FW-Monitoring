import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from telegraf.paloalto_api_collector import (
    ApiError,
    line_protocol,
    load_environment_file,
    parse_dataplane_resources,
    parse_dataplane_utilization,
    parse_environmentals,
    parse_global_counters,
    parse_ha_state,
    parse_interface_counters,
    parse_interface_status,
    parse_management_resources,
    parse_sessions,
    parse_storage,
    parse_system_info,
    request_xml,
)


class CollectorParsingTests(unittest.TestCase):
    def test_session_metrics_and_utilization(self):
        result = ET.fromstring(
            "<result><num-active>250</num-active><num-max>1000</num-max>"
            "<num-active-tcp>200</num-active-tcp><cps>17</cps><kbps>900</kbps></result>"
        )
        self.assertEqual(
            parse_sessions(result),
            {
                "sessions_active": 250,
                "sessions_max": 1000,
                "sessions_tcp": 200,
                "cps": 17,
                "session_utilization_pct": 25.0,
            },
        )

    def test_system_info_includes_numeric_uptime(self):
        result = ET.fromstring(
            "<result><system><model>PA-440</model><sw-version>12.2.2</sw-version>"
            "<uptime>3 days, 20:20:21</uptime></system></result>"
        )
        self.assertEqual(
            parse_system_info(result),
            {
                "model": "PA-440",
                "panos_version": "12.2.2",
                "uptime": "3 days, 20:20:21",
                "uptime_seconds": 332421,
            },
        )

    def test_management_cpu_and_memory(self):
        result = ET.fromstring(
            "<result>top - load average: 0.25, 0.50, 0.75\n"
            "%Cpu(s): 10.0 us, 5.0 sy, 85.0 id\n"
            "MiB Mem : 1000 total, 250 free, 750 used, 0 buff/cache</result>"
        )
        metrics = parse_management_resources(result)
        self.assertEqual(metrics["mp_cpu_pct"], 15.0)
        self.assertEqual(metrics["memory_used_pct"], 75.0)
        self.assertEqual(metrics["memory_total_bytes"], 1000 * 1024**2)

    def test_legacy_top_format(self):
        result = ET.fromstring(
            "<result>Cpu(s): 0.5%us, 0.3%sy, 99.1%id\n"
            "Mem: 1000k total, 750k used, 250k free</result>"
        )
        metrics = parse_management_resources(result)
        self.assertAlmostEqual(metrics["mp_cpu_pct"], 0.9)
        self.assertEqual(metrics["memory_used_pct"], 75.0)

    def test_dataplane_cpu_keeps_each_dp_and_core(self):
        result = ET.fromstring(
            "<result><resource-monitor><data-processors><dp0><second><cpu-load-average>"
            "<entry><coreid>0</coreid><value>10,11,12</value></entry>"
            "<entry><coreid>1</coreid><value>33,34,35</value></entry>"
            "</cpu-load-average></second></dp0>"
            "<slot2-dp1><second><cpu-load-average>"
            "<entry><coreid>0</coreid><value>70,75,77</value></entry>"
            "</cpu-load-average></second></slot2-dp1>"
            "</data-processors></resource-monitor></result>"
        )
        points = parse_dataplane_resources(result)
        indexed = {(tags["dataplane"], tags["core"]): fields["cpu_pct"] for tags, fields in points}
        self.assertEqual(indexed[("dp0", "0")], 12)
        self.assertEqual(indexed[("dp0", "1")], 35)
        self.assertEqual(indexed[("slot2-dp1", "0")], 77)
        self.assertEqual(indexed[("dp0", "average")], 23.5)
        self.assertTrue(all(isinstance(value, float) for value in indexed.values()))

    def test_dataplane_cpu_ignores_maximum_table(self):
        result = ET.fromstring(
            "<result><data-processors><dp0><second>"
            "<cpu-load-average><entry><coreid>0</coreid><value>12</value></entry></cpu-load-average>"
            "<cpu-load-maximum><entry><coreid>0</coreid><value>99</value></entry></cpu-load-maximum>"
            "</second></dp0></data-processors></result>"
        )
        indexed = {(tags["dataplane"], tags["core"]): fields["cpu_pct"] for tags, fields in parse_dataplane_resources(result)}
        self.assertEqual(indexed[("dp0", "0")], 12.0)
        self.assertEqual(indexed[("dp0", "average")], 12.0)

    def test_dataplane_name_can_be_an_entry_attribute(self):
        result = ET.fromstring(
            '<result><data-processors><entry name="s2dp1"><second>'
            "<cpu-load-average><entry><coreid>3</coreid><value>67</value></entry>"
            "</cpu-load-average></second></entry></data-processors></result>"
        )
        indexed = {
            (tags["dataplane"], tags["core"]): fields["cpu_pct"]
            for tags, fields in parse_dataplane_resources(result)
        }
        self.assertEqual(indexed[("s2dp1", "3")], 67.0)

    def test_dataplane_resource_utilization_is_kept_per_dp(self):
        result = ET.fromstring(
            "<result><data-processors><s1dp0><second><resource-utilization>"
            "<entry><name>packet buffer</name><value>41</value></entry>"
            "<entry><name>packet descriptor</name><value>12</value></entry>"
            "</resource-utilization></second></s1dp0></data-processors></result>"
        )
        self.assertEqual(
            parse_dataplane_utilization(result),
            [
                ({"dataplane": "s1dp0", "resource": "packet_buffer"}, {"utilization_pct": 41.0}),
                ({"dataplane": "s1dp0", "resource": "packet_descriptor"}, {"utilization_pct": 12.0}),
            ],
        )

    def test_global_counter_allowlist_bounds_cardinality(self):
        result = ET.fromstring(
            "<result><counters>"
            "<entry><name>flow_policy_deny</name><value>42</value></entry>"
            "<entry><name>unbounded_random_counter</name><value>99</value></entry>"
            "</counters></result>"
        )
        self.assertEqual(parse_global_counters(result), [({"counter": "flow_policy_deny"}, {"value": 42})])

    def test_hardware_interface_octet_counters_are_collected(self):
        result = ET.fromstring(
            "<result><hw>"
            "<entry><name>ethernet1/1</name><ibytes>112633947248</ibytes>"
            "<obytes>31443272030</obytes><ipackets>110950488</ipackets>"
            "<opackets>62988198</opackets><ierrors>2</ierrors><idrops>3</idrops></entry>"
            "<entry><name>ethernet1/2</name><ibytes>0</ibytes><obytes>7528446</obytes></entry>"
            "</hw><ifnet><entry><name>ignored-cpu-counter</name><ibytes>999</ibytes></entry></ifnet>"
            "</result>"
        )
        self.assertEqual(
            parse_interface_counters(result),
            [
                (
                    {"interface": "ethernet1/1"},
                    {
                        "in_octets": 112633947248,
                        "out_octets": 31443272030,
                        "in_packets": 110950488,
                        "out_packets": 62988198,
                        "in_errors": 2,
                        "in_discards": 3,
                    },
                ),
                ({"interface": "ethernet1/2"}, {"in_octets": 0, "out_octets": 7528446}),
            ],
        )

    def test_interface_status_merges_hardware_and_logical_details(self):
        result = ET.fromstring(
            "<result><hw><entry><name>ethernet1/1</name><speed>1000</speed>"
            "<duplex>full</duplex><state>up</state><mode>(autoneg)</mode></entry></hw>"
            "<ifnet><entry><name>ethernet1/1</name><zone>outside</zone><vsys>1</vsys>"
            "<fwd>vr:default</fwd></entry></ifnet></result>"
        )
        self.assertEqual(
            parse_interface_status(result),
            [
                (
                    {"interface": "ethernet1/1"},
                    {
                        "speed_mbps": 1000,
                        "duplex": "full",
                        "state": "up",
                        "mode": "(autoneg)",
                        "zone": "outside",
                        "vsys": "1",
                        "forwarding": "vr:default",
                    },
                )
            ],
        )

    def test_ha_disabled_is_reported_as_standalone(self):
        result = ET.fromstring("<result><enabled>no</enabled></result>")
        self.assertEqual(parse_ha_state(result), {"enabled": False, "state": "standalone"})

    def test_disk_space_text_is_parsed(self):
        result = ET.fromstring(
            "<result>Filesystem Size Used Avail Use% Mounted on\n"
            "/dev/root 21G 7.3G 12G 38% /\n/dev/logs 512M 128M 384M 25% /logs</result>"
        )
        points = parse_storage(result)
        self.assertEqual(points[0][0], {"filesystem": "/dev/root", "mount": "/"})
        self.assertEqual(points[0][1]["total_bytes"], 21 * 1024**3)
        self.assertEqual(points[0][1]["used_pct"], 38.0)
        self.assertEqual(points[1][1]["total_bytes"], 512 * 1024**2)

    def test_environmental_sensor_values_are_numeric(self):
        result = ET.fromstring(
            "<result><thermal><entry><slot>1</slot><description>CPU</description>"
            "<alarm>False</alarm><DegreesC>42</DegreesC><min>5</min><max>90</max>"
            "</entry></thermal></result>"
        )
        self.assertEqual(
            parse_environmentals(result, "thermal"),
            [
                (
                    {"sensor_type": "thermal", "slot": "1", "description": "CPU"},
                    {"degrees_c": 42, "min": 5, "max": 90, "alarm": "False"},
                )
            ],
        )

    def test_line_protocol_escapes_tags_and_types(self):
        line = line_protocol("metric", {"hostname": "pa, one"}, {"count": 3, "ratio": 1.5, "state": "up"})
        self.assertEqual(line, 'metric,hostname=pa\\,\\ one count=3i,ratio=1.5,state="up"')

    def test_missing_key_fails_without_network_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ApiError, "is not set"):
                request_xml({"host": "192.0.2.1", "api_key_env": "MISSING"}, "<show/>")

    def test_api_key_is_sent_in_header_not_url(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'<response status="success"><result><ok/></result></response>'
        with mock.patch.dict(os.environ, {"PAN_KEY": "super-secret"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
                request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>")
        request = urlopen.call_args.args[0]
        self.assertNotIn("super-secret", request.full_url)
        self.assertEqual(request.get_header("X-pan-key"), "super-secret")

    def test_generated_environment_file_can_be_loaded_for_host_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.env"
            path.write_text("# generated\nPALO_KEY=secret-value\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                load_environment_file(path)
                self.assertEqual(os.environ["PALO_KEY"], "secret-value")


if __name__ == "__main__":
    unittest.main()
