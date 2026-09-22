"""Rendering tests for the Telegraf SNMP templates.

The templates are rendered through generate.render_template() for a small
inventory covering SNMPv2c/SNMPv3, Palo Alto and Fortinet, and optional
feature flags, then parsed as TOML so structural mistakes are caught without
a Telegraf binary or a reachable SNMP agent.
"""

import copy
import tomllib
import unittest

import generate


BASE_INVENTORY = [
    {
        "hostname": "PA-V2",
        "host": "192.0.2.10",
        "vendor": "paloalto",
        "snmp_version": 2,
        "community": "placeholder-community",
        "model": "PA-440",
        "panos_10_2_metrics": False,
        "panos_11_2_metrics": False,
        "panos_12_metrics": False,
        "vsys_total_cps": False,
        "interface_utilization": False,
        "chassis": False,
        "pan_entity_ext": False,
        "pa_cluster": False,
    },
    {
        "hostname": "PA-V3-CHASSIS",
        "host": "192.0.2.11",
        "vendor": "paloalto",
        "snmp_version": 3,
        "username": "placeholder-user",
        "auth_protocol": "sha256",
        "auth_password": "placeholder-auth",
        "priv_protocol": "aes256",
        "priv_password": "placeholder-priv",
        "model": "PA-7080",
        "cluster": "dc1",
        "panos_10_2_metrics": True,
        "panos_11_2_metrics": True,
        "panos_12_metrics": True,
        "vsys_total_cps": True,
        "interface_utilization": True,
        "chassis": True,
        "pan_entity_ext": True,
        "pa_cluster": True,
    },
    {
        "hostname": "FGT-V2",
        "host": "192.0.2.20",
        "vendor": "fortinet",
        "snmp_version": 2,
        "community": "placeholder-community",
    },
    {
        "hostname": "FGT-V3",
        "host": "192.0.2.21",
        "vendor": "fortinet",
        "snmp_version": 3,
        "username": "placeholder-user",
        "auth_protocol": "sha256",
        "auth_password": "placeholder-auth",
        "priv_protocol": "aes256",
        "priv_password": "placeholder-priv",
        "model": "FortiGate-100F",
        "cluster": "branch",
    },
]

EXPECTED_MEASUREMENTS = [
    "pan_system",
    "interfaces",
    "pan_hr_processors",
    "pan_hr_storage",
    "pan_hr_devices",
    "vsys",
    "pan_zones",
    "pan_global_counters",
    "pan_entity_physical",
    "pan_entity_sensors",
    "pan_entity_states",
    "fortinet_system",
    "fortinet_processors",
    "fortinet_vdoms",
    "fortinet_hw_sensors",
    "fortinet_ha_members",
]

FAST_PALO_TABLES = {"interfaces", "pan_hr_processors", "vsys", "pan_zones", "pan_interfaces_cps", "pan_interface_utilization"}
SLOW_PALO_TABLES = {
    "pan_hr_storage",
    "pan_hr_devices",
    "pan_pa_cluster",
    "pan_entity_physical",
    "pan_entity_sensors",
    "pan_entity_states",
    "pan_entity_fru_modules",
    "pan_entity_fan_trays",
    "pan_entity_power_supplies",
}
FAST_FORTINET_TABLES = {"interfaces", "fortinet_processors"}
SLOW_FORTINET_TABLES = {"fortinet_vdoms", "fortinet_hw_sensors", "fortinet_ha_members"}


def build_inventory():
    firewalls = copy.deepcopy(BASE_INVENTORY)
    generate.validate_inventory(firewalls)
    generate.enrich_inventory(firewalls)
    return firewalls


def render_all(firewalls):
    context = {"firewalls": firewalls}
    return {
        "header": generate.render_template("header.tmpl", context),
        "paloalto": generate.render_template("inputs_paloalto.tmpl", context),
        "fortinet": generate.render_template("inputs_fortinet.tmpl", context),
    }


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


def table_names(instance):
    return [table["name"] for table in instance.get("table", [])]


def field_names(fields):
    return [field["name"] for field in fields]


class SnmpTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.firewalls = build_inventory()
        cls.rendered = render_all(cls.firewalls)
        cls.full_text = "\n".join(cls.rendered.values())
        cls.config = tomllib.loads(cls.full_text)
        cls.instances = cls.config["inputs"]["snmp"]

    def instances_for(self, host):
        return [item for item in self.instances if item["agents"] == [f"udp://{host}:161"]]

    def test_rendered_config_parses_as_toml(self):
        self.assertIn("agent", self.config)
        self.assertIn("influxdb_v2", self.config["outputs"])
        for part in ("paloalto", "fortinet"):
            tomllib.loads(self.rendered[part])

    def test_templates_use_plain_jinja(self):
        for name in ("inputs_paloalto.tmpl", "inputs_fortinet.tmpl"):
            text = (generate.PROJECT_DIR / "telegraf" / name).read_text(encoding="utf-8")
            self.assertNotIn("$fw", text, name)
            self.assertNotIn("{{-", text, name)

    def test_two_instances_per_firewall(self):
        self.assertEqual(len(self.instances), 2 * len(self.firewalls))
        self.assertEqual(self.full_text.count("[[inputs.snmp]]"), 8)
        for firewall in self.firewalls:
            fast, slow = self.instances_for(firewall["host"])
            self.assertNotIn("interval", fast, firewall["hostname"])
            self.assertEqual(slow["interval"], "60s", firewall["hostname"])
            self.assertEqual(fast["tags"], slow["tags"], firewall["hostname"])
            for key in ("version", "agent_host_tag", "community", "sec_name", "auth_protocol",
                        "auth_password", "priv_protocol", "priv_password", "sec_level"):
                self.assertEqual(fast.get(key), slow.get(key), f"{firewall['hostname']} {key}")
            for instance in (fast, slow):
                self.assertEqual(instance["max_repetitions"], 25)
                hostname_field = instance["field"][0]
                self.assertEqual(hostname_field["oid"], "RFC1213-MIB::sysName.0")
                self.assertEqual(hostname_field["name"], "hostname")
                self.assertTrue(hostname_field["is_tag"])
                for table in instance.get("table", []):
                    self.assertEqual(table["inherit_tags"], ["hostname"], table["name"])

    def test_credentials_for_v2c_and_v3(self):
        self.assertIn('community = "placeholder-community"', self.full_text)
        self.assertIn('sec_name = "placeholder-user"', self.full_text)
        self.assertIn('auth_protocol = "SHA256"', self.full_text)
        self.assertIn('priv_protocol = "AES256"', self.full_text)
        self.assertIn('sec_level = "authPriv"', self.full_text)
        for firewall in self.firewalls:
            for instance in self.instances_for(firewall["host"]):
                self.assertEqual(instance["version"], firewall["snmp_version"])
                if firewall["snmp_version"] == 2:
                    self.assertIn("community", instance)
                    self.assertNotIn("sec_name", instance)
                else:
                    self.assertNotIn("community", instance)
                    self.assertEqual(instance["sec_level"], "authPriv")

    def test_expected_measurements_are_present(self):
        measurements = set()
        for instance in self.instances:
            measurements.add(instance["name"])
            measurements.update(table_names(instance))
        for measurement in EXPECTED_MEASUREMENTS:
            self.assertIn(measurement, measurements)

    def test_palo_layout(self):
        for host in ("192.0.2.10", "192.0.2.11"):
            fast, slow = self.instances_for(host)
            self.assertEqual(fast["name"], "pan_system")
            self.assertEqual(slow["name"], "pan_global_counters")
            self.assertLessEqual(set(table_names(fast)), FAST_PALO_TABLES)
            self.assertLessEqual(set(table_names(slow)), SLOW_PALO_TABLES)
            self.assertIn("sessions_active", field_names(fast["field"]))
            self.assertIn("ram_used_kb", field_names(fast["field"]))
            counters = field_names(slow["field"])[1:]
            self.assertGreaterEqual(len(counters), 90)
            self.assertIn("panFlowPolicyDeny", counters)
            for field in slow["field"][1:]:
                self.assertTrue(field["oid"].endswith(".0"), field["oid"])
                self.assertEqual(field["oid"], f"PAN-COMMON-MIB::{field['name']}.0")

    def test_palo_flags_gate_blocks(self):
        plain_fast, plain_slow = self.instances_for("192.0.2.10")
        full_fast, full_slow = self.instances_for("192.0.2.11")

        self.assertNotIn("pan_interfaces_cps", table_names(plain_fast))
        self.assertIn("pan_interfaces_cps", table_names(full_fast))
        self.assertNotIn("pan_interface_utilization", table_names(plain_fast))
        self.assertIn("pan_interface_utilization", table_names(full_fast))
        self.assertNotIn("pan_pa_cluster", table_names(plain_slow))
        self.assertIn("pan_pa_cluster", table_names(full_slow))
        for table in ("pan_entity_physical", "pan_entity_sensors", "pan_entity_states", "pan_entity_power_supplies"):
            self.assertNotIn(table, table_names(plain_slow))
            self.assertIn(table, table_names(full_slow))

        def table(instance, name):
            return next(item for item in instance["table"] if item["name"] == name)

        self.assertNotIn("storage_usage_pct", field_names(table(plain_slow, "pan_hr_storage")["field"]))
        self.assertIn("storage_usage_pct", field_names(table(full_slow, "pan_hr_storage")["field"]))
        self.assertNotIn("total_cps", field_names(table(plain_fast, "vsys")["field"]))
        self.assertIn("total_cps", field_names(table(full_fast, "vsys")["field"]))
        self.assertNotIn("power_used_watts", field_names(plain_fast["field"]))
        self.assertIn("power_used_watts", field_names(full_fast["field"]))

        for name in ("pan_entity_sensors", "pan_entity_states"):
            entity_name = next(f for f in table(full_slow, name)["field"] if f["name"] == "entity_name")
            self.assertEqual(entity_name["oid"], "ENTITY-MIB::entPhysicalName")
            self.assertTrue(entity_name["is_tag"])

    def test_pa_cluster_is_independent_of_panos_version(self):
        firewalls = copy.deepcopy(BASE_INVENTORY[:1])
        firewalls[0].update(panos_10_2_metrics=True, panos_11_2_metrics=True, panos_12_metrics=True)
        generate.validate_inventory(firewalls)
        generate.enrich_inventory(firewalls)
        text = generate.render_template("inputs_paloalto.tmpl", {"firewalls": firewalls})
        self.assertNotIn("pan_pa_cluster", text)
        self.assertIn("PAN-COMMON-MIB::panIfTable", text)
        self.assertIn("PAN-COMMON-MIB::panhrStorageUsage", text)

    def test_fortinet_layout(self):
        for host in ("192.0.2.20", "192.0.2.21"):
            fast, slow = self.instances_for(host)
            self.assertEqual(fast["name"], "fortinet_system")
            self.assertEqual(set(table_names(fast)), FAST_FORTINET_TABLES)
            self.assertEqual(set(table_names(slow)), SLOW_FORTINET_TABLES)
            self.assertEqual(field_names(slow["field"]), ["hostname"])
            for name in ("cpu_pct", "mem_pct", "sessions_active", "ha_mode"):
                self.assertIn(name, field_names(fast["field"]))

            ha_mode = next(f for f in fast["field"] if f["name"] == "ha_mode")
            self.assertEqual(ha_mode["oid"], "FORTINET-FORTIGATE-MIB::fgHaSystemMode.0")

            interfaces = next(t for t in fast["table"] if t["name"] == "interfaces")
            for field in interfaces["field"]:
                self.assertIn("name", field, field["oid"])
            tags = {f["name"] for f in interfaces["field"] if f.get("is_tag")}
            self.assertLessEqual({"ifName", "ifDescr"}, tags)

            sensors = next(t for t in slow["table"] if t["name"] == "fortinet_hw_sensors")
            value = next(f for f in sensors["field"] if f["name"] == "value")
            self.assertEqual(value["conversion"], "float")

    def test_no_data_type_and_no_raw_fortinet_oid(self):
        self.assertNotIn("data_type", set(walk_keys(self.config)))
        self.assertNotIn("data_type", self.full_text)
        self.assertNotIn(".1.3.6.1.4.1.12356.101.13.1.1.0", self.full_text)

    def test_max_repetitions_set(self):
        self.assertEqual(self.full_text.count("max_repetitions = 25"), len(self.instances))


if __name__ == "__main__":
    unittest.main()
