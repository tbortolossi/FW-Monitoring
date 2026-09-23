import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate

REPO_DIR = Path(generate.__file__).resolve().parent


def api_only(**overrides):
    firewall = {
        "hostname": "PA-API",
        "host": "192.0.2.20",
        "vendor": "paloalto",
        "snmp": False,
        "api_monitoring": {"enabled": True, "api_key_env": "PALOALTO_API_KEY_PA_API"},
    }
    firewall.update(overrides)
    return firewall


def snmp_palo():
    return {
        "hostname": "PA-SNMP",
        "host": "192.0.2.10",
        "vendor": "paloalto",
        "snmp_version": 2,
        "community": "placeholder",
    }


class ApiOnlyInventoryTests(unittest.TestCase):
    def test_api_only_entry_needs_no_snmp_credentials(self):
        firewalls = [api_only(snmp="no")]
        generate.validate_inventory(firewalls)
        self.assertIs(firewalls[0]["snmp"], False)
        self.assertNotIn("snmp_version", firewalls[0])
        self.assertFalse(generate.snmp_enabled(firewalls[0]))

    def test_snmp_defaults_to_enabled(self):
        firewalls = [snmp_palo(), dict(snmp_palo(), snmp="true")]
        generate.validate_inventory(firewalls)
        self.assertNotIn("snmp", firewalls[0])
        self.assertTrue(all(generate.snmp_enabled(firewall) for firewall in firewalls))

    def test_api_only_requires_enabled_palo_alto_api(self):
        invalid = (
            api_only(api_monitoring={"enabled": False}),
            api_only(api_monitoring=None),
            {"hostname": "FGT", "host": "192.0.2.2", "vendor": "fortinet", "snmp": False},
        )
        for firewall in invalid:
            with self.subTest(firewall=firewall["hostname"]), self.assertRaisesRegex(
                SystemExit, "snmp: false requires a Palo Alto firewall with api_monitoring enabled"
            ):
                generate.validate_inventory([firewall])

    def test_invalid_snmp_boolean_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "snmp must be true or false"):
            generate.validate_inventory([api_only(snmp="sometimes")])

    def test_enrichment_skips_api_only_entries(self):
        firewall = api_only(hostname="PA-5450", panos_version="12.1.3")
        generate.enrich_inventory([firewall])
        for key in ("chassis", "panos_12_metrics", "pa_cluster"):
            self.assertNotIn(key, firewall)

    def test_discovery_skips_api_only_entries(self):
        firewalls = [api_only(), snmp_palo()]
        with mock.patch.object(generate, "SNMP_DISCOVERY", "true"), mock.patch.object(
            generate, "ensure_snmp_image", return_value=True
        ), mock.patch.object(generate, "snmp_get", return_value="") as snmp_get:
            generate.discover_paloalto_devices(firewalls, {"paloalto"})
        self.assertEqual({call.args[0] for call in snmp_get.call_args_list}, {"192.0.2.10"})

    def test_discovery_and_mibs_are_skipped_without_snmp_palo_alto(self):
        with mock.patch.object(generate, "SNMP_DISCOVERY", "true"), mock.patch.object(
            generate, "ensure_snmp_image"
        ) as image, mock.patch.object(generate.urllib.request, "urlretrieve") as download:
            generate.discover_paloalto_devices([api_only()], {"paloalto"})
            generate.prepare_paloalto_mibs([api_only(panos_version="12.1.3")], {"paloalto"})
        image.assert_not_called()
        download.assert_not_called()

    def test_telegraf_polls_only_snmp_firewalls(self):
        firewalls = [api_only(), snmp_palo()]
        contexts = {}

        def record(name, context):
            contexts[name] = [firewall["hostname"] for firewall in context["firewalls"]]
            return name

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telegraf").mkdir()
            with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                generate, "render_template", side_effect=record
            ):
                generate.render_telegraf(firewalls, {"paloalto"})
        self.assertEqual(contexts["inputs_paloalto.tmpl"], ["PA-SNMP"])
        self.assertEqual(contexts["inputs_paloalto_api.tmpl"], ["PA-API", "PA-SNMP"])

    def test_telegraf_has_no_snmp_input_when_every_firewall_is_api_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telegraf").mkdir()
            with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                generate, "render_template", side_effect=lambda name, _context: name
            ):
                generate.render_telegraf([api_only()], {"paloalto"})
            content = (root / "telegraf" / "telegraf.conf").read_text()
        self.assertNotIn("inputs_paloalto.tmpl", content)
        self.assertIn("inputs_paloalto_api.tmpl", content)

    def test_real_template_renders_no_snmp_agent_for_api_only_entry(self):
        firewalls = [api_only(), snmp_palo()]
        generate.validate_inventory(firewalls)
        generate.enrich_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPO_DIR / "telegraf", root / "telegraf", ignore=shutil.ignore_patterns("mibs", "telegraf.conf"))
            with mock.patch.object(generate, "PROJECT_DIR", root):
                generate.render_telegraf(firewalls, {"paloalto"})
            content = (root / "telegraf" / "telegraf.conf").read_text(encoding="utf-8")
        self.assertIn('agents = ["udp://192.0.2.10:161"]', content)
        self.assertNotIn("192.0.2.20", content)
        self.assertIn("[[inputs.execd]]", content)

    def test_api_only_entry_contributes_only_its_api_key_to_runtime_environment(self):
        firewalls = [api_only(community="left-over")]
        generate.validate_inventory(firewalls)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / ".env"
            source.write_text("PALOALTO_API_KEY_PA_API=api-secret\n", encoding="utf-8")
            destination = Path(directory) / "paloalto-api.env"
            with mock.patch.object(generate, "PALOALTO_API_ENV", destination):
                rendered = generate.render_paloalto_api_environment(firewalls, source=source)
            content = destination.read_text(encoding="utf-8")
        self.assertEqual(content, 'PALOALTO_API_KEY_PA_API="api-secret"\n')
        self.assertEqual(rendered[0]["community"], "left-over")


if __name__ == "__main__":
    unittest.main()
