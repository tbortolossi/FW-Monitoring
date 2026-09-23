"""Additional hermetic tests for generate.py.

Every test patches the module-level paths (PROJECT_DIR, MIB_DIR,
PALOALTO_API_ENV, PALOALTO_API_INVENTORY, ENRICHED_FIREWALLS) to temporary
directories and mocks Docker, subprocess and network access.
"""

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import yaml

import generate


def palo(**extra):
    firewall = {
        "hostname": "PA-1",
        "host": "192.0.2.10",
        "vendor": "paloalto",
        "snmp_version": 2,
        "community": "placeholder",
    }
    firewall.update(extra)
    return firewall


def fortinet(**extra):
    firewall = {
        "hostname": "FGT-1",
        "host": "192.0.2.20",
        "vendor": "fortinet",
        "snmp_version": 2,
        "community": "placeholder",
    }
    firewall.update(extra)
    return firewall


class TempProjectMixin:
    def make_project(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "telegraf").mkdir()
        mib_dir = root / "telegraf" / "mibs" / "paloalto"
        mib_dir.mkdir(parents=True)
        for name, value in (
            ("PROJECT_DIR", root),
            ("MIB_DIR", mib_dir),
            ("PALOALTO_API_ENV", root / "telegraf" / "paloalto-api.env"),
            ("PALOALTO_API_INVENTORY", root / "telegraf" / "paloalto-api.json"),
            ("ENRICHED_FIREWALLS", root / ".firewalls.generated.yml"),
        ):
            patcher = mock.patch.object(generate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        stdout = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.stdout = stdout.start()
        self.addCleanup(stdout.stop)
        return root


def fake_download(member="PAN-COMMON-MIB-11.2.my"):
    def urlretrieve(_url, path):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, "PAN-COMMON-MIB DEFINITIONS ::= BEGIN END\n")
        return str(path), None

    return urlretrieve


class PrepareMibTests(TempProjectMixin, unittest.TestCase):
    def test_download_extracts_and_removes_zip_then_skips(self):
        self.make_project()
        with mock.patch.object(generate.urllib.request, "urlretrieve", side_effect=fake_download()) as download:
            generate.prepare_paloalto_mibs([palo(panos_version="11.2.4")], ["paloalto"])
            self.assertEqual(download.call_count, 1)
            self.assertTrue((generate.MIB_DIR / "PAN-COMMON-MIB-11.2.my").is_file())
            self.assertFalse((generate.MIB_DIR / "pan-11-2-snmp-mib-modules.zip").exists())
            self.assertIn("/zip/snmp-mib/pan-11-2-snmp-mib-modules.zip", download.call_args.args[0])

            generate.prepare_paloalto_mibs([palo(panos_version="11.2.4")], ["paloalto"])
            self.assertEqual(download.call_count, 1, "existing MIBs must not be downloaded again")

    def test_first_url_failure_falls_back_to_second(self):
        self.make_project()
        downloader = fake_download("PAN-COMMON-MIB-11.1.my")
        calls = []

        def urlretrieve(url, path):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.URLError("not found")
            return downloader(url, path)

        with mock.patch.object(generate.urllib.request, "urlretrieve", side_effect=urlretrieve):
            generate.prepare_paloalto_mibs([palo(panos_version="11.1.4")], ["paloalto"])
        self.assertEqual(len(calls), 2)
        self.assertIn("/zip/snmp-mib/pan-11-1-snmp-mib-modules.zip", calls[0])
        self.assertIn("/snmp-mibs/pan-11-1-snmp-mib-modules.zip", calls[1])
        self.assertTrue((generate.MIB_DIR / "PAN-COMMON-MIB-11.1.my").is_file())

    def test_both_urls_failing_exits(self):
        self.make_project()
        with mock.patch.object(
            generate.urllib.request, "urlretrieve", side_effect=urllib.error.URLError("offline")
        ) as download:
            with self.assertRaisesRegex(SystemExit, "could not download pan-11-2-snmp-mib-modules.zip"):
                generate.prepare_paloalto_mibs([palo()], ["paloalto"])
        self.assertEqual(download.call_count, 2)

    def test_fortinet_only_returns_without_download(self):
        self.make_project()
        with mock.patch.object(generate.urllib.request, "urlretrieve") as download:
            generate.prepare_paloalto_mibs([fortinet()], ["fortinet"])
        download.assert_not_called()
        self.assertIn("skipping Palo Alto MIB step", self.stdout.getvalue())

    def test_versions_come_from_panos_version_or_default(self):
        self.make_project()
        with mock.patch.object(generate.urllib.request, "urlretrieve", side_effect=fake_download("x.txt")) as download:
            generate.prepare_paloalto_mibs(
                [
                    palo(panos_version="11.1.4"),
                    palo(hostname="PA-2", panos_version="11.1.6-h3"),
                    palo(hostname="PA-3", panos_version="12.1.2"),
                    fortinet(panos_version="9.9.9"),
                ],
                ["fortinet", "paloalto"],
            )
        names = [call.args[0].rsplit("/", 1)[1] for call in download.call_args_list]
        self.assertEqual(names, ["pan-11-1-snmp-mib-modules.zip", "pan-12-1-snmp-mib-modules.zip"])

        with mock.patch.object(generate, "DEFAULT_PALO_MIB_VERSION", "10-2"), mock.patch.object(
            generate.urllib.request, "urlretrieve", side_effect=fake_download("x.txt")
        ) as download:
            generate.prepare_paloalto_mibs([palo()], ["paloalto"])
        self.assertTrue(download.call_args.args[0].endswith("/pan-10-2-snmp-mib-modules.zip"))
        self.assertIn("using default Palo Alto MIB version 10-2", self.stdout.getvalue())


class DiscoveryNegativeTests(TempProjectMixin, unittest.TestCase):
    DISCOVERERS = (
        ("paloalto", generate.discover_paloalto_devices, palo),
        ("fortinet", generate.discover_fortinet_devices, fortinet),
    )

    def setUp(self):
        self.make_project()
        patcher = mock.patch.object(generate, "SNMP_DISCOVERY", "true")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_snmp_image_returns_early(self):
        for vendor, discover, factory in self.DISCOVERERS:
            with self.subTest(vendor=vendor):
                firewall = factory()
                with mock.patch.object(generate, "ensure_snmp_image", return_value=False), mock.patch.object(
                    generate, "snmp_get"
                ) as snmp_get:
                    discover([firewall], [vendor])
                snmp_get.assert_not_called()
                self.assertNotIn("discovered", firewall)

    def test_no_sys_descr_leaves_firewall_untouched(self):
        for vendor, discover, factory in self.DISCOVERERS:
            with self.subTest(vendor=vendor):
                firewall = factory()
                before = dict(firewall)
                with mock.patch.object(generate, "ensure_snmp_image", return_value=True), mock.patch.object(
                    generate, "snmp_get", return_value=""
                ) as snmp_get, mock.patch.object(generate, "snmp_walk_first") as walk:
                    discover([firewall], [vendor])
                self.assertEqual(snmp_get.call_count, 1)
                walk.assert_not_called()
                self.assertEqual(firewall, before)

    def test_empty_optional_values_are_not_set(self):
        for vendor, discover, factory in self.DISCOVERERS:
            with self.subTest(vendor=vendor):
                firewall = factory()

                def snmp_get(_host, oid, _conf):
                    return "generic device" if oid == ".1.3.6.1.2.1.1.1.0" else ""

                with mock.patch.object(generate, "ensure_snmp_image", return_value=True), mock.patch.object(
                    generate, "snmp_get", side_effect=snmp_get
                ), mock.patch.object(generate, "snmp_walk_first", return_value=""):
                    discover([firewall], [vendor])
                self.assertTrue(firewall["discovered"])
                self.assertEqual(firewall["sys_descr"], "generic device")
                for key in (
                    "sys_object_id",
                    "panos_version",
                    "fortios_version",
                    "serial",
                    "model",
                    "vsys_detected",
                    "vdom_detected",
                ):
                    self.assertNotIn(key, firewall)

    def test_entry_without_host_and_other_vendors_are_skipped(self):
        for vendor, discover, factory in self.DISCOVERERS:
            with self.subTest(vendor=vendor):
                other = fortinet() if vendor == "paloalto" else palo()
                no_host = factory(host="")
                with mock.patch.object(generate, "ensure_snmp_image", return_value=True), mock.patch.object(
                    generate, "snmp_get"
                ) as snmp_get:
                    discover([no_host, other], ["fortinet", "paloalto"])
                snmp_get.assert_not_called()
                self.assertNotIn("discovered", no_host)
                self.assertNotIn("discovered", other)
                self.assertIn("missing host, skipping discovery", self.stdout.getvalue())

    def test_vendor_absent_skips_image_build(self):
        for vendor, discover, _factory in self.DISCOVERERS:
            with self.subTest(vendor=vendor):
                with mock.patch.object(generate, "ensure_snmp_image") as image:
                    discover([], ["other"])
                image.assert_not_called()

    def test_disabled_discovery_leaves_populated_inventory_untouched(self):
        firewalls = [palo(), fortinet()]
        before = [dict(firewall) for firewall in firewalls]
        with mock.patch.object(generate, "SNMP_DISCOVERY", "false"), mock.patch.object(
            generate, "ensure_snmp_image"
        ) as image, mock.patch.object(generate, "snmp_get") as snmp_get:
            generate.discover_paloalto_devices(firewalls, ["fortinet", "paloalto"])
            generate.discover_fortinet_devices(firewalls, ["fortinet", "paloalto"])
        image.assert_not_called()
        snmp_get.assert_not_called()
        self.assertEqual(firewalls, before)
        self.assertIn("SNMP discovery disabled (SNMP_DISCOVERY=false)", self.stdout.getvalue())


class VendorAndImageTests(TempProjectMixin, unittest.TestCase):
    def setUp(self):
        self.make_project()

    def test_detect_vendors_sorted_deduplicated_with_default(self):
        vendors = generate.detect_vendors(
            [{"vendor": "paloalto"}, {"vendor": "fortinet"}, {}, {"vendor": "fortinet"}]
        )
        self.assertEqual(vendors, ["fortinet", "paloalto"])
        self.assertEqual(generate.detect_vendors([{}]), ["paloalto"])
        self.assertEqual(generate.detect_vendors([]), [])

    def test_ensure_snmp_image_uses_compose_image_id(self):
        with mock.patch.object(generate, "SNMP_IMAGE", ""), mock.patch.object(generate, "run") as run, mock.patch.object(
            generate, "capture", return_value="abc123\nother\n"
        ) as capture:
            self.assertTrue(generate.ensure_snmp_image())
            self.assertEqual(generate.SNMP_IMAGE, "abc123")
            # Cached: a second call does not rebuild.
            self.assertTrue(generate.ensure_snmp_image())
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "build", "telegraf"])
        # No terminal handed to the build, or Compose may try a TTY progress UI.
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["env"]["BUILDKIT_PROGRESS"], "plain")
        capture.assert_called_once_with(["docker", "compose", "images", "-q", "telegraf"])

    def test_ensure_snmp_image_shows_build_output_on_failure(self):
        error = subprocess.CalledProcessError(1, ["docker"], output="failed to get console\n")
        with mock.patch.object(generate, "SNMP_IMAGE", ""), mock.patch.object(
            generate, "run", side_effect=error
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(subprocess.CalledProcessError):
                generate.ensure_snmp_image()
        self.assertIn("failed to get console", stderr.getvalue())

    def test_ensure_snmp_image_falls_back_to_image_inspect(self):
        with mock.patch.object(generate, "SNMP_IMAGE", ""), mock.patch.object(generate, "run"), mock.patch.object(
            generate, "capture", side_effect=["", "sha256:feed\n"]
        ) as capture:
            self.assertTrue(generate.ensure_snmp_image())
            self.assertEqual(generate.SNMP_IMAGE, "sha256:feed")
        self.assertEqual(
            capture.call_args_list[1],
            mock.call(
                ["docker", "image", "inspect", "fw-monitoring-telegraf", "--format", "{{.Id}}"],
                check=False,
            ),
        )

    def test_ensure_snmp_image_reports_missing_image(self):
        with mock.patch.object(generate, "SNMP_IMAGE", ""), mock.patch.object(generate, "run"), mock.patch.object(
            generate, "capture", side_effect=["\n", ""]
        ):
            self.assertFalse(generate.ensure_snmp_image())
            self.assertEqual(generate.SNMP_IMAGE, "")


class ValidationNegativeTests(unittest.TestCase):
    def test_api_monitoring_rejections(self):
        cases = (
            ("not a mapping", {"api_monitoring": "yes"}, "api_monitoring must be a mapping"),
            ("invalid enabled", {"api_monitoring": {"enabled": "sometimes", "api_key_env": "K"}}, "invalid API monitoring boolean"),
            ("invalid verify_tls", {"api_monitoring": {"verify_tls": "maybe", "api_key_env": "K"}}, "invalid API monitoring boolean"),
            ("bad env name", {"api_monitoring": {"api_key_env": "9bad"}}, "must name a valid environment variable"),
            ("port text", {"api_monitoring": {"api_key_env": "K", "port": "abc"}}, r"api_monitoring\.port must be an integer"),
            ("port range", {"api_monitoring": {"api_key_env": "K", "port": 70000}}, r"api_monitoring\.port must be between 1 and 65535"),
            ("port none", {"api_monitoring": {"api_key_env": "K", "port": None}}, r"api_monitoring\.port must be an integer"),
            ("both keys", {"api_monitoring": {"api_key": "secret", "api_key_env": "K"}}, "exactly one of"),
            ("no key", {"api_monitoring": {"enabled": True}}, "exactly one of"),
            ("blank key", {"api_monitoring": {"api_key": "   "}}, "exactly one of"),
        )
        for name, extra, message in cases:
            with self.subTest(case=name):
                with self.assertRaisesRegex(SystemExit, message) as raised:
                    generate.validate_inventory([palo(**extra)])
                self.assertIn("PA-1", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))

    def test_api_monitoring_on_fortinet_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "supported only for Palo Alto"):
            generate.validate_inventory([fortinet(api_monitoring={"api_key_env": "K"})])

    def test_disabled_api_monitoring_is_accepted_on_any_vendor(self):
        firewalls = [
            palo(api_monitoring={"enabled": False, "api_key_env": "9bad"}),
            fortinet(api_monitoring={"enabled": "no"}),
        ]
        generate.validate_inventory(firewalls)
        for firewall in firewalls:
            self.assertIs(firewall["api_monitoring"]["enabled"], False)
            self.assertNotIn("runtime_api_key_env", firewall["api_monitoring"])

    def test_snmp_rejections(self):
        cases = (
            ("text version", palo(snmp_version="x"), "snmp_version must be 2 or 3"),
            ("none version", palo(snmp_version=None), "snmp_version must be 2 or 3"),
            ("version 1", palo(snmp_version=1), "snmp_version must be 2 or 3"),
            ("v2 no community", palo(community=""), "community is required for SNMPv2c"),
            (
                "v3 no username",
                palo(snmp_version=3, auth_password="a", priv_password="p"),
                "username is required for SNMPv3",
            ),
            (
                "v3 no priv",
                palo(snmp_version=3, username="u", auth_password="a"),
                "priv_password is required for SNMPv3",
            ),
            ("unsupported vendor", palo(vendor="cisco"), "unsupported vendor 'cisco'"),
            ("no hostname", palo(hostname=""), "entry #1: hostname is required"),
            ("no host", palo(host=""), "PA-1: host is required"),
        )
        for name, firewall, message in cases:
            with self.subTest(case=name):
                with self.assertRaisesRegex(SystemExit, message):
                    generate.validate_inventory([firewall])


class LoadInventoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inventory = self.root / "firewalls.yml"
        self.env = self.root / ".env"

    def test_structure_errors(self):
        cases = (
            ("mapping", "hostname: PA\n", "must be a YAML list"),
            ("scalar", "just-text\n", "must be a YAML list"),
            ("non-mapping entry", "- hostname: PA\n- plain\n", "entry #2 must be a mapping"),
        )
        for name, text, message in cases:
            with self.subTest(case=name):
                self.inventory.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, message):
                    generate.load_inventory(self.inventory, self.env)

    def test_empty_file_is_an_empty_list(self):
        self.inventory.write_text("", encoding="utf-8")
        self.assertEqual(generate.load_inventory(self.inventory, self.env), [])

    def test_references_resolved_from_env_file_with_process_precedence(self):
        self.inventory.write_text(
            "- hostname: PA\n  host: 192.0.2.1\n  community: ${SNMP_COMMUNITY}\n"
            "  api_monitoring:\n    api_key: ' ${API_KEY} '\n  tags: ['${SNMP_COMMUNITY}', literal]\n",
            encoding="utf-8",
        )
        self.env.write_text("SNMP_COMMUNITY=from-file\nAPI_KEY='file-key'\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SNMP_COMMUNITY": "from-process"}):
            os.environ.pop("API_KEY", None)
            data = generate.load_inventory(self.inventory, self.env)
        self.assertEqual(data[0]["community"], "from-process")
        self.assertEqual(data[0]["api_monitoring"]["api_key"], "file-key")
        self.assertEqual(data[0]["tags"], ["from-process", "literal"])

    def test_default_env_path_comes_from_project_dir(self):
        self.inventory.write_text("- community: ${ONLY_IN_DOTENV_XYZ}\n", encoding="utf-8")
        self.env.write_text("ONLY_IN_DOTENV_XYZ=value\n", encoding="utf-8")
        with mock.patch.object(generate, "PROJECT_DIR", self.root), mock.patch.dict(os.environ, {}):
            os.environ.pop("ONLY_IN_DOTENV_XYZ", None)
            self.assertEqual(generate.load_inventory(self.inventory)[0]["community"], "value")

    def test_missing_or_empty_variable_names_the_variable_only(self):
        self.inventory.write_text(
            "- hostname: PA\n  community: public-value\n  auth_password: ${MISSING_SECRET_VAR}\n",
            encoding="utf-8",
        )
        self.env.write_text("OTHER=do-not-print\nMISSING_SECRET_VAR=\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("MISSING_SECRET_VAR", None)
            with self.assertRaises(SystemExit) as raised:
                generate.load_inventory(self.inventory, self.env)
        message = str(raised.exception)
        self.assertIn("MISSING_SECRET_VAR", message)
        self.assertIn("firewalls.yml[0].auth_password", message)
        self.assertNotIn("do-not-print", message)
        self.assertNotIn("public-value", message)


class RuntimeEnvironmentTests(TempProjectMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_project()
        self.source = self.root / ".env"

    def api(self, **config):
        base = {"enabled": True}
        base.update(config)
        return base

    def test_conflicting_keys_for_the_same_runtime_name(self):
        self.source.write_text("SHARED=from-env\n", encoding="utf-8")
        firewalls = [
            palo(hostname="PA-1", api_monitoring=self.api(api_key_env="SHARED")),
            palo(hostname="PA-2", host="192.0.2.11", api_monitoring=self.api(api_key="direct-key")),
        ]
        generate.validate_inventory(firewalls)
        firewalls[1]["api_monitoring"]["runtime_api_key_env"] = "SHARED"
        with self.assertRaisesRegex(SystemExit, "conflicting Palo Alto API keys resolve to SHARED") as raised:
            generate.render_paloalto_api_environment(firewalls, self.source)
        self.assertNotIn("direct-key", str(raised.exception))
        self.assertNotIn("from-env", str(raised.exception))
        self.assertFalse(generate.PALOALTO_API_ENV.exists())

    def test_same_env_reference_shared_by_two_firewalls_is_allowed(self):
        self.source.write_text("SHARED=from-env\n", encoding="utf-8")
        firewalls = [
            palo(hostname="PA-1", api_monitoring=self.api(api_key_env="SHARED")),
            palo(hostname="PA-2", host="192.0.2.11", api_monitoring=self.api(api_key_env="SHARED")),
        ]
        generate.validate_inventory(firewalls)
        generate.render_paloalto_api_environment(firewalls, self.source)
        self.assertEqual(generate.PALOALTO_API_ENV.read_text().count("SHARED="), 1)

    def test_missing_env_keys_are_listed_sorted_and_deduplicated(self):
        self.source.write_text("PRESENT=value\nEMPTY_KEY=\n", encoding="utf-8")
        firewalls = [
            palo(hostname="PA-1", api_monitoring=self.api(api_key_env="ZZZ_MISSING")),
            palo(hostname="PA-2", host="192.0.2.11", api_monitoring=self.api(api_key_env="EMPTY_KEY")),
            palo(hostname="PA-3", host="192.0.2.12", api_monitoring=self.api(api_key_env="ZZZ_MISSING")),
            palo(hostname="PA-4", host="192.0.2.13", api_monitoring=self.api(api_key_env="PRESENT")),
        ]
        generate.validate_inventory(firewalls)
        with self.assertRaises(SystemExit) as raised:
            generate.render_paloalto_api_environment(firewalls, self.source)
        self.assertTrue(str(raised.exception).endswith(": EMPTY_KEY, ZZZ_MISSING"))
        self.assertNotIn("value", str(raised.exception).split(":", 1)[1])

    def test_snmp_secrets_become_references_and_values_stay_private(self):
        self.source.write_text("", encoding="utf-8")
        firewalls = [
            palo(community="v2-community"),
            fortinet(
                snmp_version=3,
                username="monitor",
                auth_password="auth-secret",
                priv_password="priv-secret",
            ),
        ]
        del firewalls[1]["community"]
        generate.validate_inventory(firewalls)
        rendered = generate.render_paloalto_api_environment(firewalls, self.source)

        # The input inventory keeps the real values; the rendered copy does not.
        self.assertEqual(firewalls[0]["community"], "v2-community")
        self.assertEqual(firewalls[1]["auth_password"], "auth-secret")
        expected = {
            (0, "community"): "v2-community",
            (1, "auth_password"): "auth-secret",
            (1, "priv_password"): "priv-secret",
        }
        content = generate.PALOALTO_API_ENV.read_text(encoding="utf-8")
        for (index, field), secret in expected.items():
            name = generate.snmp_runtime_environment_name(firewalls[index], field)
            self.assertTrue(name.startswith("FIREWALL_SNMP_"))
            self.assertEqual(rendered[index][field], f"${name}")
            self.assertIn(f'{name}="{secret}"\n', content)
        self.assertEqual(rendered[1]["username"], "monitor")
        self.assertNotIn("community", rendered[1])
        self.assertEqual(stat.S_IMODE(generate.PALOALTO_API_ENV.stat().st_mode), 0o600)
        for secret in expected.values():
            self.assertNotIn(secret, self.stdout.getvalue())
        self.assertIn("3 monitoring secret(s)", self.stdout.getvalue())


class MainOrchestrationTests(TempProjectMixin, unittest.TestCase):
    ORDER = (
        "check_docker",
        "check_env_file",
        "prepare_runtime_dirs",
        "load_inventory",
        "validate_inventory",
        "detect_vendors",
        "enrich_inventory",
        "discover_paloalto_devices",
        "discover_fortinet_devices",
        "enrich_inventory",
        "save_inventory",
        "prepare_paloalto_mibs",
        "render_paloalto_api_inventory",
        "render_paloalto_api_environment",
        "render_telegraf",
        "start_stack",
    )

    def test_main_calls_steps_in_order_and_renders_redacted_inventory(self):
        root = self.make_project()
        firewalls = [palo()]
        vendors = ["paloalto"]
        rendered = [palo(community="$FIREWALL_SNMP_REF")]
        parent = mock.Mock()
        for name in dict.fromkeys(self.ORDER):
            patcher = mock.patch.object(generate, name)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            parent.attach_mock(mocked, name)
        parent.load_inventory.return_value = firewalls
        parent.detect_vendors.return_value = vendors
        parent.render_paloalto_api_environment.return_value = rendered

        generate.main()

        self.assertEqual([call[0] for call in parent.mock_calls], list(self.ORDER))
        parent.load_inventory.assert_called_once_with(root / "firewalls.yml")
        parent.validate_inventory.assert_called_once_with(firewalls)
        parent.discover_paloalto_devices.assert_called_once_with(firewalls, vendors)
        parent.save_inventory.assert_called_once_with(firewalls, generate.ENRICHED_FIREWALLS)
        parent.render_paloalto_api_environment.assert_called_once_with(firewalls)
        parent.render_telegraf.assert_called_once_with(rendered, vendors)

    def test_main_stops_before_any_write_when_docker_is_missing(self):
        self.make_project()
        with mock.patch.object(generate, "check_docker", side_effect=SystemExit("no docker")), mock.patch.object(
            generate, "check_env_file"
        ) as env_check, mock.patch.object(generate, "start_stack") as start:
            with self.assertRaises(SystemExit):
                generate.main()
        env_check.assert_not_called()
        start.assert_not_called()


class ConfigureLoggingTests(unittest.TestCase):
    def test_log_file_created_and_restore_closes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = os.getcwd()
            original_stdout, original_stderr = io.StringIO(), io.StringIO()
            try:
                with mock.patch.object(generate, "PROJECT_DIR", root), mock.patch.object(
                    sys, "stdout", original_stdout
                ), mock.patch.object(sys, "stderr", original_stderr):
                    restore = generate.configure_logging()
                    try:
                        self.assertEqual(Path(os.getcwd()).resolve(), root.resolve())
                        self.assertIsInstance(sys.stderr, generate.Tee)
                        handle = sys.stdout.streams[1]
                        self.assertIs(sys.stderr.streams[1], handle)
                        sys.stderr.write("to-stderr\n")
                        sys.stderr.flush()
                    finally:
                        restore()
                    self.assertIs(sys.stdout, original_stdout)
                    self.assertIs(sys.stderr, original_stderr)
            finally:
                os.chdir(cwd)
            self.assertTrue(handle.closed)
            self.assertIn("to-stderr", original_stderr.getvalue())
            logs = list((root / "logs").glob("generate-*.log"))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text(encoding="utf-8")
            self.assertIn(f"Logging to {logs[0]}", text)
            self.assertIn("to-stderr", text)


class SaveInventoryTests(TempProjectMixin, unittest.TestCase):
    def test_saved_inventory_redacts_secrets_and_is_private(self):
        self.make_project()
        firewalls = [
            palo(api_monitoring={"enabled": True, "api_key": "direct"}, _inferred_keys=["chassis"]),
            fortinet(snmp_version=3, username="u", auth_password="a", priv_password="p", community=""),
        ]
        generate.save_inventory(firewalls, generate.ENRICHED_FIREWALLS)
        saved = yaml.safe_load(generate.ENRICHED_FIREWALLS.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["community"], "<redacted>")
        self.assertEqual(saved[0]["api_monitoring"]["api_key"], "<redacted>")
        self.assertNotIn("_inferred_keys", saved[0])
        self.assertEqual(saved[1]["auth_password"], "<redacted>")
        self.assertEqual(saved[1]["community"], "")
        self.assertEqual(saved[1]["username"], "u")
        # The in-memory inventory keeps its values.
        self.assertEqual(firewalls[0]["api_monitoring"]["api_key"], "direct")
        self.assertEqual(stat.S_IMODE(generate.ENRICHED_FIREWALLS.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
