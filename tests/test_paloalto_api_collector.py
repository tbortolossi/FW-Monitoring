import io
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import urllib.error

from telegraf.paloalto_api_collector import (
    CATEGORY_SCHEDULES,
    DATAPLANE_COMMAND,
    OPTIONAL_CATEGORIES,
    ApiError,
    _first_number,
    _result,
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
    parse_globalprotect,
    parse_ingress_backlogs,
    parse_log_receiver,
    parse_raid,
    parse_software_status,
    request_xml,
    run_once,
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

    def test_management_memory_with_truncated_decimals(self):
        # PAN-OS 12.1.7 on a PA-5580: top drops the decimal of a too-wide value and marks it with "+".
        result = ET.fromstring(
            "<result>MiB Mem : 1031206.+total, 170483.8 free, 603620.1 used, 311240.7 buff/cache\n"
            "MiB Swap:   2047.9 total,   2047.9 free,      0.0 used. 427586.6 avail Mem</result>"
        )
        metrics = parse_management_resources(result)
        self.assertEqual(metrics["memory_total_bytes"], 1031206.0 * 1024**2)
        self.assertEqual(metrics["memory_free_bytes"], 170483.8 * 1024**2)
        self.assertAlmostEqual(metrics["memory_used_pct"], 603620.1 / 1031206.0 * 100.0)
        self.assertEqual(metrics["swap_total_bytes"], 2047.9 * 1024**2)
        self.assertEqual(metrics["swap_used_pct"], 0.0)

    def test_management_memory_with_truncated_integer_is_ignored(self):
        result = ET.fromstring(
            "<result>KiB Mem : 13184950+total, 10234567+free, 2345678 used, 1234567 buff/cache\n"
            "KiB Swap:  8388604 total,  8388604 free,        0 used.</result>"
        )
        metrics = parse_management_resources(result)
        self.assertNotIn("memory_used_pct", metrics)
        self.assertNotIn("memory_total_bytes", metrics)
        self.assertEqual(metrics["swap_total_bytes"], 8388604.0 * 1024)

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
            "<result><data-processors><dp0><minute>"
            "<cpu-load-average><entry><coreid>0</coreid><value>12</value></entry></cpu-load-average>"
            "<cpu-load-maximum><entry><coreid>0</coreid><value>99</value></entry></cpu-load-maximum>"
            "</minute></dp0></data-processors></result>"
        )
        indexed = {(tags["dataplane"], tags["core"]): fields for tags, fields in parse_dataplane_resources(result)}
        # The maximum is stored separately and never mixed into cpu_pct.
        self.assertEqual(indexed[("dp0", "0")], {"cpu_pct": 12.0, "cpu_max_pct": 99.0})
        self.assertEqual(indexed[("dp0", "average")], {"cpu_pct": 12.0, "cpu_max_pct": 99.0})

    def test_dataplane_average_point_uses_mean_of_averages_and_max_of_maxima(self):
        result = ET.fromstring(
            "<result><data-processors><s1dp0><minute>"
            "<cpu-load-average><entry><coreid>0</coreid><value>10</value></entry>"
            "<entry><coreid>1</coreid><value>30</value></entry></cpu-load-average>"
            "<cpu-load-maximum><entry><coreid>0</coreid><value>55</value></entry>"
            "<entry><coreid>1</coreid><value>80</value></entry></cpu-load-maximum>"
            "</minute></s1dp0></data-processors></result>"
        )
        indexed = {(tags["dataplane"], tags["core"]): fields for tags, fields in parse_dataplane_resources(result)}
        self.assertEqual(indexed[("s1dp0", "1")], {"cpu_pct": 30.0, "cpu_max_pct": 80.0})
        self.assertEqual(indexed[("s1dp0", "average")], {"cpu_pct": 20.0, "cpu_max_pct": 80.0})
        line = line_protocol("m", {}, indexed[("s1dp0", "average")])
        self.assertEqual(line, "m cpu_max_pct=80.0,cpu_pct=20.0")

    def test_dataplane_command_reads_last_minute(self):
        self.assertIn("<minute><last>1</last></minute>", DATAPLANE_COMMAND)

    def test_dataplane_poll_records_names_for_ingress_backlogs(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        dataplane = ET.fromstring(
            "<result><data-processors><dp0><minute><cpu-load-average>"
            "<entry><coreid>0</coreid><value>5</value></entry></cpu-load-average>"
            "</minute></dp0><dp1><minute><cpu-load-average>"
            "<entry><coreid>0</coreid><value>7</value></entry></cpu-load-average>"
            "</minute></dp1></data-processors></result>"
        )
        backlog = ET.fromstring("<result>none</result>")
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=lambda _config, command: backlog if "ingress-backlogs" in command else dataplane,
        ):
            lines = collect_firewall(config, {"dataplane", "ingress_backlogs"})
        self.assertEqual(config["_dataplanes"], ["dp0", "dp1"])
        self.assertIn("paloalto_api_ingress_backlogs,dataplane=dp0,hostname=fw sessions=0i,usage_pct=0.0", lines)
        self.assertIn("paloalto_api_ingress_backlogs,dataplane=dp1,hostname=fw sessions=0i,usage_pct=0.0", lines)

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

    def test_optional_command_refused_by_role_is_disabled_after_first_failure(self):
        config = {"hostname": "vm", "host": "192.0.2.1", "api_key_env": "KEY"}
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=ApiError("You are not authorized to perform this operation"),
        ) as request, mock.patch("sys.stderr"):
            collect_firewall(config, {"logging"})
            collect_firewall(config, {"logging"})
        self.assertEqual(request.call_count, 1)
        self.assertIn("logging", config["_unsupported"])

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
                    {"degrees_c": 42.0, "min": 5.0, "max": 90.0, "alarm": "False"},
                )
            ],
        )
        # Every physical value is a float so the InfluxDB field type is stable.
        for name in ("degrees_c", "min", "max"):
            self.assertIsInstance(parse_environmentals(result, "thermal")[0][1][name], float)

    def test_physical_values_keep_the_same_line_protocol_type(self):
        def power_line(volts, watts):
            result = ET.fromstring(
                f"<result><power><entry><slot>1</slot><description>PSU</description>"
                f"<Volts>{volts}</Volts><Watts>{watts}</Watts><RPMs>{volts}</RPMs></entry></power></result>"
            )
            tags, fields = parse_environmentals(result, "power")[0]
            return line_protocol("paloalto_api_sensors", tags, fields)

        integer, decimal = power_line("12", "300"), power_line("12.5", "300.5")
        self.assertIn("volts=12.0", integer)
        self.assertIn("watts=300.0", integer)
        self.assertIn("rpm=12.0", integer)
        self.assertIn("volts=12.5", decimal)
        self.assertNotRegex(integer, r"=\d+i")
        self.assertNotRegex(decimal, r"=\d+i")

        def chassis_line(power):
            result = ET.fromstring(
                f'<result><slots><entry name="s1"><component>NC</component><power>{power}</power></entry></slots>'
                f"<summary><provided>{power}</provided></summary></result>"
            )
            return [line_protocol("m", tags, fields) for tags, fields in parse_chassis_power(result)]

        self.assertIn("power_w=350.0", chassis_line("350")[0])
        self.assertIn("power_w=350.5", chassis_line("350.5")[0])
        self.assertIn("provided_w=350.0", chassis_line("350")[1])

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
            ({"slot": "s1", "component": "PA-7500-NC-A"}, {"status": "Up", "power_w": 350.0}),
        )
        self.assertIsInstance(parse_chassis_power(result)[0][1]["power_w"], float)

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
            path.write_text(
                "# generated\n"
                'PALO_KEY="pa\'ss\\\\wo\\"rd\\$x #=y "\n'
                "LEGACY_KEY='secret-\\\\value\\'s'\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                load_environment_file(path)
                self.assertEqual(os.environ["PALO_KEY"], "pa'ss\\wo\"rd$x #=y ")
                self.assertEqual(os.environ["LEGACY_KEY"], "secret-\\value's")

class NewCategoryTests(unittest.TestCase):
    def test_sessions_active_prefers_num_active_over_earlier_num_installed(self):
        result = ET.fromstring(
            "<result><num-installed>987654</num-installed><num-max>1000</num-max>"
            "<num-active>250</num-active></result>"
        )
        fields = parse_sessions(result)
        self.assertEqual(fields["sessions_active"], 250)
        self.assertEqual(fields["session_utilization_pct"], 25.0)

    def test_first_number_falls_back_to_later_alias(self):
        result = ET.fromstring("<result><num-installed>7</num-installed></result>")
        self.assertEqual(_first_number(result, "num-active", "num-installed"), 7)

    def test_system_info_extras(self):
        result = ET.fromstring(
            "<result><system><model>PA-5220</model><family>5200</family>"
            "<app-version>8800-9000</app-version><threat-version>8800-9000</threat-version>"
            "<av-version>4900-5400</av-version><wildfire-version>900000-904000</wildfire-version>"
            "<url-filtering-version>20260101.20001</url-filtering-version><multi-vsys>on</multi-vsys>"
            "<operational-mode>normal</operational-mode>"
            "<device-certificate-status>Valid</device-certificate-status></system></result>"
        )
        fields = parse_system_info(result)
        self.assertEqual(fields["app_version"], "8800-9000")
        self.assertEqual(fields["threat_version"], "8800-9000")
        self.assertEqual(fields["av_version"], "4900-5400")
        self.assertEqual(fields["wildfire_version"], "900000-904000")
        self.assertEqual(fields["url_filtering_version"], "20260101.20001")
        self.assertEqual(fields["multi_vsys"], "on")
        self.assertEqual(fields["operational_mode"], "normal")
        self.assertEqual(fields["device_certificate_status"], "Valid")
        self.assertEqual(fields["family"], "5200")

    def test_ha_state_extras(self):
        result = ET.fromstring(
            "<result><enabled>yes</enabled><group><group-id>1</group-id><mode>Active-Passive</mode>"
            "<local-info><state>active</state><state-reason>User requested</state-reason>"
            "<state-duration>86400</state-duration><priority>100</priority><preemptive>no</preemptive>"
            "<ha1><conn-status>up</conn-status></ha1><ha2><conn-status>up</conn-status></ha2></local-info>"
            "<peer-info><state>passive</state><conn-status>up</conn-status><priority>110</priority></peer-info>"
            "<link-monitoring><enabled>yes</enabled></link-monitoring>"
            "<path-monitoring><enabled>no</enabled></path-monitoring>"
            "<running-sync>synchronized</running-sync></group></result>"
        )
        fields = parse_ha_state(result)
        self.assertEqual(fields["group"], "1")
        self.assertEqual(fields["state"], "active")
        self.assertEqual(fields["peer_state"], "passive")
        self.assertEqual(fields["state_reason"], "User requested")
        self.assertEqual(fields["state_duration"], "86400")
        self.assertEqual(fields["ha1_status"], "up")
        self.assertEqual(fields["ha2_status"], "up")
        self.assertEqual(fields["link_monitoring"], "yes")
        self.assertEqual(fields["path_monitoring"], "no")
        self.assertEqual(fields["preemptive"], "no")
        self.assertEqual(fields["local_priority"], 100)
        self.assertEqual(fields["peer_priority"], 110)

    def test_ingress_backlogs_text_with_dp_headers(self):
        result = ET.fromstring(
            "<result>-- SLOT: s1, DP: dp0 --\n"
            "USAGE - ATOMIC: 92% TOTAL: 93%\n\n"
            "TOP SESSIONS:\n"
            "SESS-ID         PCT     GRP-ID  COUNT\n"
            "6               92%     1       156\n"
            "                        7       1732\n"
            "88              4%      1       20\n\n"
            "-- SLOT: s1, DP: dp1 --\n"
            "USAGE - ATOMIC: 0% TOTAL: 0%\n</result>"
        )
        points = dict((tags["dataplane"], fields) for tags, fields in parse_ingress_backlogs(result, ["dp0", "dp1"]))
        self.assertEqual(points["dp0"], {"usage_pct": 93.0, "sessions": 2})
        self.assertEqual(points["dp1"], {"usage_pct": 0.0, "sessions": 0})

    def test_ingress_backlogs_short_headers_keep_chassis_names(self):
        result = ET.fromstring("<result>DP s1dp0\n12   40%   1   3\n-- DP s2dp1 --\n</result>")
        points = dict((tags["dataplane"], fields) for tags, fields in parse_ingress_backlogs(result, ["s1dp0", "s2dp1", "s3dp0"]))
        self.assertEqual(points["s1dp0"], {"usage_pct": 40.0, "sessions": 1})
        self.assertEqual(points["s2dp1"], {"usage_pct": 0.0, "sessions": 0})
        self.assertEqual(points["s3dp0"], {"usage_pct": 0.0, "sessions": 0})

    def test_ingress_backlogs_empty_output_zero_fills_known_dataplanes(self):
        for body in ("<result/>", "<result>none</result>"):
            points = parse_ingress_backlogs(ET.fromstring(body), ["dp1", "dp0"])
            self.assertEqual(
                points,
                [
                    ({"dataplane": "dp0"}, {"usage_pct": 0.0, "sessions": 0}),
                    ({"dataplane": "dp1"}, {"usage_pct": 0.0, "sessions": 0}),
                ],
            )
        self.assertEqual(parse_ingress_backlogs(ET.fromstring("<result/>")), [])

    def test_ingress_backlogs_xml_form(self):
        result = ET.fromstring(
            '<result><dp name="dp0"><entry><session-id>6</session-id><usage>55%</usage></entry>'
            "<entry><session-id>7</session-id><usage>12</usage></entry></dp>"
            '<dp name="dp1"/></result>'
        )
        points = dict((tags["dataplane"], fields) for tags, fields in parse_ingress_backlogs(result))
        self.assertEqual(points["dp0"], {"usage_pct": 55.0, "sessions": 2})
        self.assertEqual(points["dp1"], {"usage_pct": 0.0, "sessions": 0})

    def test_log_receiver_statistics(self):
        result = ET.fromstring(
            "<result>Log incoming rate: 12/sec\n"
            "Log written rate: 11.5/sec\n"
            "Log forwarded rate: 0/sec\n"
            "Traffic logs written: 99\n"
            "Total logs discarded: 42\n"
            "Logs discarded (queue full): 7\n"
            "Logs dropped by filter: 3\n</result>"
        )
        self.assertEqual(
            parse_log_receiver(result),
            {
                "log_incoming_rate": 12.0,
                "log_written_rate": 11.5,
                "log_forwarded_rate": 0.0,
                "total_logs_discarded": 42,
                "logs_discarded_queue_full": 7,
                "logs_dropped_by_filter": 3,
            },
        )
        long_label = "<result>" + "x" * 100 + " rate: 1/sec</result>"
        (name,) = parse_log_receiver(ET.fromstring(long_label))
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.endswith("_rate"))

    def test_globalprotect_statistics(self):
        result = ET.fromstring(
            "<result><Gateway><entry><name>gw-a</name><CurrentUsers>5</CurrentUsers>"
            "<PreviousUsers>2</PreviousUsers></entry><entry><name>gw b</name>"
            "<CurrentUsers>1</CurrentUsers><PreviousUsers>0</PreviousUsers></entry></Gateway>"
            "<TotalCurrentUsers>6</TotalCurrentUsers><TotalPreviousUsers>2</TotalPreviousUsers></result>"
        )
        self.assertEqual(
            parse_globalprotect(result),
            [
                ({}, {"current_users": 6, "previous_users": 2}),
                ({"gateway": "gw-a"}, {"current_users": 5, "previous_users": 2}),
                ({"gateway": "gw b"}, {"current_users": 1, "previous_users": 0}),
            ],
        )

    def test_software_status(self):
        result = ET.fromstring(
            "<result>Slot 1, Role mgmt\n"
            "-----------------\n"
            "Process: devsrvr    (pid: 1234)  running\n"
            "Process sysdagent      running    (pid: 3043)\n"
            "Process crashy         exited     (pid: 0)\n"
            "mgmtsrvr: running\n"
            "Role: mgmt\n</result>"
        )
        self.assertEqual(
            parse_software_status(result),
            [
                ({"process": "crashy"}, {"running": False, "status": "exited"}),
                ({"process": "devsrvr"}, {"running": True, "status": "running"}),
                ({"process": "mgmtsrvr"}, {"running": True, "status": "running"}),
                ({"process": "sysdagent"}, {"running": True, "status": "running"}),
            ],
        )

    def test_raid_detail(self):
        result = ET.fromstring(
            "<result>Disk Pair A                           Available\n"
            "    Status                     clean\n"
            "    Disk id A1                           Present\n"
            "        model        : ST1000NX0313\n"
            "        status       : active sync\n"
            "    Disk id A2                           Missing\n</result>"
        )
        self.assertEqual(
            parse_raid(result),
            [
                ({"disk": "disk_a1"}, {"status": "Present", "healthy": True}),
                ({"disk": "disk_a2"}, {"status": "Missing", "healthy": False}),
                ({"disk": "disk_pair_a"}, {"status": "Available", "healthy": True}),
                ({"disk": "disk_pair_a_array"}, {"status": "clean", "healthy": True}),
            ],
        )
        simple = parse_raid(ET.fromstring("<result>Status: optimal\nDisk1: OK\nDisk2: failed</result>"))
        self.assertEqual(
            simple,
            [
                ({"disk": "array"}, {"status": "optimal", "healthy": True}),
                ({"disk": "disk1"}, {"status": "OK", "healthy": True}),
                ({"disk": "disk2"}, {"status": "failed", "healthy": False}),
            ],
        )

    def test_raid_only_polled_on_highend_or_chassis(self):
        responses = {
            "PA-440": ET.fromstring("<result><system><model>PA-440</model></system></result>"),
            "PA-5220": ET.fromstring("<result><system><model>PA-5220</model></system></result>"),
        }
        raid = ET.fromstring("<result>Disk1: OK</result>")
        for model, expected_calls in (("PA-440", 1), ("PA-5220", 2)):
            config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
            with mock.patch(
                "telegraf.paloalto_api_collector.request_xml",
                side_effect=lambda _config, command, model=model: raid if "<raid>" in command else responses[model],
            ) as request:
                lines = collect_firewall(config, {"system", "raid"})
            self.assertEqual(request.call_count, expected_calls, model)
        self.assertTrue(config["_is_highend"])
        self.assertIn("paloalto_api_raid,disk=disk1,hostname=fw healthy=true,status=\"OK\"", lines)

    def test_unsupported_new_category_is_disabled_without_affecting_others(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        sessions = ET.fromstring("<result><num-active>5</num-active></result>")

        def response_for(_config, command):
            if "log-receiver" in command:
                raise ApiError("debug -> log-receiver is unexpected")
            if "global-protect" in command:
                raise ApiError("GlobalProtect gateway not configured")
            return sessions

        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml", side_effect=response_for
        ) as request, mock.patch("sys.stderr"):
            first = collect_firewall(config, {"logging", "globalprotect", "sessions"})
            second = collect_firewall(config, {"logging", "globalprotect", "sessions"})
        self.assertEqual(request.call_count, 4)
        self.assertEqual(config["_unsupported"], {"logging", "globalprotect"})
        self.assertIn("paloalto_api_sessions,hostname=fw sessions_active=5i", first)
        self.assertEqual(first, second)

    def test_transient_failure_does_not_disable_optional_category(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        with mock.patch(
            "telegraf.paloalto_api_collector.request_xml",
            side_effect=ApiError("request failed: timed out"),
        ), mock.patch("sys.stderr"):
            collect_firewall(config, {"software"})
        self.assertNotIn("software", config["_unsupported"])

    def test_schedules_are_the_single_source_of_truth(self):
        for category in ("ingress_backlogs", "logging", "globalprotect", "software", "raid"):
            self.assertIn(category, CATEGORY_SCHEDULES)
            self.assertIn(category, OPTIONAL_CATEGORIES)
        self.assertEqual(CATEGORY_SCHEDULES["raid"]({"system_interval": 900}), 900)
        self.assertEqual(CATEGORY_SCHEDULES["ingress_backlogs"]({}), 60)
        with mock.patch("telegraf.paloalto_api_collector.collect_firewall", return_value=[]) as collect:
            run_once([{"hostname": "fw"}])
        self.assertEqual(collect.call_args.args[1], set(CATEGORY_SCHEDULES))


class ErrorPathTests(unittest.TestCase):
    def test_result_error_status_raises_with_message(self):
        root = ET.fromstring(
            '<response status="error"><msg><line>show -> foo is unexpected</line></msg></response>'
        )
        with self.assertRaisesRegex(ApiError, "show -> foo is unexpected"):
            _result(root)

    def _urlopen_returning(self, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return mock.patch("urllib.request.urlopen", return_value=response)

    def test_request_xml_invalid_xml_raises_api_error(self):
        with mock.patch.dict(os.environ, {"PAN_KEY": "k"}, clear=True), self._urlopen_returning(b"<html><body>"):
            with self.assertRaisesRegex(ApiError, "invalid XML"):
                request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>")

    def test_request_xml_url_error_raises_api_error(self):
        with mock.patch.dict(os.environ, {"PAN_KEY": "k"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")
        ):
            with self.assertRaisesRegex(ApiError, "request failed: .*connection refused"):
                request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>")

    def test_request_xml_scopes_command_to_vsys(self):
        with mock.patch.dict(os.environ, {"PAN_KEY": "k"}, clear=True), self._urlopen_returning(
            b'<response status="success"><result><cps>3</cps></result></response>'
        ) as urlopen:
            request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>", vsys="vsys1")
            plain = request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>")
        scoped_body = urlopen.call_args_list[0].args[0].data.decode()
        plain_body = urlopen.call_args_list[1].args[0].data.decode()
        self.assertIn("vsys=vsys1", scoped_body)
        self.assertNotIn("vsys=", plain_body)
        self.assertEqual(plain.findtext("cps"), "3")

    def test_request_xml_http_error_keeps_pan_os_explanation(self):
        error = urllib.error.HTTPError(
            "https://192.0.2.1/api/", 400, "Bad Request", {}, io.BytesIO(b"You must specify a valid vsys")
        )
        with mock.patch.dict(os.environ, {"PAN_KEY": "k"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=error
        ):
            with self.assertRaisesRegex(ApiError, "HTTP 400: You must specify a valid vsys"):
                request_xml({"host": "192.0.2.1", "api_key_env": "PAN_KEY"}, "<show/>")


class VsysCollectionTests(unittest.TestCase):
    METER = ET.fromstring(
        "<result>"
        "<entry><vsys>1</vsys><maximum>0</maximum><current>402</current><throttled>0</throttled></entry>"
        "<entry><vsys>2</vsys><maximum>0</maximum><current>0</current><throttled>0</throttled></entry>"
        "</result>"
    )
    SCOPED = ET.fromstring(
        "<result><cps>8</cps><pps>204</pps><num-active>351</num-active><num-max>200000</num-max>"
        "<num-tcp>218</num-tcp><num-udp>101</num-udp><num-icmp>32</num-icmp></result>"
    )

    def _request(self, calls):
        def request(_config, command, vsys=None):
            calls.append((command, vsys))
            if "<meter>" in command:
                return self.METER
            if vsys == "vsys2":
                raise ApiError("request failed: HTTP 400: You must specify a valid vsys")
            return self.SCOPED

        return request

    def test_unconfigured_vsys_slots_are_dropped_and_cps_is_added(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        calls = []
        with mock.patch("telegraf.paloalto_api_collector.request_xml", side_effect=self._request(calls)):
            lines = collect_firewall(config, {"vsys"})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("paloalto_api_vsys,hostname=fw,vsys=vsys1 "))
        for field in ("sessions_active=402i", "cps=8i", "packet_rate_pps=204i", "sessions_tcp=218i", "sessions_udp=101i", "sessions_icmp=32i"):
            self.assertIn(field, lines[0])
        # The meter keeps the DP-summed session count; num-max of the scoped
        # output is the platform limit, not a VSYS limit.
        self.assertNotIn("sessions_active=351i", lines[0])
        self.assertNotIn("sessions_max", lines[0])
        self.assertIn("vsys2", config["_missing_vsys"])
        self.assertEqual([vsys for _, vsys in calls], [None, "vsys1", "vsys2"])

    def test_missing_vsys_is_probed_again_only_after_system_interval(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY", "system_interval": 3600}
        calls = []
        with mock.patch("telegraf.paloalto_api_collector.request_xml", side_effect=self._request(calls)):
            collect_firewall(config, {"vsys"})
            collect_firewall(config, {"vsys"})
            self.assertEqual([vsys for _, vsys in calls].count("vsys2"), 1)
            config["_missing_vsys"]["vsys2"] -= 3601
            collect_firewall(config, {"vsys"})
        self.assertEqual([vsys for _, vsys in calls].count("vsys2"), 2)

    def test_scoped_session_info_is_disabled_when_unsupported_but_meter_survives(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}
        calls = []

        def request(_config, command, vsys=None):
            calls.append(vsys)
            if vsys:
                raise ApiError("You are not authorized to perform this operation")
            return self.METER

        with mock.patch("telegraf.paloalto_api_collector.request_xml", side_effect=request), mock.patch("sys.stderr"):
            first = collect_firewall(config, {"vsys"})
            second = collect_firewall(config, {"vsys"})
        self.assertEqual(len(first), 2)
        self.assertIn("sessions_active=402i", first[0])
        self.assertNotIn("cps=", first[0])
        self.assertIn("vsys_sessions", config["_unsupported"])
        self.assertEqual(calls, [None, "vsys1", None])
        self.assertEqual(len(second), 2)

    def test_transient_scoped_failure_keeps_the_vsys_point(self):
        config = {"hostname": "fw", "host": "192.0.2.1", "api_key_env": "KEY"}

        def request(_config, command, vsys=None):
            if vsys:
                raise ApiError("request failed: timed out")
            return self.METER

        with mock.patch("telegraf.paloalto_api_collector.request_xml", side_effect=request), mock.patch("sys.stderr"):
            lines = collect_firewall(config, {"vsys"})
        self.assertEqual(len(lines), 2)
        self.assertNotIn("vsys_sessions", config["_unsupported"])
        self.assertEqual(config["_missing_vsys"], {})


if __name__ == "__main__":
    unittest.main()
