"""Additional hermetic tests for paloalto_api_key.py (no network access)."""

import io
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import paloalto_api_key
from paloalto_api_key import (
    find_inventory_entry,
    generate_key,
    main,
    paloalto_inventory_entries,
    update_env,
    update_inventory,
    validate_target,
)


def fake_response(body: bytes):
    response = mock.MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class GenerateKeyTests(unittest.TestCase):
    def test_success_returns_stripped_key_and_posts_credentials(self):
        body = b"<response status='success'><result><key>\n  KEY-123==  \n</key></result></response>"
        with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)) as urlopen:
            key = generate_key("192.0.2.1", "api user", "p&ss=word", 8443, True, 7)
        self.assertEqual(key, "KEY-123==")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://192.0.2.1:8443/api/")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("p&ss", request.full_url)
        self.assertIn(b"type=keygen", request.data)
        self.assertIn(b"password=p%26ss%3Dword", request.data)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)
        self.assertTrue(urlopen.call_args.kwargs["context"].check_hostname)

    def test_insecure_mode_uses_unverified_context(self):
        body = b"<response status='success'><result><key>K</key></result></response>"
        with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)) as urlopen:
            generate_key("192.0.2.1", "u", "p", 443, False, 15)
        self.assertFalse(urlopen.call_args.kwargs["context"].check_hostname)

    def test_error_status_raises_with_message(self):
        body = b"<response status='error'><result><msg>Invalid credentials.</msg></result></response>"
        with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)):
            with self.assertRaisesRegex(RuntimeError, "Invalid credentials."):
                generate_key("192.0.2.1", "u", "p", 443, True, 15)

    def test_success_without_key_raises(self):
        for body in (
            b"<response status='success'><result></result></response>",
            b"<response status='success'><result><key></key></result></response>",
        ):
            with self.subTest(body=body):
                with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)):
                    with self.assertRaises(RuntimeError):
                        generate_key("192.0.2.1", "u", "p", 443, True, 15)

    def test_whitespace_only_key_is_rejected(self):
        body = b"<response status='success'><result><key>   </key></result></response>"
        with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)):
            with self.assertRaisesRegex(RuntimeError, "did not return an API key"):
                generate_key("192.0.2.1", "u", "p", 443, True, 15)

    def test_empty_error_uses_default_message(self):
        body = b"<response status='error'/>"
        with mock.patch.object(paloalto_api_key.urllib.request, "urlopen", return_value=fake_response(body)):
            with self.assertRaisesRegex(RuntimeError, "did not return an API key"):
                generate_key("192.0.2.1", "u", "p", 443, True, 15)


class TargetAndInventoryTests(unittest.TestCase):
    def test_validate_target_errors(self):
        for host, port, message in (
            ("", 443, "bare IP"),
            ("fw 1", 443, "bare IP"),
            ("fw?x", 443, "bare IP"),
            ("fw#x", 443, "bare IP"),
            ("192.0.2.1", 0, "port must be between"),
            ("192.0.2.1", 65536, "port must be between"),
        ):
            with self.subTest(host=host, port=port):
                with self.assertRaisesRegex(ValueError, message):
                    validate_target(host, port)
        validate_target("fw.example.test", 65535)

    def test_find_inventory_entry_errors(self):
        data = [
            {"hostname": "PA-1", "host": "192.0.2.1"},
            {"hostname": "PA-2", "host": "192.0.2.2"},
            {"hostname": "PA-2", "host": "192.0.2.3"},
            {"hostname": "FGT", "host": "192.0.2.4", "vendor": "fortinet"},
            "not-a-mapping",
        ]
        with self.assertRaisesRegex(ValueError, "must contain a list"):
            find_inventory_entry({"hostname": "PA-1"}, "192.0.2.1", None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            find_inventory_entry(data, "192.0.2.99", None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            find_inventory_entry(data, "", "PA-2")
        with self.assertRaisesRegex(ValueError, "not a Palo Alto"):
            find_inventory_entry(data, "192.0.2.4", None)
        self.assertIs(find_inventory_entry(data, "ignored", "PA-1"), data[0])
        self.assertIs(find_inventory_entry(data, "192.0.2.2", None), data[1])

    def test_find_inventory_entry_accepts_vendor_aliases(self):
        for vendor in ("paloalto", "PANOS", "palo", "palo_alto"):
            with self.subTest(vendor=vendor):
                data = [{"hostname": "PA", "host": "192.0.2.1", "vendor": vendor}]
                self.assertIs(find_inventory_entry(data, "192.0.2.1", None), data[0])

    def test_paloalto_inventory_entries_errors(self):
        with self.assertRaisesRegex(ValueError, "must contain a list"):
            paloalto_inventory_entries("text")
        with self.assertRaisesRegex(ValueError, "does not contain a Palo Alto"):
            paloalto_inventory_entries([{"hostname": "FGT", "host": "192.0.2.4", "vendor": "fortinet"}])
        with self.assertRaisesRegex(ValueError, "does not contain a Palo Alto"):
            paloalto_inventory_entries([])
        for entry in ({"hostname": "PA"}, {"host": "192.0.2.1"}):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, "must define hostname and host"):
                    paloalto_inventory_entries([entry])


class UpdateEnvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".env"

    def test_creates_missing_file_without_leading_blank_line(self):
        update_env(self.path, "PALOALTO_API_KEY_PA", "key")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "# Palo Alto XML API monitoring key.\nPALOALTO_API_KEY_PA=key\n",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_appends_after_blank_line_separator(self):
        self.path.write_text("INFLUX_ORG=netops\n", encoding="utf-8")
        self.path.chmod(0o644)
        update_env(self.path, "PALOALTO_API_KEY_PA", "key")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "INFLUX_ORG=netops\n\n# Palo Alto XML API monitoring key.\nPALOALTO_API_KEY_PA=key\n",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_no_extra_separator_when_file_ends_with_blank_line(self):
        self.path.write_text("A=1\n\n", encoding="utf-8")
        update_env(self.path, "K", "v")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "A=1\n\n# Palo Alto XML API monitoring key.\nK=v\n")

    def test_replaces_only_the_exact_variable(self):
        self.path.write_text("K_EXTRA=keep\nK=old\n# K=comment\nB=2\n", encoding="utf-8")
        update_env(self.path, "K", "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "K_EXTRA=keep\nK=new\n# K=comment\nB=2\n")


class UpdateInventoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "firewalls.yml"
        self.original = "- hostname: PA-1\n  host: 192.0.2.1\n  vendor: paloalto\n"
        self.path.write_text(self.original, encoding="utf-8")
        self.backup = self.path.with_suffix(".yml.bak")

    def test_requires_exactly_one_key_source(self):
        for kwargs in ({}, {"api_key": "k", "api_key_env": "E"}, {"api_key": "", "api_key_env": ""}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    update_inventory(self.path, "192.0.2.1", "PA-1", True, 443, **kwargs)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_backup_created_once_and_host_override_only_when_different(self):
        result = update_inventory(self.path, "192.0.2.50", "PA-1", False, 8443, api_key_env="E1")
        self.assertEqual(result, str(self.backup))
        self.assertEqual(self.backup.read_text(encoding="utf-8"), self.original)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        api = yaml.safe_load(self.path.read_text(encoding="utf-8"))[0]["api_monitoring"]
        self.assertEqual(
            api,
            {"enabled": True, "port": 8443, "verify_tls": False, "host": "192.0.2.50", "api_key": "${E1}"},
        )

        update_inventory(self.path, "192.0.2.1", "PA-1", True, 443, api_key="direct")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), self.original, "backup must not be overwritten")
        api = yaml.safe_load(self.path.read_text(encoding="utf-8"))[0]["api_monitoring"]
        self.assertNotIn("host", api)
        self.assertNotIn("api_key_env", api)
        self.assertEqual(api["api_key"], "direct")

    def test_existing_api_key_env_is_replaced(self):
        self.path.write_text(
            self.original + "  api_monitoring:\n    enabled: false\n    api_key_env: OLD\n    interval: 30\n",
            encoding="utf-8",
        )
        update_inventory(self.path, "192.0.2.1", "PA-1", True, 443, api_key_env="NEW")
        api = yaml.safe_load(self.path.read_text(encoding="utf-8"))[0]["api_monitoring"]
        self.assertEqual(api, {"enabled": True, "interval": 30, "port": 443, "verify_tls": True, "api_key": "${NEW}"})


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inventory = self.root / "firewalls.yml"
        self.env_file = self.root / ".env"
        self.inventory.write_text(
            "- hostname: PA-PARIS\n  host: 192.0.2.10\n  vendor: paloalto\n"
            "- hostname: FGT\n  host: 192.0.2.20\n  vendor: fortinet\n",
            encoding="utf-8",
        )
        self.env_file.write_text("INFLUX_ORG=netops\n", encoding="utf-8")
        stdout = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.stdout = stdout.start()
        self.addCleanup(stdout.stop)

    def run_main(self, *extra, key="generated-secret", password="Unique-Pass-42", username="typed-user"):
        with mock.patch("builtins.input", return_value=f"  {username}  ") as prompt, mock.patch(
            "getpass.getpass", return_value=password
        ), mock.patch.object(paloalto_api_key, "generate_key", return_value=key) as keygen:
            result = main(["--inventory", str(self.inventory), "--env-file", str(self.env_file), *extra])
        return result, keygen, prompt

    def test_env_storage_with_hostname_and_prompted_username(self):
        result, keygen, prompt = self.run_main("--hostname", "PA-PARIS", "--storage", "env")
        self.assertEqual(result, 0)
        prompt.assert_called_once_with("API username: ")
        keygen.assert_called_once_with("192.0.2.10", "typed-user", "Unique-Pass-42", 443, True, 15)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"),
            "INFLUX_ORG=netops\n\n# Palo Alto XML API monitoring key.\nPALOALTO_API_KEY_PA_PARIS=generated-secret\n",
        )
        data = yaml.safe_load(self.inventory.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["api_monitoring"]["api_key"], "${PALOALTO_API_KEY_PA_PARIS}")
        self.assertNotIn("api_monitoring", data[1])
        self.assertNotIn("generated-secret", self.stdout.getvalue())
        self.assertNotIn("Unique-Pass-42", self.stdout.getvalue())

    def test_yaml_storage_with_host_override_and_options(self):
        result, keygen, _ = self.run_main(
            "--hostname", "PA-PARIS",
            "--host", "198.51.100.7",
            "--username", "api",
            "--port", "8443",
            "--timeout", "5",
            "--insecure",
            "--storage", "yaml",
        )
        self.assertEqual(result, 0)
        keygen.assert_called_once_with("198.51.100.7", "api", "Unique-Pass-42", 8443, False, 5)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "INFLUX_ORG=netops\n")
        api = yaml.safe_load(self.inventory.read_text(encoding="utf-8"))[0]["api_monitoring"]
        self.assertEqual(
            api,
            {"enabled": True, "port": 8443, "verify_tls": False, "host": "198.51.100.7", "api_key": "generated-secret"},
        )
        self.assertIn("ignored local inventory", self.stdout.getvalue())
        self.assertNotIn("generated-secret", self.stdout.getvalue())

    def test_host_alone_selects_matching_entry(self):
        result, keygen, _ = self.run_main("--host", "192.0.2.10", "--username", "api")
        self.assertEqual(result, 0)
        keygen.assert_called_once_with("192.0.2.10", "api", "Unique-Pass-42", 443, True, 15)
        self.assertIn("PALOALTO_API_KEY_PA_PARIS=generated-secret", self.env_file.read_text(encoding="utf-8"))

    def test_keygen_failure_exits_without_writing(self):
        with mock.patch("getpass.getpass", return_value="Unique-Pass-42"), mock.patch.object(
            paloalto_api_key, "generate_key", side_effect=RuntimeError("Invalid credentials")
        ):
            with self.assertRaisesRegex(SystemExit, "could not generate API key: Invalid credentials"):
                main([
                    "--inventory", str(self.inventory),
                    "--env-file", str(self.env_file),
                    "--hostname", "PA-PARIS",
                    "--username", "api",
                ])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "INFLUX_ORG=netops\n")
        self.assertFalse(self.inventory.with_suffix(".yml.bak").exists())

    def test_argument_errors_exit_with_usage(self):
        cases = (
            ("fortinet target", ["--hostname", "FGT"]),
            ("unknown host", ["--host", "192.0.2.99"]),
            ("url host", ["--hostname", "PA-PARIS", "--host", "https://fw/"]),
            ("port range", ["--hostname", "PA-PARIS", "--port", "0"]),
            ("missing inventory", ["--inventory", str(self.root / "missing.yml"), "--hostname", "PA-PARIS"]),
        )
        for name, argv in cases:
            with self.subTest(case=name):
                with mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch.object(
                    paloalto_api_key, "generate_key"
                ) as keygen, mock.patch("getpass.getpass") as password:
                    with self.assertRaises(SystemExit) as raised:
                        main(["--inventory", str(self.inventory), "--env-file", str(self.env_file), *argv])
                self.assertEqual(raised.exception.code, 2)
                keygen.assert_not_called()
                password.assert_not_called()

    def test_empty_credentials_are_rejected(self):
        for username, password in (("", "Unique-Pass-42"), ("api", "")):
            with self.subTest(username=username, password=bool(password)):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, mock.patch(
                    "builtins.input", return_value=username
                ), mock.patch("getpass.getpass", return_value=password), mock.patch.object(
                    paloalto_api_key, "generate_key"
                ) as keygen:
                    with self.assertRaises(SystemExit) as raised:
                        main(["--inventory", str(self.inventory), "--env-file", str(self.env_file), "--hostname", "PA-PARIS"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("username and password are required", stderr.getvalue())
                keygen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
