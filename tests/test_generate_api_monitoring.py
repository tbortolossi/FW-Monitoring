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
    def test_api_defaults_are_normalized(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440"})
        generate.validate_inventory(firewalls)
        api = firewalls[0]["api_monitoring"]
        self.assertEqual(api["interval"], 20)
        self.assertEqual(api["resource_interval"], 60)
        self.assertTrue(api["verify_tls"])

    def test_interval_below_ten_seconds_is_rejected(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440", "interval": 5})
        with self.assertRaisesRegex(SystemExit, "between 10 and 3600"):
            generate.validate_inventory(firewalls)

    def test_plaintext_key_is_not_accepted_in_inventory(self):
        firewalls = inventory({"enabled": True, "api_key": "secret"})
        with self.assertRaisesRegex(SystemExit, "api_key_env"):
            generate.validate_inventory(firewalls)

    def test_api_monitoring_is_rejected_for_fortinet(self):
        firewalls = inventory({"enabled": True, "api_key_env": "FORTINET_KEY"})
        firewalls[0]["vendor"] = "fortinet"
        with self.assertRaisesRegex(SystemExit, "only for Palo Alto"):
            generate.validate_inventory(firewalls)

    def test_runtime_inventory_contains_reference_but_no_key(self):
        firewalls = inventory({"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_440"})
        generate.validate_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "paloalto-api.json"
            with mock.patch.object(generate, "PALOALTO_API_INVENTORY", destination):
                generate.render_paloalto_api_inventory(firewalls)
            data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["api_key_env"], "PALOALTO_API_KEY_PA_440")
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


if __name__ == "__main__":
    unittest.main()
