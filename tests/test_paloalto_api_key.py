import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from paloalto_api_key import environment_name, update_env, update_inventory, validate_target


class ApiKeySetupTests(unittest.TestCase):
    def test_environment_name_is_safe(self):
        self.assertEqual(environment_name("edge-pa-440.example"), "PALOALTO_API_KEY_EDGE_PA_440_EXAMPLE")

    def test_target_rejects_url_instead_of_bare_host(self):
        with self.assertRaisesRegex(ValueError, "bare IP address"):
            validate_target("https://192.0.2.10/api", 443)

    def test_update_env_replaces_key_and_sets_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("UNCHANGED=yes\nPALOALTO_API_KEY_PA_440=old\n", encoding="utf-8")
            update_env(path, "PALOALTO_API_KEY_PA_440", "new-secret")
            self.assertEqual(path.read_text(encoding="utf-8"), "UNCHANGED=yes\nPALOALTO_API_KEY_PA_440=new-secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_update_inventory_uses_environment_reference_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firewalls.yml"
            path.write_text(
                "- hostname: PA-440\n  host: 192.0.2.10\n  vendor: paloalto\n",
                encoding="utf-8",
            )
            backup = update_inventory(
                path,
                "192.0.2.10",
                None,
                False,
                443,
                api_key_env="PALOALTO_API_KEY_PA_440",
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertTrue(Path(backup).is_file())
            self.assertEqual(data[0]["api_monitoring"]["api_key_env"], "PALOALTO_API_KEY_PA_440")
            self.assertFalse(data[0]["api_monitoring"]["verify_tls"])

    def test_update_inventory_can_store_key_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firewalls.yml"
            path.write_text(
                "- hostname: PA-440\n  host: 192.0.2.10\n  vendor: paloalto\n",
                encoding="utf-8",
            )
            backup = update_inventory(
                path,
                "192.0.2.10",
                None,
                True,
                443,
                api_key="direct-secret",
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["api_monitoring"]["api_key"], "direct-secret")
            self.assertNotIn("api_key_env", data[0]["api_monitoring"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(backup).stat().st_mode), 0o600)
            original_backup = Path(backup).read_text(encoding="utf-8")
            update_inventory(path, "192.0.2.10", None, True, 443, api_key="new-secret")
            self.assertEqual(Path(backup).read_text(encoding="utf-8"), original_backup)

    def test_update_inventory_stores_distinct_api_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firewalls.yml"
            path.write_text(
                "- hostname: PA-440\n  host: 192.0.2.10\n  vendor: paloalto\n",
                encoding="utf-8",
            )
            update_inventory(
                path,
                "api-pa.example.test",
                "PA-440",
                True,
                443,
                api_key="direct-secret",
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["host"], "192.0.2.10")
            self.assertEqual(data[0]["api_monitoring"]["host"], "api-pa.example.test")


if __name__ == "__main__":
    unittest.main()
