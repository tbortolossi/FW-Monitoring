import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate


class GeneratorCoreTests(unittest.TestCase):
    def test_protocol_and_vendor_normalization(self):
        self.assertEqual(generate.normalize_vendor(None), "paloalto")
        self.assertEqual(generate.normalize_vendor("PANOS"), "paloalto")
        self.assertEqual(generate.normalize_vendor("fortigate"), "fortinet")
        self.assertEqual(generate.normalize_snmp_auth("sha256"), "SHA-256")
        self.assertEqual(generate.normalize_snmp_priv("aes256"), "AES-256")
        self.assertEqual(generate.normalize_telegraf_snmp_auth("sha256"), "SHA256")
        self.assertEqual(generate.normalize_telegraf_snmp_priv("aes192"), "AES192")

    def test_boolean_normalization_and_invalid_value(self):
        self.assertTrue(generate.normalize_boolean("yes"))
        self.assertFalse(generate.normalize_boolean("off", default=True))
        self.assertTrue(generate.normalize_boolean(None, default=True))
        with self.assertRaisesRegex(ValueError, "expected a boolean"):
            generate.normalize_boolean("sometimes")

    def test_version_and_chassis_helpers(self):
        self.assertEqual(generate.version_tuple("PAN-OS 12.1.3"), (12, 1))
        self.assertIsNone(generate.version_tuple("unknown"))
        self.assertTrue(generate.pan_at_least("12.1.3", 12, 1))
        self.assertFalse(generate.pan_at_least("11.2.4", 12, 1))
        self.assertTrue(generate.is_palo_chassis("PA-7500"))
        self.assertFalse(generate.is_palo_chassis("PA-5580"))
        self.assertEqual(generate.chassis_family("PA-7080"), "pa7000")
        self.assertEqual(generate.chassis_family("PA-5450"), "pa5400")
        self.assertEqual(generate.chassis_family("PA-7500"), "pa7500")

    def test_inventory_validation_accepts_v2_and_v3(self):
        firewalls = [
            {"hostname": "PA", "host": "192.0.2.1", "community": "public"},
            {
                "hostname": "FGT",
                "host": "192.0.2.2",
                "vendor": "fortios",
                "snmp_version": "3",
                "username": "monitor",
                "auth_password": "auth",
                "priv_password": "priv",
                "auth_protocol": "sha256",
                "priv_protocol": "aes256",
            },
        ]
        generate.validate_inventory(firewalls)
        self.assertEqual(firewalls[0]["vendor"], "paloalto")
        self.assertFalse(firewalls[0]["api_monitoring"]["enabled"])
        self.assertEqual(firewalls[1]["vendor"], "fortinet")
        self.assertEqual(firewalls[1]["telegraf_auth_protocol"], "SHA256")
        self.assertEqual(firewalls[1]["telegraf_priv_protocol"], "AES256")

    def test_inventory_validation_rejects_invalid_entries(self):
        invalid = (
            ([{"hostname": "FW", "host": "192.0.2.1", "vendor": "unknown", "community": "x"}], "unsupported vendor"),
            ([{"host": "192.0.2.1", "community": "x"}], "hostname is required"),
            ([{"hostname": "FW", "community": "x"}], "host is required"),
            ([{"hostname": "FW", "host": "192.0.2.1", "snmp_version": 2}], "community is required"),
            ([{"hostname": "FW", "host": "192.0.2.1", "snmp_version": 3}], "username is required"),
            ([{"hostname": "FW", "host": "192.0.2.1", "snmp_version": 4}], "must be 2 or 3"),
        )
        for firewalls, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(SystemExit, message):
                generate.validate_inventory(firewalls)

    def test_inventory_enrichment_sets_feature_flags(self):
        firewalls = [
            {"hostname": "PA-7500", "vendor": "paloalto", "panos_version": "12.1.2", "model": "PA-7500"},
            {"hostname": "FGT", "vendor": "fortinet", "fortios_version": "7.6"},
        ]
        generate.enrich_inventory(firewalls)
        palo = firewalls[0]
        self.assertTrue(palo["panos_11_2_metrics"])
        self.assertTrue(palo["panos_12_metrics"])
        self.assertTrue(palo["vsys_total_cps"])
        self.assertTrue(palo["interface_utilization"])
        self.assertTrue(palo["chassis"])
        self.assertEqual(palo["chassis_family"], "pa7500")
        self.assertNotIn("chassis", firewalls[1])

    def test_snmp_arguments_cover_v2_v3_security_levels(self):
        self.assertEqual(
            generate.build_snmp_args({"snmp_version": 2, "community": "community"}),
            ["-v2c", "-c", "community"],
        )
        auth_only = generate.build_snmp_args({
            "snmp_version": 3,
            "username": "user",
            "auth_protocol": "sha256",
            "auth_password": "auth",
        })
        self.assertIn("authNoPriv", auth_only)
        auth_priv = generate.build_snmp_args({
            "snmp_version": 3,
            "username": "user",
            "auth_protocol": "sha256",
            "auth_password": "auth",
            "priv_protocol": "aes256",
            "priv_password": "priv",
        })
        self.assertIn("authPriv", auth_priv)
        self.assertIn("AES-256", auth_priv)

    def test_model_detection(self):
        self.assertEqual(generate.detect_palo_model("panPA-5580"), "PA-5580")
        self.assertEqual(generate.detect_palo_model("Palo Alto PA440 firewall"), "PA-440")
        self.assertEqual(generate.detect_palo_model("unrelated"), "")
        self.assertEqual(generate.detect_fortinet_model("FortiGate 80F appliance"), "FortiGate-80F")
        self.assertEqual(generate.detect_fortinet_model("FGT-100F"), "FGT-100F")
        self.assertEqual(generate.detect_fortinet_model("unrelated"), "")

    def test_paloalto_discovery_enriches_inventory(self):
        firewall = {
            "hostname": "PA",
            "host": "192.0.2.1",
            "vendor": "paloalto",
            "snmp_version": 2,
            "community": "community",
        }
        values = {
            ".1.3.6.1.2.1.1.1.0": "Palo Alto PA-440 firewall",
            ".1.3.6.1.2.1.1.2.0": "panPA440",
            ".1.3.6.1.4.1.25461.2.1.2.1.1.0": "12.1.3",
            ".1.3.6.1.4.1.25461.2.1.2.1.3.0": "SERIAL",
        }
        with mock.patch.object(generate, "SNMP_DISCOVERY", "true"), mock.patch.object(
            generate, "ensure_snmp_image", return_value=True
        ), mock.patch.object(generate, "snmp_get", side_effect=lambda _host, oid, _args: values[oid]), mock.patch.object(
            generate, "snmp_walk_first", return_value="vsys1"
        ):
            generate.discover_paloalto_devices([firewall], {"paloalto"})
        self.assertTrue(firewall["discovered"])
        self.assertEqual(firewall["model"], "PA-440")
        self.assertEqual(firewall["panos_version"], "12.1.3")
        self.assertTrue(firewall["vsys_detected"])

    def test_fortinet_discovery_enriches_inventory(self):
        firewall = {
            "hostname": "FGT",
            "host": "192.0.2.2",
            "vendor": "fortinet",
            "snmp_version": 2,
            "community": "community",
        }
        values = {
            ".1.3.6.1.2.1.1.1.0": "FortiGate-80F appliance",
            ".1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.12356",
            ".1.3.6.1.4.1.12356.101.4.1.1.0": "7.6.5",
            ".1.3.6.1.4.1.12356.100.1.1.1.0": "SERIAL",
        }
        with mock.patch.object(generate, "SNMP_DISCOVERY", "true"), mock.patch.object(
            generate, "ensure_snmp_image", return_value=True
        ), mock.patch.object(generate, "snmp_get", side_effect=lambda _host, oid, _args: values[oid]), mock.patch.object(
            generate, "snmp_walk_first", return_value="root"
        ):
            generate.discover_fortinet_devices([firewall], {"fortinet"})
        self.assertTrue(firewall["discovered"])
        self.assertEqual(firewall["model"], "FortiGate-80F")
        self.assertEqual(firewall["fortios_version"], "7.6.5")
        self.assertTrue(firewall["vdom_detected"])

    def test_discovery_can_be_disabled(self):
        with mock.patch.object(generate, "SNMP_DISCOVERY", "false"), mock.patch.object(
            generate, "ensure_snmp_image"
        ) as image:
            generate.discover_paloalto_devices([], {"paloalto"})
            generate.discover_fortinet_devices([], {"fortinet"})
        image.assert_not_called()

    def test_legacy_template_conversion_and_errors(self):
        template = (
            '{{ range $fw := .firewalls }}\n'
            'name = "{{ $fw.hostname }}"\n'
            '{{ if eq $fw.snmp_version 2 }}\nversion = 2\n{{ end }}\n'
            '{{ end }}\n'
        )
        converted = generate.convert_legacy_template(template)
        self.assertIn("{% for fw in firewalls %}", converted)
        self.assertIn("{{ fw.hostname }}", converted)
        self.assertIn("{% endif %}", converted)
        self.assertIn("{% endfor %}", converted)
        with self.assertRaisesRegex(SystemExit, "unmatched end"):
            generate.convert_legacy_template("{{ end }}")
        with self.assertRaisesRegex(SystemExit, "unclosed block"):
            generate.convert_legacy_template("{{ range $fw := .firewalls }}")

    def test_render_telegraf_selects_vendor_and_api_templates(self):
        firewalls = [
            {"vendor": "paloalto", "api_monitoring": {"enabled": True}},
            {"vendor": "fortinet"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telegraf").mkdir()
            with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                generate, "render_template", side_effect=lambda name, _context: name
            ) as render:
                generate.render_telegraf(firewalls, {"paloalto", "fortinet"})
            content = (root / "telegraf" / "telegraf.conf").read_text()
            mode = (root / "telegraf" / "telegraf.conf").stat().st_mode & 0o777
        self.assertIn("header.tmpl", content)
        self.assertIn("inputs_paloalto.tmpl", content)
        self.assertIn("inputs_fortinet.tmpl", content)
        self.assertIn("inputs_paloalto_api.tmpl", content)
        self.assertEqual(render.call_count, 4)
        self.assertEqual(mode, 0o644)

    def test_dotenv_parser_handles_comments_quotes_and_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\nPLAIN=value\nQUOTED='two words'\nTOKEN=part=two\nINVALID\n",
                encoding="utf-8",
            )
            self.assertEqual(
                generate.load_dotenv(path),
                {"PLAIN": "value", "QUOTED": "two words", "TOKEN": "part=two"},
            )

    def test_check_env_file_restricts_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("TOKEN=secret\n", encoding="utf-8")
            env_path.chmod(0o644)
            with mock.patch.object(generate, "PROJECT_DIR", Path(directory)):
                generate.check_env_file()
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_stack_start_uses_repeatable_compose_commands(self):
        with mock.patch.object(generate, "run") as run:
            generate.start_stack()
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["docker", "compose", "build", "telegraf"],
                ["docker", "compose", "up", "-d"],
                ["docker", "compose", "ps"],
            ],
        )

    def test_main_orchestrates_generation_in_order(self):
        firewalls = [{"hostname": "PA", "vendor": "paloalto"}]
        patch_names = (
            "check_docker",
            "check_env_file",
            "prepare_runtime_dirs",
            "validate_inventory",
            "render_paloalto_api_environment",
            "enrich_inventory",
            "discover_paloalto_devices",
            "discover_fortinet_devices",
            "save_inventory",
            "prepare_paloalto_mibs",
            "render_paloalto_api_inventory",
            "render_telegraf",
            "start_stack",
        )
        patches = [mock.patch.object(generate, name) for name in patch_names]
        mocks = [patch.start() for patch in patches]
        self.addCleanup(lambda: [patch.stop() for patch in reversed(patches)])
        with mock.patch.object(generate, "load_inventory", return_value=firewalls), mock.patch.object(
            generate, "detect_vendors", return_value=["paloalto"]
        ):
            generate.main()
        for mocked in mocks:
            mocked.assert_called()

    def test_check_docker_reports_missing_binary(self):
        with mock.patch.object(generate, "command_exists", return_value=False):
            with self.assertRaisesRegex(SystemExit, "Docker is required"):
                generate.check_docker()

    def test_capture_propagates_command_failure(self):
        with self.assertRaises(subprocess.CalledProcessError):
            generate.capture(["sh", "-c", "exit 7"])


if __name__ == "__main__":
    unittest.main()
