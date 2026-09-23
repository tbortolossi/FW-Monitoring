"""Behavior of the generate.sh bootstrap wrapper around Docker access.

The wrapper is sourced in a Bash subprocess so that root detection, Docker,
`id` and `usermod` can be replaced by shell functions; `sg` and the venv
Python are stub executables because the wrapper `exec`s them.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
WRAPPER = REPO_DIR / "generate.sh"

STUBS = r"""
is_root() { [[ "$TEST_ROOT" == 1 ]]; }
docker() {
  case "$1" in
    compose) return 0 ;;
    info)
      if [[ -n "${TEST_DOCKER_ERROR:-}" ]]; then
        echo "$TEST_DOCKER_ERROR" >&2
        return 1
      fi
      return 0
      ;;
  esac
}
id() {
  case "$1" in
    -un) echo "$TEST_USER" ;;
    -nG) echo "$TEST_GROUPS" ;;
    -u) echo 1000 ;;
  esac
}
getent() { return 0; }
groupadd() { echo "groupadd $*" >>"$TEST_LOG"; }
usermod() { echo "usermod $*" >>"$TEST_LOG"; }
"""

PERMISSION_DENIED = (
    "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
)


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class GenerateWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log = self.tmp / "calls.log"
        self.log.touch()
        bin_dir = self.tmp / "bin"
        venv_bin = self.tmp / "venv" / "bin"
        bin_dir.mkdir()
        venv_bin.mkdir(parents=True)
        self._stub(bin_dir / "sg", 'echo "sg $*" >>"$TEST_LOG"')
        self._stub(venv_bin / "python", 'echo "python $*" >>"$TEST_LOG"')
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def _stub(self, path, body):
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def run_wrapper(self, *args, root=False, sudo_user="", groups="alice", docker_error="",
                    extra="", env=None):
        script = f'source "$1"\n{STUBS}\n{extra}\nshift\nmain "$@"\n'
        environment = {
            "PATH": self.path,
            "HOME": str(self.tmp),
            "TEST_LOG": str(self.log),
            "TEST_ROOT": "1" if root else "0",
            "TEST_USER": "alice",
            "TEST_GROUPS": groups,
            "TEST_DOCKER_ERROR": docker_error,
            "VENV_DIR": str(self.tmp / "venv"),
        }
        if sudo_user:
            environment["SUDO_USER"] = sudo_user
        environment.update(env or {})
        result = subprocess.run(
            ["bash", "-c", script, "wrapper", str(WRAPPER), *args],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        return result, self.log.read_text(encoding="utf-8")

    def test_sourcing_does_not_run_main(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && declare -F main', "wrapper", str(WRAPPER)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "main")

    def test_sudo_adds_operator_to_docker_group_when_docker_is_preinstalled(self):
        result, calls = self.run_wrapper(root=True, sudo_user="alice", groups="alice sudo")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usermod -aG docker alice", calls)
        self.assertIn("run the generator as alice, without sudo", result.stdout)
        self.assertIn("root-equivalent", result.stdout)

    def test_sudo_stops_before_writing_project_files(self):
        result, calls = self.run_wrapper(root=True, sudo_user="alice", groups="alice docker")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already in the docker group", result.stdout)
        self.assertNotIn("usermod", calls)
        self.assertNotIn("python", calls)

    def test_plain_root_login_still_runs_the_generator(self):
        result, calls = self.run_wrapper("--flag", root=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-m pip install", calls)
        self.assertRegex(calls, r"python \S+generate\.py --flag")

    def test_user_with_docker_access_runs_the_generator(self):
        result, calls = self.run_wrapper("--flag")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(calls, r"python \S+generate\.py --flag")
        self.assertNotIn("sg ", calls)

    def test_group_member_without_new_login_restarts_through_sg(self):
        result, calls = self.run_wrapper("--flag", groups="alice docker", docker_error=PERMISSION_DENIED)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(calls, r"sg docker -c bash \S*generate\.sh --flag")
        self.assertNotIn("python", calls)
        self.assertIn("Using the docker group for this run", result.stdout)

    def test_sg_restart_is_attempted_only_once(self):
        result, calls = self.run_wrapper(
            groups="alice docker",
            docker_error=PERMISSION_DENIED,
            env={"FW_MONITORING_DOCKER_SG": "1"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("sg ", calls)
        self.assertIn("Log out and back in", result.stderr)

    def test_user_outside_docker_group_is_told_to_run_sudo_once(self):
        result, calls = self.run_wrapper(docker_error=PERMISSION_DENIED)

        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo ./generate.sh", result.stderr)
        self.assertEqual(calls, "")

    def test_stopped_daemon_is_left_to_generate_py(self):
        result, calls = self.run_wrapper(docker_error="Cannot connect to the Docker daemon")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("generate.py", calls)

    def test_root_owned_files_stop_the_run_with_a_chown_command(self):
        extra = textwrap.dedent(
            """
            root_owned_paths() { printf '%s\\n' .venv telegraf/telegraf.conf; }
            """
        )
        result, calls = self.run_wrapper(extra=extra)

        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo chown -R alice: .venv telegraf/telegraf.conf", result.stderr)
        self.assertEqual(calls, "")

    def test_root_owned_paths_is_empty_for_a_clean_checkout(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && root_owned_paths', "wrapper", str(WRAPPER)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.geteuid() != 0:
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
