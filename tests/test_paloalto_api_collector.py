import os
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from telegraf.paloalto_api_collector import (
    ApiError,
    line_protocol,
    parse_dataplane_resources,
    parse_global_counters,
    parse_management_resources,
    parse_sessions,
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

    def test_global_counter_allowlist_bounds_cardinality(self):
        result = ET.fromstring(
            "<result><counters>"
            "<entry><name>flow_policy_deny</name><value>42</value></entry>"
            "<entry><name>unbounded_random_counter</name><value>99</value></entry>"
            "</counters></result>"
        )
        self.assertEqual(parse_global_counters(result), [({"counter": "flow_policy_deny"}, {"value": 42})])

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


if __name__ == "__main__":
    unittest.main()
