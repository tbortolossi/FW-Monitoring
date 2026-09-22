import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from telegraf.paloalto_api_collector import (
    ApiError,
    collect_firewall,
    line_protocol,
    load_environment_file,
    parse_dataplane_resources,
    parse_dataplane_utilization,
    parse_chassis_inventory,
    parse_chassis_power,
    parse_chassis_status,
    parse_environmentals,
    parse_global_counters,
    parse_ha_state,
    parse_interface_counters,
    parse_interface_status,
    parse_logical_interface_counters,
    interface_context,
    parse_session_meter,
    parse_management_resources,
    parse_management_processes,
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
            "Tasks: 100 total, 2 running, 96 sleeping, 1 stopped, 1 zombie\n"
            "%Cpu(s): 10.0 us, 5.0 sy, 84.0 id, 1.0 wa\n"
            "MiB Mem : 1000 total, 250 free, 750 used, 0 buff/cache\n"
            "MiB Swap: 200 total, 150 free, 50 used</result>"
        )
        metrics = parse_management_resources(result)
        self.assertEqual(metrics["mp_cpu_pct"], 16.0)
        self.assertEqual(metrics["cpu_iowait_pct"], 1.0)
        self.assertEqual(metrics["memory_used_pct"], 75.0)
        self.assertEqual(metrics["memory_total_bytes"], 1000 * 1024**2)
        self.assertEqual(metrics["swap_used_pct"], 25.0)
        self.assertEqual(metrics["tasks_zombie"], 1)

    def test_management_processes_are_aggregated_by_name(self):
        result = ET.fromstring(
            "<result>PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
            "123 root 20 0 100m 20m 4m R 40.0 2.0 1:00 pan_task\n"
            "124 root 20 0 110m 30m 4m R 35.0 3.0 1:00 pan_task\n"
            "200 root 20 0 50m 10m 2m S 5.0 1.0 0:10 mgmtsrvr</result>"
        )
        indexed = {tags["process"]: fields for tags, fields in parse_management_processes(result)}
        self.assertEqual(indexed["pan_task"]["cpu_pct"], 75.0)
        self.assertEqual(indexed["pan_task"]["processes"], 2)
        self.assertEqual(indexed["pan_task"]["resident_bytes"], 50 * 1024**2)

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

    def test_global_counters_keep_metadata_and_bound_cardinality(self):
        result = ET.fromstring(
            "<result><counters>"
            "<entry><name>flow_policy_deny</name><value>42</value><rate>3</rate>"
            "<severity>drop</severity><category>flow</category><aspect>session</aspect>"
            "<description>Policy denied</description></entry>"
            "<entry><name>another_drop</name><value>99</value><rate>10</rate></entry>"
            "</counters></result>"
        )
        points = parse_global_counters(result, limit=1)
        self.assertEqual(points[0][0]["counter"], "flow_policy_deny")
        self.assertEqual(points[0][0]["severity"], "drop")
        self.assertEqual(points[0][1], {"value": 42, "rate": 3, "description": "Policy denied"})
        all_points = parse_global_counters(result, limit=2)
        fallback = next(point for point in all_points if point[0]["counter"] == "another_drop")
        self.assertEqual(fallback[0]["category"], "unknown")
        self.assertEqual(fallback[0]["aspect"], "unknown")

    def test_hardware_interface_octet_counters_are_collected(self):
        result = ET.fromstring(
            "<result><hw>"
            "<entry><name>ethernet1/1</name><ibytes>112633947248</ibytes>"
            "<obytes>31443272030</obytes><ipackets>110950488</ipackets>"
            "<opackets>62988198</opackets><ierrors>2</ierrors><idrops>3</idrops>"
            "<port><tx-error>4</tx-error><link-down>1</link-down></port></entry>"
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
                        "out_errors": 4,
                        "link_down_count": 1,
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

    def test_session_meter_sums_dataplanes_per_vsys(self):
        result = ET.fromstring(
            "<result>"
            "<entry><vsys>1</vsys><dp>s1dp0</dp><current>100</current><throttled>0</throttled><maximum>0</maximum></entry>"
            "<entry><vsys>1</vsys><dp>s1dp1</dp><current>50</current><throttled>2</throttled><maximum>0</maximum></entry>"
            "<entry><vsys>2</vsys><dp>s1dp0</dp><current>30</current><throttled>0</throttled><maximum>60</maximum></entry>"
            "</result>"
        )
        self.assertEqual(
            parse_session_meter(result),
            [
                ({"vsys": "vsys1"}, {"sessions_active": 150, "sessions_throttled": 2}),
                (
                    {"vsys": "vsys2"},
                    {"sessions_active": 30, "sessions_throttled": 0, "sessions_max": 60, "session_utilization_pct": 50.0},
                ),
            ],
        )

    def test_logical_interface_counters_carry_zone_and_vsys(self):
        status = parse_interface_status(ET.fromstring(
            "<result><ifnet>"
            "<entry><name>ethernet1/1.100</name><zone>trust</zone><vsys>1</vsys></entry>"
            "<entry><name>tunnel.1</name><zone>N/A</zone><vsys>N/A</vsys></entry>"
            "</ifnet></result>"
        ))
        context = interface_context(status)
        self.assertEqual(context, {"ethernet1/1.100": {"zone": "trust", "vsys": "vsys1"}})
        result = ET.fromstring(
            "<result><hw><entry><name>ethernet1/1</name><ibytes>999</ibytes></entry></hw><ifnet><ifnet>"
            "<entry><name>ethernet1/1.100</name><ibytes>1000</ibytes><obytes>2000</obytes>"
            "<tcp_conn>40</tcp_conn><noroute>3</noroute><flowstate>1</flowstate></entry>"
            "<entry><name>tunnel.1</name><ibytes>5</ibytes></entry>"
            "</ifnet></ifnet></result>"
        )
        self.assertEqual(
            parse_logical_interface_counters(result, context),
            [
                (
                    {"interface": "ethernet1/1.100", "zone": "trust", "vsys": "vsys1"},
                    {
                        "in_octets": 1000,
                        "out_octets": 2000,
                        "drop_noroute": 3,
                        "drop_flowstate": 1,
                    },
                ),
                ({"interface": "tunnel.1"}, {"in_octets": 5}),
            ],
        )
        limited = parse_logical_interface_counters(result, context, limit=1)
        self.assertEqual([tags["interface"] for tags, _fields in limited], ["ethernet1/1.100"])

    def test_interface_status_runs_before_counters_to_tag_logical_interfaces(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        status = ET.fromstring(
            "<result><ifnet><entry><name>ethernet1/2</name><zone>untrust</zone><vsys>1</vsys></entry></ifnet></result>"
        )
        counters = ET.fromstring(
            "<result><hw/><ifnet><entry><name>ethernet1/2</name><ibytes>10</ibytes></entry></ifnet></result>"
        )

        def response_for(_config, command):
            return counters if "<counter>" in command else status

        with mock.patch("telegraf.paloalto_api_collector.request_xml", side_effect=response_for):
            lines = collect_firewall(config, {"interfaces", "interface_status"})
        self.assertIn(
            "paloalto_api_logical_interfaces,hostname=fw,interface=ethernet1/2,vsys=vsys1,zone=untrust in_octets=10i",
            lines,
        )

    def test_dos_counters_are_merged_without_duplicates(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY", "counter_limit": 16}
        drops = ET.fromstring(
            "<result><entry><name>flow_dos_red_tcp</name><value>5</value><severity>drop</severity>"
            "<category>flow</category><aspect>dos</aspect></entry></result>"
        )
        dos = ET.fromstring(
            "<result><entry><name>flow_dos_red_tcp</name><value>5</value><severity>drop</severity>"
            "<category>flow</category><aspect>dos</aspect></entry>"
            "<entry><name>flow_dos_syncookie_sent</name><value>7</value><severity>info</severity>"
            "<category>flow</category><aspect>dos</aspect></entry></result>"
        )
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=lambda _config, command: dos if "<aspect>" in command else drops,
        ):
            lines = collect_firewall(config, {"counters"})
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum("flow_dos_red_tcp" in line for line in lines), 1)
        self.assertTrue(any("flow_dos_syncookie_sent" in line for line in lines))

    def test_unsupported_optional_command_is_disabled_after_first_failure(self):
        config = {"hostname": "vm", "host": "192.0.2.1", "api_key_env": "KEY"}
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=ApiError("show -> session -> meter is unexpected"),
        ) as request, mock.patch("sys.stderr"):
            collect_firewall(config, {"vsys"})
            collect_firewall(config, {"vsys"})
        self.assertEqual(request.call_count, 1)
        self.assertIn("vsys", config["_unsupported"])

    def test_ha_state_includes_peer_and_sync(self):
        result = ET.fromstring(
            "<result><enabled>yes</enabled><group><mode>Active-Passive</mode>"
            "<local-info><state>active</state><state-sync>Complete</state-sync></local-info>"
            "<peer-info><state>passive</state><conn-status>up</conn-status></peer-info>"
            "<running-sync>synchronized</running-sync></group></result>"
        )
        self.assertEqual(
            parse_ha_state(result),
            {
                "enabled": True,
                "state": "active",
                "mode": "Active-Passive",
                "peer_state": "passive",
                "peer_connection": "up",
                "config_sync": "synchronized",
                "state_sync": "Complete",
            },
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
                    {"degrees_c": 42.0, "min": 5, "max": 90, "alarm": "False"},
                )
            ],
        )
        self.assertIsInstance(
            parse_environmentals(result, "thermal")[0][1]["degrees_c"],
            float,
        )

    def test_chassis_inventory_exposes_slots_and_card_types(self):
        result = ET.fromstring(
            "<result><chassis><slots>"
            '<entry name="s1"><component>PA-7500-MPC-A</component>'
            "<serial>ABC123</serial><hw-version>1.0</hw-version><operational-status>Up</operational-status></entry>"
            '<entry name="s2"><component>PA-7500-NC-A</component><operational-status>Up</operational-status></entry>'
            "</slots></chassis></result>"
        )
        points = parse_chassis_inventory(result)
        self.assertEqual(points[0][0], {"slot": "s1", "card_type": "supervisor"})
        self.assertEqual(points[0][1]["status"], "Up")
        self.assertEqual(points[1][0]["card_type"], "linecard")

    def test_chassis_power_exposes_component_watts(self):
        result = ET.fromstring(
            "<result><chassis><power><slots>"
            '<entry name="s1"><component>PA-7500-NC-A</component><card-status>Up</card-status><power>350</power></entry>'
            "</slots></power></chassis></result>"
        )
        self.assertEqual(
            parse_chassis_power(result)[0],
            ({"slot": "s1", "component": "PA-7500-NC-A"}, {"status": "Up", "power_w": 350}),
        )

    def test_chassis_status_exposes_each_slot_state(self):
        result = ET.fromstring(
            "<result><status><entry><family>7500</family><slot>1</slot>"
            "<component>PA-7500-MPC-A</component><type>mpc</type><status>Up</status>"
            "<sysrole>active</sysrole><config>success</config><detail>Ready</detail>"
            "<config_detail>In sync</config_detail><disabled>no</disabled></entry>"
            "<entry><family>7500</family><slot>2</slot><component>PA-7500-NC-A</component>"
            "<type>nc</type><status>Down</status><disabled>yes</disabled></entry></status></result>"
        )
        points = parse_chassis_status(result)
        self.assertEqual(points[0][0], {"slot": "1", "card_type": "supervisor"})
        self.assertEqual(points[0][1]["system_role"], "active")
        self.assertFalse(points[0][1]["disabled"])
        self.assertEqual(points[1][0]["card_type"], "linecard")
        self.assertTrue(points[1][1]["disabled"])

    def test_chassis_calls_are_skipped_for_fixed_platforms(self):
        config = {"hostname": "fixed", "host": "192.0.2.1", "api_key_env": "KEY"}
        system = ET.fromstring("<result><system><model>PA-440</model></system></result>")
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            return_value=system,
        ) as request:
            collect_firewall(config, {"system", "chassis_inventory", "chassis_status", "chassis_power"})
        self.assertEqual(request.call_count, 1)
        self.assertFalse(config["_is_chassis"])

    def test_chassis_calls_run_after_api_model_detection(self):
        config = {"hostname": "chassis", "host": "192.0.2.1", "api_key_env": "KEY"}
        responses = {
            "system": ET.fromstring("<result><system><model>PA-7500</model></system></result>"),
            "inventory": ET.fromstring(
                '<result><chassis><slots><entry name="s1"><component>PA-7500-MPC-A</component></entry>'
                "</slots></chassis></result>"
            ),
            "power": ET.fromstring("<result><chassis><power/></chassis></result>"),
            "status": ET.fromstring(
                "<result><status><entry><slot>1</slot><component>PA-7500-MPC-A</component>"
                "<status>Up</status></entry></status></result>"
            ),
        }

        def response_for(_config, command):
            if "<system><info" in command:
                return responses["system"]
            if "<inventory>" in command:
                return responses["inventory"]
            if "<status>" in command:
                return responses["status"]
            return responses["power"]

        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=response_for,
        ) as request:
            lines = collect_firewall(
                config,
                {"system", "chassis_inventory", "chassis_status", "chassis_power"},
            )
        self.assertEqual(request.call_count, 4)
        self.assertTrue(config["_is_chassis"])
        self.assertTrue(any(line.startswith("paloalto_api_chassis_inventory") for line in lines))
        self.assertTrue(any(line.startswith("paloalto_api_chassis_status") for line in lines))

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
            path.write_text("# generated\nPALO_KEY='secret-\\\\value\\'s'\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                load_environment_file(path)
                self.assertEqual(os.environ["PALO_KEY"], "secret-\\value's")


if __name__ == "__main__":
    unittest.main()
