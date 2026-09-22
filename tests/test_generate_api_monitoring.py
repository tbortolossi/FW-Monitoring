import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate


def inventory(api_monitoring):
    return [
        {
            "hostname": "PA-440",
            "host": "192.0.2.10",
            "vendor": "paloalto",
            "snmp_version": 2,
            "community": "placeholder",
            "api_monitoring": api_monitoring,
        }
    ]


class GeneratorApiValidationTests(unittest.TestCase):
    def test_yaml_environment_references_are_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            inventory_path = directory / "firewalls.yml"
            env_path = directory / ".env"
            inventory_path.write_text(
                "- hostname: PA-440\n"
                "  host: 192.0.2.10\n"
                "  community: ${PA_COMMUNITY}\n"
                "  api_monitoring:\n"
                "    enabled: true\n"
                "    api_key: ${PA_API_KEY}\n",
                encoding="utf-8",
            )
            env_path.write_text(
                "PA_COMMUNITY=snmp-secret\nPA_API_KEY=api-secret\n",
                encoding="utf-8",
            )
            firewalls = generate.load_inventory(inventory_path, env_path)
        self.assertEqual(firewalls[0]["community"], "snmp-secret")
        self.assertEqual(firewalls[0]["api_monitoring"]["api_key"], "api-secret")

    def test_missing_yaml_environment_reference_fails_without_leaking_value(self):
        with self.assertRaisesRegex(SystemExit, "environment variable MISSING_SECRET"):
            generate.resolve_environment_references(
                {"api_key": "${MISSING_SECRET}"},
                {},
            )

    def test_generated_inventory_redacts_credentials(self):
        firewalls = inventory({"enabled": True, "api_key": "api-secret"})
        firewalls[0]["community"] = "snmp-secret"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".firewalls.generated.yml"
            generate.save_inventory(firewalls, destination)
            generated = destination.read_text(encoding="utf-8")
            mode = stat.S_IMODE(destination.stat().st_mode)
        self.assertNotIn("api-secret", generated)
        self.assertNotIn("snmp-secret", generated)
        self.assertEqual(mode, 0o600)

    def test_api_defaults_are_normalized(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440"})
        generate.validate_inventory(firewalls)
        api = firewalls[0]["api_monitoring"]
        self.assertEqual(api["interval"], 20)
        self.assertEqual(api["counter_limit"], 256)
        self.assertEqual(api["resource_interval"], 60)
        self.assertTrue(api["verify_tls"])
        self.assertEqual(api["host"], "192.0.2.10")

    def test_api_host_can_differ_from_snmp_host(self):
        firewalls = inventory(
            {"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440", "host": "api-pa.example.test"}
        )
        generate.validate_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "paloalto-api.json"
            with mock.patch.object(generate, "PALOALTO_API_INVENTORY", destination):
                generate.render_paloalto_api_inventory(firewalls)
            data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["host"], "api-pa.example.test")
        self.assertEqual(firewalls[0]["host"], "192.0.2.10")

    def test_api_host_rejects_a_url(self):
        firewalls = inventory(
            {"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440", "host": "https://pa.example.test/api"}
        )
        with self.assertRaisesRegex(SystemExit, "bare IP address"):
            generate.validate_inventory(firewalls)

    def test_interval_below_ten_seconds_is_rejected(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440", "interval": 5})
        with self.assertRaisesRegex(SystemExit, "between 10 and 3600"):
            generate.validate_inventory(firewalls)

    def test_direct_key_is_accepted_in_inventory(self):
        firewalls = inventory({"enabled": True, "api_key": "secret"})
        generate.validate_inventory(firewalls)
        api = firewalls[0]["api_monitoring"]
        self.assertEqual(api["api_key"], "secret")
        self.assertRegex(api["runtime_api_key_env"], r"^PALOALTO_API_KEY_YAML_PA_440_[A-F0-9]{8}$")

    def test_exactly_one_key_source_is_required(self):
        with self.assertRaisesRegex(SystemExit, "exactly one"):
            generate.validate_inventory(inventory({"enabled": True}))
        with self.assertRaisesRegex(SystemExit, "exactly one"):
            generate.validate_inventory(
                inventory({"enabled": True, "api_key": "secret", "api_key_env": "PALO_KEY"})
            )

    def test_direct_key_must_be_one_line(self):
        with self.assertRaisesRegex(SystemExit, "single line"):
            generate.validate_inventory(inventory({"enabled": True, "api_key": "first\nsecond"}))

    def test_api_monitoring_is_rejected_for_fortinet(self):
        firewalls = inventory({"enabled": True, "api_key_env": "FORTINET_KEY"})
        firewalls[0]["vendor"] = "fortinet"
        with self.assertRaisesRegex(SystemExit, "only for Palo Alto"):
            generate.validate_inventory(firewalls)

    def test_runtime_inventory_contains_reference_but_no_key(self):
        firewalls = inventory({"enabled": True, "api_key": "direct-secret"})
        generate.validate_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "paloalto-api.json"
            with mock.patch.object(generate, "PALOALTO_API_INVENTORY", destination):
                generate.render_paloalto_api_inventory(firewalls)
            data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertRegex(data[0]["api_key_env"], r"^PALOALTO_API_KEY_YAML_PA_440_[A-F0-9]{8}$")
        self.assertNotIn("api_key", data[0])

    def test_execd_template_is_rendered_when_api_enabled(self):
        rendered = generate.render_template("inputs_paloalto_api.tmpl", {"firewalls": []})
        self.assertIn("[[inputs.execd]]", rendered)
        self.assertIn("paloalto_api_collector.py", rendered)
        self.assertIn('tagexclude = ["host"]', rendered)

    def test_runtime_environment_contains_only_required_api_key(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440"})
        generate.validate_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / ".env"
            destination = directory / "paloalto-api.env"
            source.write_text(
                "PALOALTO_API_KEY_PA_440=api-secret\nGRAFANA_ADMIN_PASSWORD=unrelated-secret\n",
                encoding="utf-8",
            )
            with mock.patch.object(generate, "PALOALTO_API_ENV", destination):
                generate.render_paloalto_api_environment(firewalls, source=source)
            content = destination.read_text(encoding="utf-8")
            mode = stat.S_IMODE(destination.stat().st_mode)
        self.assertEqual(content, "PALOALTO_API_KEY_PA_440=api-secret\n")
        self.assertEqual(mode, 0o600)

    def test_direct_key_is_copied_to_protected_runtime_environment(self):
        firewalls = inventory({"enabled": True, "api_key": "direct-secret"})
        generate.validate_inventory(firewalls)
        runtime_name = firewalls[0]["api_monitoring"]["runtime_api_key_env"]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / ".env"
            destination = directory / "paloalto-api.env"
            source.write_text("GRAFANA_ADMIN_PASSWORD=unrelated-secret\n", encoding="utf-8")
            with mock.patch.object(generate, "PALOALTO_API_ENV", destination):
                generate.render_paloalto_api_environment(firewalls, source=source)
            content = destination.read_text(encoding="utf-8")
        self.assertEqual(content, f"{runtime_name}=direct-secret\n")
        self.assertNotIn("GRAFANA_ADMIN_PASSWORD", content)


if __name__ == "__main__":
    unittest.main()
