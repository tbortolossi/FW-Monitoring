"""Runtime tests for the Palo Alto API collector entry points.

collect_firewall is always mocked, so no request reaches a firewall.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import telegraf.paloalto_api_collector as collector


INTERVAL_CATEGORIES = {"sessions", "interfaces"}


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class FakeEventFactory:
    """Build Event replacements that advance a fake clock on every wait()."""

    def __init__(self, clock, step, max_waits):
        self.clock = clock
        self.step = step
        self.max_waits = max_waits
        self.waits = []
        self.instances = []

    def __call__(self):
        factory = self

        class _Event:
            def __init__(self):
                self._set = False
                factory.instances.append(self)

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

            def wait(self, timeout=None):
                factory.waits.append(timeout)
                factory.clock.now += factory.step
                if len(factory.waits) >= factory.max_waits:
                    self._set = True
                return self._set

        return _Event()


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_non_list_config_is_rejected(self):
        for payload in ({"hostname": "PA"}, "text", 3, None):
            with self.subTest(payload=payload):
                path = self.root / "config.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must be a JSON list"):
                    collector.load_config(path)

    def test_list_config_is_returned(self):
        path = self.root / "config.json"
        path.write_text('[{"hostname": "PA"}]', encoding="utf-8")
        self.assertEqual(collector.load_config(path), [{"hostname": "PA"}])

    def test_invalid_json_raises(self):
        path = self.root / "config.json"
        path.write_text("[", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            collector.load_config(path)


class LoadEnvironmentFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "api.env"

    def test_invalid_name_is_rejected_without_value(self):
        for name in ("9BAD", "BAD-NAME", "", "A B"):
            with self.subTest(name=name):
                self.path.write_text(f"{name}=hidden-value\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(ValueError, "invalid environment variable name") as raised:
                        collector.load_environment_file(self.path)
                self.assertNotIn("hidden-value", str(raised.exception))

    def test_unquoted_comments_blank_lines_and_setdefault(self):
        self.path.write_text(
            "\n"
            "# comment=ignored\n"
            "   \n"
            "not an assignment\n"
            "  PLAIN =  raw value with spaces  \n"
            "EXISTING=from-file\n"
            "EQUALS=a=b=c\n"
            "EMPTY=\n"
            "ONE_CHAR=\"\n"
            "MIXED=\"abc'\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"EXISTING": "from-process"}, clear=True):
            collector.load_environment_file(self.path)
            self.assertEqual(os.environ["PLAIN"], "raw value with spaces")
            self.assertEqual(os.environ["EXISTING"], "from-process")
            self.assertEqual(os.environ["EQUALS"], "a=b=c")
            self.assertEqual(os.environ["EMPTY"], "")
            self.assertEqual(os.environ["ONE_CHAR"], '"')
            self.assertEqual(os.environ["MIXED"], "\"abc'")
            self.assertNotIn("comment", os.environ)

    def test_quoted_values_round_trip_generator_encoding(self):
        import generate

        values = {"A": "plain", "B": 'q"uote', "C": "back\\slash\\", "D": "$dollar ${X}", "E": "it's # = x"}
        self.path.write_text(
            "".join(f"{name}={generate.compose_environment_value(value)}\n" for name, value in values.items()),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            collector.load_environment_file(self.path)
            for name, value in values.items():
                with self.subTest(name=name):
                    self.assertEqual(os.environ[name], value)

    def test_legacy_single_quote_keeps_unknown_escapes(self):
        self.path.write_text("K='a\\nb\\\\'\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            collector.load_environment_file(self.path)
            self.assertEqual(os.environ["K"], "a\\nb\\")


class RunOnceTests(unittest.TestCase):
    def test_every_line_from_every_firewall_is_printed(self):
        configs = [{"hostname": "PA-1"}, {"hostname": "PA-2"}]

        def collect(config, due):
            self.assertEqual(due, set(collector.CATEGORY_SCHEDULES))
            return [f"m,hostname={config['hostname']} a=1i", f"m,hostname={config['hostname']} b=2i"]

        with mock.patch.object(collector, "collect_firewall", side_effect=collect) as collect_mock, mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            self.assertEqual(collector.run_once(configs), 0)
        self.assertEqual(collect_mock.call_count, 2)
        self.assertEqual(
            sorted(stdout.getvalue().splitlines()),
            sorted(
                [
                    "m,hostname=PA-1 a=1i",
                    "m,hostname=PA-1 b=2i",
                    "m,hostname=PA-2 a=1i",
                    "m,hostname=PA-2 b=2i",
                ]
            ),
        )

    def test_explicit_categories_are_forwarded(self):
        with mock.patch.object(collector, "collect_firewall", return_value=[]) as collect_mock, mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            collector.run_once([{"hostname": "PA"}], {"sessions"})
        collect_mock.assert_called_once_with({"hostname": "PA"}, {"sessions"})


class RunDaemonTests(unittest.TestCase):
    def run_daemon(self, configs, step, max_waits):
        clock = FakeClock()
        events = FakeEventFactory(clock, step, max_waits)
        calls = []

        def collect(config, due):
            calls.append((clock.now, config["hostname"], set(due)))
            return [f"line,hostname={config['hostname']} n={len(calls)}i"]

        with mock.patch.object(collector, "time", SimpleNamespace(monotonic=clock.monotonic)), mock.patch.object(
            collector, "threading", SimpleNamespace(Event=events)
        ), mock.patch.object(collector.signal, "signal") as signal_mock, mock.patch.object(
            collector, "collect_firewall", side_effect=collect
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = collector.run_daemon(configs)
        return result, calls, events, signal_mock, stdout.getvalue(), clock

    def test_first_cycle_polls_everything_then_only_due_categories(self):
        configs = [{"hostname": "PA-1"}]
        result, calls, events, _signal, output, clock = self.run_daemon(configs, step=20, max_waits=3)
        self.assertEqual(result, 0)
        self.assertEqual(events.waits, [1.0, 1.0, 1.0])
        self.assertEqual(len(calls), 3)
        start = clock.now - 60
        self.assertEqual(calls[0], (start, "PA-1", set(collector.CATEGORY_SCHEDULES)))
        self.assertEqual(calls[1], (start + 20, "PA-1", INTERVAL_CATEGORIES))
        self.assertEqual(calls[2], (start + 40, "PA-1", INTERVAL_CATEGORIES))
        self.assertEqual(output.splitlines(), [f"line,hostname=PA-1 n={n}i" for n in (1, 2, 3)])

    def test_resource_categories_return_after_their_interval(self):
        configs = [{"hostname": "PA-1"}]
        _result, calls, *_rest = self.run_daemon(configs, step=20, max_waits=4)
        resource_categories = {
            category
            for category in collector.CATEGORY_SCHEDULES
            if collector.CATEGORY_SCHEDULES[category]({}) == 60
        }
        self.assertIn("dataplane", resource_categories)
        self.assertIn("counters", resource_categories)
        self.assertEqual(calls[3][2], INTERVAL_CATEGORIES | resource_categories)
        self.assertNotIn("system", calls[3][2])

    def test_per_firewall_intervals_are_independent(self):
        configs = [{"hostname": "FAST", "interval": 10}, {"hostname": "SLOW"}]
        _result, calls, *_rest = self.run_daemon(configs, step=10, max_waits=2)
        by_host = {}
        for _now, hostname, due in calls:
            by_host.setdefault(hostname, []).append(due)
        self.assertEqual(by_host["FAST"], [set(collector.CATEGORY_SCHEDULES), INTERVAL_CATEGORIES])
        self.assertEqual(by_host["SLOW"], [set(collector.CATEGORY_SCHEDULES)])

    def test_idle_loop_does_not_submit_empty_jobs(self):
        configs = [{"hostname": "PA-1"}]
        _result, calls, events, *_rest = self.run_daemon(configs, step=1, max_waits=5)
        self.assertEqual(len(events.waits), 5)
        self.assertEqual(len(calls), 1)

    def test_signal_handlers_stop_the_loop(self):
        configs = [{"hostname": "PA-1"}]
        _result, _calls, events, signal_mock, *_rest = self.run_daemon(configs, step=20, max_waits=1)
        handled = {call.args[0] for call in signal_mock.call_args_list}
        self.assertEqual(handled, {collector.signal.SIGTERM, collector.signal.SIGINT})
        stopped = events.instances[0]
        stopped._set = False
        signal_mock.call_args_list[0].args[1](collector.signal.SIGTERM, None)
        self.assertTrue(stopped.is_set())
        stopped._set = False
        signal_mock.call_args_list[1].args[1](collector.signal.SIGINT, None)
        self.assertTrue(stopped.is_set())


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "paloalto-api.json"
        self.env = self.root / "paloalto-api.env"

    def test_once_loads_environment_and_polls(self):
        self.config.write_text('[{"hostname": "PA", "api_key_env": "RUNTIME_KEY"}]', encoding="utf-8")
        self.env.write_text('RUNTIME_KEY="secret-value"\n', encoding="utf-8")
        seen = {}

        def collect(config, due):
            seen["key"] = os.environ.get(config["api_key_env"])
            seen["due"] = due
            return ["m,hostname=PA v=1i"]

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            collector, "collect_firewall", side_effect=collect
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, mock.patch.object(
            collector, "run_daemon"
        ) as daemon:
            result = collector.main(["--once", "--config", str(self.config), "--env-file", str(self.env)])
        self.assertEqual(result, 0)
        daemon.assert_not_called()
        self.assertEqual(seen, {"key": "secret-value", "due": set(collector.CATEGORY_SCHEDULES)})
        self.assertEqual(stdout.getvalue(), "m,hostname=PA v=1i\n")

    def test_empty_config_returns_without_polling(self):
        self.config.write_text("[]", encoding="utf-8")
        with mock.patch.object(collector, "run_once") as once, mock.patch.object(collector, "run_daemon") as daemon:
            self.assertEqual(collector.main(["--config", str(self.config)]), 0)
            self.assertEqual(collector.main(["--once", "--config", str(self.config)]), 0)
        once.assert_not_called()
        daemon.assert_not_called()

    def test_without_once_runs_daemon(self):
        self.config.write_text('[{"hostname": "PA"}]', encoding="utf-8")
        with mock.patch.object(collector, "run_daemon", return_value=0) as daemon, mock.patch.object(
            collector, "load_environment_file"
        ) as load_env:
            self.assertEqual(collector.main(["--config", str(self.config)]), 0)
        daemon.assert_called_once_with([{"hostname": "PA"}])
        load_env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
