import os
import stat
import subprocess
import sys
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

    def test_inventory_enrichment_preserves_explicit_metric_overrides(self):
        firewalls = [{
            "hostname": "PA-440",
            "vendor": "paloalto",
            "panos_version": "12.2.3",
            "panos_11_2_metrics": False,
            "interface_utilization": False,
        }]
        generate.enrich_inventory(firewalls)
        palo = firewalls[0]
        self.assertFalse(palo["panos_11_2_metrics"])
        self.assertFalse(palo["interface_utilization"])
        self.assertTrue(palo["panos_12_metrics"])
        self.assertTrue(palo["vsys_total_cps"])

        rendered = generate.render_template("inputs_paloalto.tmpl", {"firewalls": firewalls})
        self.assertNotIn('name         = "pan_pa_cluster"', rendered)
        self.assertNotIn('name         = "pan_interface_utilization"', rendered)
        self.assertNotIn('name = "storage_usage_pct"', rendered)
        self.assertIn('name = "total_cps"', rendered)

    def test_inventory_enrichment_recomputes_inferred_chassis_after_discovery(self):
        firewall = {"hostname": "FW-CORE-01", "vendor": "paloalto"}
        generate.enrich_inventory([firewall])
        self.assertFalse(firewall["chassis"])
        self.assertFalse(firewall["pan_entity_ext"])
        firewall["model"] = "PA-7050"
        firewall["panos_version"] = "11.1.4"
        generate.enrich_inventory([firewall])
        self.assertIs(firewall["chassis"], True)
        self.assertIs(firewall["pan_entity_ext"], True)
        self.assertEqual(firewall["chassis_family"], "pa7000")
        self.assertTrue(firewall["panos_10_2_metrics"])
        self.assertFalse(firewall["panos_11_2_metrics"])

    def test_inventory_enrichment_keeps_operator_chassis_override_after_discovery(self):
        firewall = {"hostname": "FW-CORE-01", "vendor": "paloalto", "chassis": False}
        generate.enrich_inventory([firewall])
        firewall["model"] = "PA-7050"
        generate.enrich_inventory([firewall])
        self.assertIs(firewall["chassis"], False)
        self.assertIs(firewall["pan_entity_ext"], False)

    def test_inventory_enrichment_recomputes_version_flags_after_discovery(self):
        firewall = {"hostname": "PA", "vendor": "paloalto", "panos_version": "10.1.0", "panos_12_metrics": True}
        generate.enrich_inventory([firewall])
        self.assertFalse(firewall["panos_10_2_metrics"])
        firewall["panos_version"] = "12.1.2"
        generate.enrich_inventory([firewall])
        self.assertTrue(firewall["panos_10_2_metrics"])
        self.assertTrue(firewall["panos_11_2_metrics"])
        self.assertTrue(firewall["panos_12_metrics"])

    def test_panos_10_2_flag_and_pa_cluster_defaults(self):
        firewalls = [
            {"hostname": "OLD", "vendor": "paloalto", "panos_version": "10.1.9"},
            {"hostname": "NEW", "vendor": "paloalto", "panos_version": "10.2.0"},
            {"hostname": "OVR", "vendor": "paloalto", "panos_version": "11.2.1", "panos_10_2_metrics": False},
            {"hostname": "CL", "vendor": "paloalto", "panos_version": "11.2.1", "pa_cluster": "yes"},
            {"hostname": "NOVER", "vendor": "paloalto"},
            {"hostname": "FGT", "vendor": "fortinet"},
        ]
        generate.enrich_inventory(firewalls)
        self.assertFalse(firewalls[0]["panos_10_2_metrics"])
        self.assertTrue(firewalls[1]["panos_10_2_metrics"])
        self.assertFalse(firewalls[2]["panos_10_2_metrics"])
        self.assertIs(firewalls[0]["pa_cluster"], False)
        self.assertIs(firewalls[1]["pa_cluster"], False)
        self.assertIs(firewalls[3]["pa_cluster"], True)
        self.assertNotIn("panos_10_2_metrics", firewalls[4])
        self.assertIs(firewalls[4]["pa_cluster"], False)
        self.assertNotIn("pa_cluster", firewalls[5])
        with self.assertRaisesRegex(SystemExit, "pa_cluster"):
            generate.enrich_inventory([{"hostname": "BAD", "vendor": "paloalto", "pa_cluster": "maybe"}])

    def test_saved_inventory_omits_private_bookkeeping(self):
        firewall = {"hostname": "FW-CORE-01", "vendor": "paloalto"}
        generate.enrich_inventory([firewall])
        self.assertIn(generate.INFERRED_KEYS_FIELD, firewall)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "generated.yml"
            generate.save_inventory([firewall], destination)
            content = destination.read_text(encoding="utf-8")
        self.assertNotIn("_inferred_keys", content)
        self.assertIn("chassis: false", content)

    def test_snmp_conf_v2c_quotes_community(self):
        conf = generate.build_snmp_conf({"snmp_version": 2, "community": 'a b"c\\d$e'})
        self.assertEqual(conf, 'defVersion 2c\ndefCommunity "a b\\"c\\\\d$e"\n')

    def test_snmp_conf_v3_auth_priv_and_auth_no_priv(self):
        conf = generate.build_snmp_conf({
            "snmp_version": 3,
            "username": "monitor",
            "auth_protocol": "sha256",
            "auth_password": "auth secret",
            "priv_protocol": "aes256",
            "priv_password": "priv\\secret",
        })
        self.assertEqual(
            conf.splitlines(),
            [
                "defVersion 3",
                "defSecurityLevel authPriv",
                'defSecurityName "monitor"',
                "defAuthType SHA-256",
                'defAuthPassphrase "auth secret"',
                "defPrivType AES-256",
                'defPrivPassphrase "priv\\\\secret"',
            ],
        )
        auth_only = generate.build_snmp_conf({
            "snmp_version": 3, "username": "u", "auth_protocol": "sha", "auth_password": "a",
        })
        self.assertIn("defSecurityLevel authNoPriv", auth_only)
        self.assertNotIn("defPriv", auth_only)

    def test_snmp_conf_rejects_line_breaks_and_injected_protocols(self):
        with self.assertRaises(SystemExit) as raised:
            generate.build_snmp_conf({"snmp_version": 2, "community": "top\ndefVersion 1"})
        self.assertIn("community", str(raised.exception))
        self.assertNotIn("top", str(raised.exception))
        with self.assertRaisesRegex(SystemExit, "auth_protocol"):
            generate.build_snmp_conf({
                "snmp_version": 3, "username": "u", "auth_password": "a", "priv_password": "p",
                "auth_protocol": "SHA\ndefVersion 1",
            })

    def test_snmp_discovery_keeps_secrets_off_the_command_line(self):
        firewall = {
            "snmp_version": 3,
            "username": "monitor",
            "auth_protocol": "sha256",
            "auth_password": "AuthSecret-1",
            "priv_protocol": "aes256",
            "priv_password": "PrivSecret-2",
        }
        conf = generate.build_snmp_conf(firewall)
        results = [
            subprocess.CompletedProcess([], 0, stdout='"PA-440 firewall"\n', stderr=""),
            subprocess.CompletedProcess([], 0, stdout='"vsys1"\n"vsys2"\n', stderr=""),
        ]
        with mock.patch.object(generate, "SNMP_IMAGE", "image-id"), mock.patch.object(
            generate.subprocess, "run", side_effect=results
        ) as run:
            self.assertEqual(generate.snmp_get("192.0.2.1", ".1.3.6.1.2.1.1.1.0", conf), "PA-440 firewall")
            self.assertEqual(generate.snmp_walk_first("192.0.2.1", ".1.3.6.1.2", conf), "vsys1")
        self.assertEqual(run.call_count, 2)
        for call, tool, oid in zip(run.call_args_list, ("snmpget", "snmpwalk"), (".1.3.6.1.2.1.1.1.0", ".1.3.6.1.2")):
            argv = call.args[0]
            joined = " ".join(argv)
            self.assertNotIn("AuthSecret-1", joined)
            self.assertNotIn("PrivSecret-2", joined)
            self.assertNotIn("monitor", joined)
            self.assertIn("AuthSecret-1", call.kwargs["input"])
            self.assertIn("PrivSecret-2", call.kwargs["input"])
            self.assertEqual(argv[:4], ["docker", "run", "-i", "--rm"])
            self.assertIn("SNMPCONFPATH=/tmp/snmp", argv)
            self.assertIn(tool, argv)
            self.assertEqual(argv[-2:], ["192.0.2.1", oid])
            timeout_index = argv.index("-t")
            self.assertEqual(argv[timeout_index + 1], str(generate.SNMP_DISCOVERY_TIMEOUT))
            self.assertEqual(argv[argv.index("-r") + 1], str(generate.SNMP_DISCOVERY_RETRIES))

    def test_snmp_discovery_without_image_does_not_run_docker(self):
        with mock.patch.object(generate, "SNMP_IMAGE", ""), mock.patch.object(generate.subprocess, "run") as run:
            self.assertEqual(generate.snmp_get("192.0.2.1", ".1", "conf"), "")
            self.assertEqual(generate.snmp_walk_first("192.0.2.1", ".1", "conf"), "")
        run.assert_not_called()

    def test_snmp_discovery_timeout_environment_override(self):
        self.assertEqual(generate.SNMP_DISCOVERY_RETRIES, 1)
        with mock.patch.dict(os.environ, {"SNMP_DISCOVERY_TIMEOUT": "5"}):
            self.assertEqual(generate._discovery_timeout(), 5)
        with mock.patch.dict(os.environ, {"SNMP_DISCOVERY_TIMEOUT": "invalid"}):
            self.assertEqual(generate._discovery_timeout(), 2)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SNMP_DISCOVERY_TIMEOUT", None)
            self.assertEqual(generate._discovery_timeout(), 2)

    def test_import_has_no_logging_side_effects(self):
        self.assertNotIsInstance(sys.stdout, generate.Tee)
        self.assertFalse(hasattr(generate, "LOG_FILE"))
        self.assertFalse(hasattr(generate, "_log_handle"))

    def test_configure_logging_tees_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = os.getcwd()
            original_stdout = sys.stdout
            try:
                with mock.patch.object(generate, "PROJECT_DIR", root):
                    restore = generate.configure_logging()
                    try:
                        self.assertIsInstance(sys.stdout, generate.Tee)
                        print("hello-log")
                    finally:
                        restore()
            finally:
                os.chdir(cwd)
            self.assertIs(sys.stdout, original_stdout)
            logs = list((root / "logs").glob("generate-*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("hello-log", logs[0].read_text(encoding="utf-8"))

    def test_prepare_runtime_dirs_leaves_writable_dirs_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "grafana-data").mkdir()
            (root / "logs" / "telegraf").mkdir(parents=True)
            (root / "grafana-data").chmod(0o777)
            (root / "logs" / "telegraf").chmod(0o777)
            with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                generate, "is_root", return_value=False
            ), mock.patch("builtins.print") as printed:
                generate.prepare_runtime_dirs()
            warnings = [call.args[0] for call in printed.call_args_list if "WARNING" in str(call.args[0])]
            self.assertEqual(warnings, [])

    def test_prepare_runtime_dirs_warns_before_widening_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                generate, "is_root", return_value=False
            ), mock.patch("builtins.print") as printed:
                generate.prepare_runtime_dirs()
            grafana_mode = stat.S_IMODE((root / "grafana-data").stat().st_mode)
            logs_mode = stat.S_IMODE((root / "logs" / "telegraf").stat().st_mode)
        messages = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertEqual(grafana_mode, 0o777)
        self.assertEqual(logs_mode, 0o777)
        self.assertIn("sudo chown -R 472:472", messages)
        self.assertIn("WARNING", messages)

    def test_container_can_write_detects_matching_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            path.chmod(0o755)
            owner = os.stat(path).st_uid
            self.assertTrue(generate.container_can_write(path, owner))
            self.assertFalse(generate.container_can_write(path, owner + 12345))
            self.assertFalse(generate.container_can_write(path, None))
            with mock.patch("builtins.print"):
                self.assertFalse(generate.ensure_container_writable(path, owner))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

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
        ), mock.patch.object(generate, "snmp_get", side_effect=lambda _host, oid, _conf: values[oid]), mock.patch.object(
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
        ), mock.patch.object(generate, "snmp_get", side_effect=lambda _host, oid, _conf: values[oid]), mock.patch.object(
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
                ["docker", "compose", "restart", "telegraf"],
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
            "check_paloalto_api_access",
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

    def _check_docker_with_info(self, returncode, stderr=""):
        info = subprocess.CompletedProcess(["docker", "info"], returncode, None, stderr)
        with mock.patch.object(generate, "command_exists", return_value=True), mock.patch.object(
            generate, "capture", return_value="Docker Compose version v2.29.0"
        ), mock.patch.object(generate.subprocess, "run", return_value=info) as run:
            generate.check_docker()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["docker", "info"])

    def test_check_docker_accepts_reachable_daemon(self):
        self._check_docker_with_info(0)

    def test_check_docker_reports_socket_permission_denied(self):
        stderr = "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock\n"
        with self.assertRaisesRegex(SystemExit, r"usermod -aG docker \$USER"):
            self._check_docker_with_info(1, stderr)

    def test_check_docker_reports_stopped_daemon(self):
        stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n"
        with self.assertRaisesRegex(SystemExit, "(?s)Cannot connect to the Docker daemon.*systemctl enable --now docker"):
            self._check_docker_with_info(1, stderr)

    def test_capture_propagates_command_failure(self):
        with self.assertRaises(subprocess.CalledProcessError):
            generate.capture(["sh", "-c", "exit 7"])


if __name__ == "__main__":
    unittest.main()
