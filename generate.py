#!/usr/bin/env python3
import datetime as _datetime
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yaml
    from jinja2 import Environment
except ImportError as exc:
    raise SystemExit(
        "ERROR: Python dependencies are missing. Run ./generate.sh so the local "
        "virtual environment can install requirements.txt."
    ) from exc


PROJECT_DIR = Path(__file__).resolve().parent


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def configure_logging():
    """Mirror stdout/stderr into logs/generate-<timestamp>.log.

    Called only when generate.py runs as a script, so importing the module
    (for example from the unit tests) has no side effects. Returns a callable
    that restores the original streams and closes the log file.
    """
    os.chdir(PROJECT_DIR)
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"generate-{_datetime.datetime.now():%Y%m%d-%H%M%S}.log"
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log_handle = log_file.open("a", encoding="utf-8")
    sys.stdout = Tee(original_stdout, log_handle)
    sys.stderr = Tee(original_stderr, log_handle)
    print(f"Logging to {log_file}")

    def restore():
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_handle.close()

    return restore


MIB_DIR = PROJECT_DIR / "telegraf" / "mibs" / "paloalto"
ENRICHED_FIREWALLS = PROJECT_DIR / ".firewalls.generated.yml"
PALOALTO_API_INVENTORY = PROJECT_DIR / "telegraf" / "paloalto-api.json"
PALOALTO_API_ENV = PROJECT_DIR / "telegraf" / "paloalto-api.env"
SNMP_DISCOVERY = os.environ.get("SNMP_DISCOVERY", "true").lower()
DEFAULT_PALO_MIB_VERSION = os.environ.get("PALO_MIB_VERSION", "11-2")
SNMP_IMAGE = ""
GRAFANA_CONTAINER_UID = 472


def _discovery_timeout():
    # SNMP_DISCOVERY_TIMEOUT (seconds, integer >= 1) can be raised in the
    # environment for slow or distant firewalls; the default is 2 seconds.
    try:
        return max(1, int(os.environ.get("SNMP_DISCOVERY_TIMEOUT", "2")))
    except ValueError:
        return 2


SNMP_DISCOVERY_TIMEOUT = _discovery_timeout()
SNMP_DISCOVERY_RETRIES = 1
API_CHECK = os.environ.get("API_CHECK", "true").lower()


def _api_check_timeout():
    # API_CHECK_TIMEOUT (seconds, integer >= 1) bounds the one-shot API check;
    # the firewall's own api_monitoring.timeout is used when it is shorter.
    try:
        return max(1, int(os.environ.get("API_CHECK_TIMEOUT", "5")))
    except ValueError:
        return 5


API_CHECK_TIMEOUT = _api_check_timeout()


def run(cmd, **kwargs):
    print("+ " + " ".join(str(part) for part in cmd))
    return subprocess.run([str(part) for part in cmd], check=True, **kwargs)


def capture(cmd, check=True):
    result = subprocess.run(
        [str(part) for part in cmd],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.stdout


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def command_exists(command):
    return shutil.which(command) is not None


def check_docker():
    if not command_exists("docker"):
        if Path("/etc/debian_version").exists():
            raise SystemExit(
                "ERROR: Docker is required before running this generator.\n"
                "Run the bootstrap wrapper with sudo so it can install Docker on Debian/Ubuntu:\n"
                "  sudo ./generate.sh"
            )
        raise SystemExit(
            "ERROR: Docker is required before running this generator. "
            "Install Docker first: https://docs.docker.com/get-docker/"
        )

    try:
        capture(["docker", "compose", "version"])
    except subprocess.CalledProcessError as exc:
        raise SystemExit("ERROR: Docker Compose v2 is not installed. Install Docker Compose v2 or docker-compose-plugin.") from exc

    # `docker compose version` works without the daemon; `docker info` needs it.
    result = subprocess.run(
        ["docker", "info"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return
    if "permission denied" in result.stderr.lower():
        raise SystemExit(
            "ERROR: this user cannot access the Docker daemon (permission denied on /var/run/docker.sock).\n"
            "Add the user to the docker group, then log out and back in:\n"
            "  sudo usermod -aG docker $USER\n"
            "Check with `docker ps`, then rerun ./generate.sh (do not run it with sudo)."
        )
    raise SystemExit(
        "ERROR: the Docker daemon is not reachable:\n"
        f"  {result.stderr.strip()}\n"
        "Start it and rerun this script:\n"
        "  sudo systemctl enable --now docker"
    )


def check_env_file():
    env_path = PROJECT_DIR / ".env"
    if not env_path.is_file():
        raise SystemExit(
            "ERROR: .env is missing.\n"
            "Copy .env.example to .env and change every secret before running this script:\n"
            "  cp .env.example .env"
        )
    env_path.chmod(0o600)


def prepare_runtime_dirs():
    grafana_data = PROJECT_DIR / "grafana-data"
    telegraf_logs = PROJECT_DIR / "logs" / "telegraf"
    if not grafana_data.is_dir():
        print(f"Creating {grafana_data}")
        grafana_data.mkdir(parents=True)
    telegraf_logs.mkdir(parents=True, exist_ok=True)

    if is_root():
        print(f"Fixing permissions on {grafana_data} (UID {GRAFANA_CONTAINER_UID})...")
        for root, dirs, files in os.walk(grafana_data):
            for name in dirs + files:
                os.chown(Path(root) / name, GRAFANA_CONTAINER_UID, GRAFANA_CONTAINER_UID)
        os.chown(grafana_data, GRAFANA_CONTAINER_UID, GRAFANA_CONTAINER_UID)
    else:
        ensure_container_writable(grafana_data, GRAFANA_CONTAINER_UID)
    ensure_container_writable(telegraf_logs, None)

    MIB_DIR.mkdir(parents=True, exist_ok=True)


def container_can_write(path, uid):
    """Return True when a container running as ``uid`` can already write to ``path``."""
    info = os.stat(path)
    mode = stat.S_IMODE(info.st_mode)
    if uid is not None and info.st_uid == uid and mode & stat.S_IWUSR:
        return True
    if uid is not None and info.st_gid == uid and mode & stat.S_IWGRP:
        return True
    return bool(mode & stat.S_IWOTH and mode & stat.S_IXOTH)


def ensure_container_writable(path, uid):
    """Widen permissions only when the container user cannot already write.

    Without root, the generator cannot chown the directory to the container
    UID, so it falls back to mode 0777 and explains the safer alternative.
    """
    path = Path(path)
    if container_can_write(path, uid):
        return False
    if uid is not None:
        print(
            f"WARNING: {path} is not owned by the container UID {uid} and this script is not running as root; "
            f"setting mode 0777 so the container can write to it. Safer alternative: "
            f"sudo chown -R {uid}:{uid} {path}"
        )
    else:
        print(
            f"WARNING: {path} is not writable by the container user; setting mode 0777 so the container "
            f"can write logs to it."
        )
    path.chmod(0o777)
    return True


ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
SECRET_KEYS = {"community", "auth_password", "priv_password", "api_key"}


def resolve_environment_references(value, environment, location="firewalls.yml"):
    if isinstance(value, dict):
        return {
            key: resolve_environment_references(item, environment, f"{location}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_environment_references(item, environment, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, str):
        return value
    match = ENV_REFERENCE.fullmatch(value.strip())
    if not match:
        return value
    name = match.group(1)
    resolved = environment.get(name)
    if resolved is None or resolved == "":
        raise SystemExit(f"ERROR: {location}: environment variable {name} is not set or is empty.")
    return resolved


def load_inventory(path, env_path=None):
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or []
    if not isinstance(data, list):
        raise SystemExit("ERROR: firewalls.yml must be a YAML list of firewall objects.")
    for index, firewall in enumerate(data):
        if not isinstance(firewall, dict):
            raise SystemExit(f"ERROR: firewalls.yml entry #{index + 1} must be a mapping.")
    environment = {}
    source = Path(env_path or PROJECT_DIR / ".env")
    if source.is_file():
        environment.update(load_dotenv(source))
    environment.update(os.environ)
    return resolve_environment_references(data, environment)


def save_inventory(firewalls, path):
    sanitized = copy.deepcopy(firewalls)
    for firewall in sanitized:
        for key in [key for key in firewall if str(key).startswith("_")]:
            del firewall[key]
        for key in SECRET_KEYS:
            if firewall.get(key):
                firewall[key] = "<redacted>"
        api_config = firewall.get("api_monitoring")
        if isinstance(api_config, dict) and api_config.get("api_key"):
            api_config["api_key"] = "<redacted>"
    with Path(path).open("w", encoding="utf-8") as handle:
        yaml.safe_dump(sanitized, handle, sort_keys=False, default_flow_style=False)
    Path(path).chmod(0o600)


def normalize_vendor(value):
    vendor = str(value or "paloalto").strip().lower()
    aliases = {
        "panos": "paloalto",
        "palo": "paloalto",
        "palo_alto": "paloalto",
        "fortigate": "fortinet",
        "fortios": "fortinet",
    }
    return aliases.get(vendor, vendor)


def normalize_snmp_auth(value):
    mapping = {
        "md5": "MD5",
        "sha": "SHA",
        "sha1": "SHA",
        "sha224": "SHA-224",
        "sha256": "SHA-256",
        "sha384": "SHA-384",
        "sha512": "SHA-512",
    }
    return mapping.get(str(value or "SHA").lower(), str(value or "SHA"))


def normalize_snmp_priv(value):
    mapping = {
        "des": "DES",
        "aes": "AES",
        "aes128": "AES",
        "aes192": "AES-192",
        "aes256": "AES-256",
    }
    return mapping.get(str(value or "AES").lower(), str(value or "AES"))


def normalize_telegraf_snmp_auth(value):
    return normalize_snmp_auth(value).replace("-", "")


def normalize_telegraf_snmp_priv(value):
    return normalize_snmp_priv(value).replace("-", "")


def normalize_boolean(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def api_runtime_environment_name(firewall):
    label = re.sub(r"[^A-Za-z0-9]+", "_", str(firewall.get("hostname") or "firewall")).strip("_").upper()
    identity = f"{firewall.get('hostname', '')}\0{firewall.get('host', '')}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:8].upper()
    return f"PALOALTO_API_KEY_YAML_{label}_{suffix}"


def validate_api_monitoring(firewall, label):
    config = firewall.get("api_monitoring")
    if config is None:
        firewall["api_monitoring"] = {"enabled": False}
        return
    if not isinstance(config, dict):
        raise SystemExit(f"ERROR: {label}: api_monitoring must be a mapping.")
    try:
        enabled = normalize_boolean(config.get("enabled"), default=True)
        verify_tls = normalize_boolean(config.get("verify_tls"), default=True)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {label}: invalid API monitoring boolean: {exc}") from exc
    config["enabled"] = enabled
    config["verify_tls"] = verify_tls
    if not enabled:
        return
    if firewall.get("vendor") != "paloalto":
        raise SystemExit(f"ERROR: {label}: API monitoring is supported only for Palo Alto firewalls.")
    api_host = str(config.get("host") or firewall["host"]).strip()
    if not api_host or re.search(r"[\s/?#]", api_host):
        raise SystemExit(
            f"ERROR: {label}: api_monitoring.host must be a bare IP address or DNS name."
        )
    config["host"] = api_host
    api_key = str(config.get("api_key") or "").strip()
    key_env = str(config.get("api_key_env") or "").strip()
    if bool(api_key) == bool(key_env):
        raise SystemExit(
            f"ERROR: {label}: set exactly one of api_monitoring.api_key or api_monitoring.api_key_env."
        )
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise SystemExit(f"ERROR: {label}: api_monitoring.api_key_env must name a valid environment variable.")
    if api_key:
        if "\n" in api_key or "\r" in api_key:
            raise SystemExit(f"ERROR: {label}: api_monitoring.api_key must be a single line.")
        config["api_key"] = api_key
        config["runtime_api_key_env"] = api_runtime_environment_name(firewall)
    else:
        config["api_key_env"] = key_env
        config["runtime_api_key_env"] = key_env
    for key, default, minimum, maximum in (
        ("port", 443, 1, 65535),
        ("timeout", 15, 1, 120),
        ("interval", 20, 10, 3600),
        ("resource_interval", 60, 10, 3600),
        ("counter_interval", 60, 10, 3600),
        ("counter_limit", 256, 16, 2048),
        ("system_interval", 3600, 60, 86400),
    ):
        try:
            config[key] = int(config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"ERROR: {label}: api_monitoring.{key} must be an integer.") from exc
        if not minimum <= config[key] <= maximum:
            raise SystemExit(
                f"ERROR: {label}: api_monitoring.{key} must be between {minimum} and {maximum}."
            )


def version_tuple(value):
    match = re.search(r"(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def pan_at_least(value, major, minor):
    parsed = version_tuple(value)
    if not parsed:
        return False
    return parsed[0] > major or (parsed[0] == major and parsed[1] >= minor)


def is_palo_chassis(model):
    return bool(re.search(r"(^|[^0-9])(5450|7050|7080|7500)([^0-9]|$)", str(model or ""), re.IGNORECASE))


def chassis_family(model):
    text = str(model or "")
    if re.search(r"7050|7080", text, re.IGNORECASE):
        return "pa7000"
    if re.search(r"5450", text, re.IGNORECASE):
        return "pa5400"
    if re.search(r"7500", text, re.IGNORECASE):
        return "pa7500"
    return ""


def validate_inventory(firewalls):
    for index, firewall in enumerate(firewalls, start=1):
        vendor = normalize_vendor(firewall.get("vendor"))
        firewall["vendor"] = vendor
        label = firewall.get("hostname") or firewall.get("host") or f"entry #{index}"

        if vendor not in {"paloalto", "fortinet"}:
            raise SystemExit(f"ERROR: {label}: unsupported vendor '{vendor}'.")
        if not firewall.get("hostname"):
            raise SystemExit(f"ERROR: entry #{index}: hostname is required.")
        if not firewall.get("host"):
            raise SystemExit(f"ERROR: {label}: host is required.")

        validate_api_monitoring(firewall, label)

        try:
            firewall["snmp_version"] = int(firewall.get("snmp_version", 2))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"ERROR: {label}: snmp_version must be 2 or 3.") from exc

        if firewall["snmp_version"] == 2:
            if not firewall.get("community"):
                raise SystemExit(f"ERROR: {label}: community is required for SNMPv2c.")
        elif firewall["snmp_version"] == 3:
            required = ["username", "auth_password", "priv_password"]
            for key in required:
                if not firewall.get(key):
                    raise SystemExit(f"ERROR: {label}: {key} is required for SNMPv3.")
            firewall["auth_protocol"] = normalize_snmp_auth(firewall.get("auth_protocol"))
            firewall["priv_protocol"] = normalize_snmp_priv(firewall.get("priv_protocol"))
            firewall["telegraf_auth_protocol"] = normalize_telegraf_snmp_auth(firewall.get("auth_protocol"))
            firewall["telegraf_priv_protocol"] = normalize_telegraf_snmp_priv(firewall.get("priv_protocol"))
        else:
            raise SystemExit(f"ERROR: {label}: snmp_version must be 2 or 3.")


# Private bookkeeping key listing the flags that enrich_inventory() inferred
# itself. Keys absent from this list but present on the firewall are operator
# overrides from firewalls.yml and are never recomputed. The key is stripped
# from .firewalls.generated.yml by save_inventory().
INFERRED_KEYS_FIELD = "_inferred_keys"


def _set_inferred(firewall, key, value, keep_falsy_override=True):
    inferred = firewall.setdefault(INFERRED_KEYS_FIELD, [])
    if key in firewall and key not in inferred:
        if keep_falsy_override or firewall[key]:
            return
    firewall[key] = value
    if key not in inferred:
        inferred.append(key)


def enrich_inventory(firewalls):
    """Infer Palo Alto feature flags from model and PAN-OS version.

    Safe to call several times: main() calls it before and after SNMP
    discovery, and flags inferred on the first pass are recomputed once
    discovery has filled in the real model and version, while values declared
    by the operator in firewalls.yml are always preserved.
    """
    for firewall in firewalls:
        if normalize_vendor(firewall.get("vendor")) != "paloalto":
            continue

        panos_version = firewall.get("panos_version")
        if panos_version:
            _set_inferred(firewall, "panos_10_2_metrics", pan_at_least(panos_version, 10, 2))
            _set_inferred(firewall, "panos_11_2_metrics", pan_at_least(panos_version, 11, 2))
            _set_inferred(firewall, "panos_12_metrics", pan_at_least(panos_version, 12, 1))
            _set_inferred(firewall, "vsys_total_cps", pan_at_least(panos_version, 12, 1))
            _set_inferred(firewall, "interface_utilization", pan_at_least(panos_version, 12, 1))

        # Advanced flag for PA-cluster deployments (PAN-OS 11.2+ clustering).
        # It is never inferred: set pa_cluster: true in firewalls.yml to poll
        # the PA cluster summary objects.
        try:
            firewall["pa_cluster"] = normalize_boolean(firewall.get("pa_cluster"), default=False)
        except ValueError as exc:
            label = firewall.get("hostname") or firewall.get("host") or "firewall"
            raise SystemExit(f"ERROR: {label}: pa_cluster must be true or false.") from exc

        model = firewall.get("model") or firewall.get("chassis_model") or firewall.get("hostname")
        _set_inferred(firewall, "chassis", is_palo_chassis(model))
        _set_inferred(firewall, "pan_entity_ext", bool(firewall.get("chassis")))
        _set_inferred(firewall, "chassis_family", chassis_family(model), keep_falsy_override=False)


def build_snmp_args(firewall):
    if int(firewall.get("snmp_version", 2)) == 3:
        username = firewall.get("username", "")
        auth_protocol = normalize_snmp_auth(firewall.get("auth_protocol"))
        auth_password = firewall.get("auth_password", "")
        priv_protocol = normalize_snmp_priv(firewall.get("priv_protocol"))
        priv_password = firewall.get("priv_password", "")

        if priv_password:
            return [
                "-v3",
                "-l",
                "authPriv",
                "-u",
                username,
                "-a",
                auth_protocol,
                "-A",
                auth_password,
                "-x",
                priv_protocol,
                "-X",
                priv_password,
            ]
        return ["-v3", "-l", "authNoPriv", "-u", username, "-a", auth_protocol, "-A", auth_password]

    return ["-v2c", "-c", firewall.get("community", "public")]


SNMP_CONF_TOKEN = re.compile(r"^[A-Za-z0-9-]+$")


def snmp_conf_quote(value, field):
    """Quote a value for Net-SNMP snmp.conf (parsed by copy_nword()).

    Inside double quotes a backslash escapes the next character, so only
    backslash and double quote need escaping. Line breaks and NUL bytes cannot
    be represented and are rejected without echoing the value.
    """
    value = str(value)
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SystemExit(f"ERROR: SNMP {field} cannot contain newlines or NUL bytes.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def snmp_conf_token(value, field):
    value = str(value)
    if not SNMP_CONF_TOKEN.fullmatch(value):
        raise SystemExit(f"ERROR: unsupported SNMP {field} value.")
    return value


def build_snmp_conf(firewall):
    """Return a Net-SNMP client snmp.conf holding the SNMP credentials.

    The file is piped to the discovery container on stdin so that communities
    and SNMPv3 passphrases never appear on a command line (ps, docker inspect,
    audit logs).
    """
    if int(firewall.get("snmp_version", 2)) == 3:
        lines = [
            "defVersion 3",
            f"defSecurityName {snmp_conf_quote(firewall.get('username', ''), 'username')}",
            f"defAuthType {snmp_conf_token(normalize_snmp_auth(firewall.get('auth_protocol')), 'auth_protocol')}",
            f"defAuthPassphrase {snmp_conf_quote(firewall.get('auth_password', ''), 'auth_password')}",
        ]
        if firewall.get("priv_password"):
            lines[1:1] = ["defSecurityLevel authPriv"]
            lines += [
                f"defPrivType {snmp_conf_token(normalize_snmp_priv(firewall.get('priv_protocol')), 'priv_protocol')}",
                f"defPrivPassphrase {snmp_conf_quote(firewall.get('priv_password'), 'priv_password')}",
            ]
        else:
            lines[1:1] = ["defSecurityLevel authNoPriv"]
    else:
        lines = [
            "defVersion 2c",
            f"defCommunity {snmp_conf_quote(firewall.get('community', 'public'), 'community')}",
        ]
    return "\n".join(lines) + "\n"


def ensure_snmp_image():
    global SNMP_IMAGE
    if SNMP_IMAGE:
        return True
    # No stream may be a terminal here: when only stdout was redirected, some
    # Compose/buildx versions still picked the TTY progress UI from stderr and
    # failed with "failed to get console: provided file is not a console".
    print("Building the Telegraf image for SNMP discovery (the first build can take a few minutes)...")
    try:
        run(
            ["docker", "compose", "build", "telegraf"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "BUILDKIT_PROGRESS": "plain"},
        )
    except subprocess.CalledProcessError as exc:
        if exc.output:
            sys.stderr.write(exc.output)
        raise
    images = capture(["docker", "compose", "images", "-q", "telegraf"]).splitlines()
    SNMP_IMAGE = images[0].strip() if images else ""
    if not SNMP_IMAGE:
        SNMP_IMAGE = capture(
            ["docker", "image", "inspect", "fw-monitoring-telegraf", "--format", "{{.Id}}"],
            check=False,
        ).strip()
    return bool(SNMP_IMAGE)


# The snmp.conf is written inside the throwaway container only; the host and
# OID are passed as positional arguments, never interpolated into the script.
SNMP_CONF_SCRIPT = 'umask 077 && mkdir -p /tmp/snmp && cat > /tmp/snmp/snmp.conf && exec "$@"'


def run_snmp_tool(tool, host, oid, snmp_conf):
    return subprocess.run(
        [
            "docker", "run", "-i", "--rm", "-e", "SNMPCONFPATH=/tmp/snmp", SNMP_IMAGE,
            "sh", "-c", SNMP_CONF_SCRIPT, "snmp-discovery",
            tool, "-Oqv", "-t", str(SNMP_DISCOVERY_TIMEOUT), "-r", str(SNMP_DISCOVERY_RETRIES),
            str(host), str(oid),
        ],
        input=snmp_conf,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def snmp_get(host, oid, snmp_conf):
    if not SNMP_IMAGE:
        return ""
    result = run_snmp_tool("snmpget", host, oid, snmp_conf)
    return result.stdout.strip().strip('"')


def snmp_walk_first(host, oid, snmp_conf):
    if not SNMP_IMAGE:
        return ""
    result = run_snmp_tool("snmpwalk", host, oid, snmp_conf)
    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    return first_line.strip().strip('"')


def detect_palo_model(text):
    match = re.search(r"panPA-?([0-9]{3,5})", text or "", flags=re.IGNORECASE)
    if match:
        return f"PA-{match.group(1)}"
    match = re.search(r"(PA-?[0-9]{3,5})", text or "", flags=re.IGNORECASE)
    if not match:
        return ""
    model = match.group(1).upper()
    return re.sub(r"^PA([0-9])", r"PA-\1", model)


def detect_fortinet_model(text):
    match = re.search(r"(FortiGate[-\s]?[A-Za-z0-9-]+)", text or "")
    if match:
        return re.sub(r"FortiGate[ -]*", "FortiGate-", match.group(1))
    match = re.search(r"(FGT[-\s]?[A-Za-z0-9-]+)", text or "")
    if match:
        return re.sub(r"FGT[ -]*", "FGT-", match.group(1))
    return ""


def discover_paloalto_devices(firewalls, vendors):
    if SNMP_DISCOVERY != "true":
        print(f"SNMP discovery disabled (SNMP_DISCOVERY={SNMP_DISCOVERY})")
        return
    if "paloalto" not in vendors:
        return

    print("Running best-effort Palo Alto SNMP discovery...")
    if not ensure_snmp_image():
        print("    Could not find the Telegraf image for SNMP discovery; skipping.")
        return

    for firewall in firewalls:
        if firewall.get("vendor") != "paloalto":
            continue

        host = firewall.get("host", "")
        hostname = firewall.get("hostname") or host
        if not host:
            print(f"    {hostname}: missing host, skipping discovery.")
            continue

        snmp_conf = build_snmp_conf(firewall)
        sys_descr = snmp_get(host, ".1.3.6.1.2.1.1.1.0", snmp_conf)
        if not sys_descr:
            print(f"    {hostname}: no SNMP response, keeping declared configuration.")
            continue

        sys_object_id = snmp_get(host, ".1.3.6.1.2.1.1.2.0", snmp_conf)
        panos_version = snmp_get(host, ".1.3.6.1.4.1.25461.2.1.2.1.1.0", snmp_conf)
        serial = snmp_get(host, ".1.3.6.1.4.1.25461.2.1.2.1.3.0", snmp_conf)
        vsys_probe = snmp_walk_first(host, ".1.3.6.1.4.1.25461.2.1.2.3.9.1.2", snmp_conf)
        model = detect_palo_model(sys_object_id) or detect_palo_model(sys_descr)

        firewall["discovered"] = True
        firewall["sys_descr"] = sys_descr
        if sys_object_id:
            firewall["sys_object_id"] = sys_object_id
        if panos_version:
            firewall["panos_version"] = panos_version
        if serial:
            firewall["serial"] = serial
        if model:
            firewall["model"] = model
        if vsys_probe:
            firewall["vsys_detected"] = True

        print(f"    {hostname}: SNMP OK{', model ' + model if model else ''}{', PAN-OS ' + panos_version if panos_version else ''}")


def discover_fortinet_devices(firewalls, vendors):
    if SNMP_DISCOVERY != "true" or "fortinet" not in vendors:
        return

    print("Running best-effort Fortinet SNMP discovery...")
    if not ensure_snmp_image():
        print("    Could not find the Telegraf image for SNMP discovery; skipping.")
        return

    for firewall in firewalls:
        if firewall.get("vendor") != "fortinet":
            continue

        host = firewall.get("host", "")
        hostname = firewall.get("hostname") or host
        if not host:
            print(f"    {hostname}: missing host, skipping discovery.")
            continue

        snmp_conf = build_snmp_conf(firewall)
        sys_descr = snmp_get(host, ".1.3.6.1.2.1.1.1.0", snmp_conf)
        if not sys_descr:
            print(f"    {hostname}: no SNMP response, keeping declared configuration.")
            continue

        sys_object_id = snmp_get(host, ".1.3.6.1.2.1.1.2.0", snmp_conf)
        fortios_version = snmp_get(host, ".1.3.6.1.4.1.12356.101.4.1.1.0", snmp_conf)
        serial = snmp_get(host, ".1.3.6.1.4.1.12356.100.1.1.1.0", snmp_conf)
        vdom_probe = snmp_walk_first(host, ".1.3.6.1.4.1.12356.101.3.2.1.1.2", snmp_conf)
        model = detect_fortinet_model(sys_descr)

        firewall["discovered"] = True
        firewall["sys_descr"] = sys_descr
        if sys_object_id:
            firewall["sys_object_id"] = sys_object_id
        if fortios_version:
            firewall["fortios_version"] = fortios_version
        if serial:
            firewall["serial"] = serial
        if model:
            firewall["model"] = model
        if vdom_probe:
            firewall["vdom_detected"] = True

        print(f"    {hostname}: SNMP OK{', model ' + model if model else ''}{', FortiOS ' + fortios_version if fortios_version else ''}")


def detect_vendors(firewalls):
    print("Analyzing declared firewalls...")
    vendors = sorted({firewall.get("vendor", "paloalto") for firewall in firewalls})
    print(f"    Detected vendors: {', '.join(vendors)}")
    return vendors


def prepare_paloalto_mibs(firewalls, vendors):
    if "paloalto" not in vendors:
        print("No Palo Alto firewall declared; skipping Palo Alto MIB step.")
        return

    print("Preparing Palo Alto MIBs...")
    versions = sorted(
        {
            "-".join(str(firewall["panos_version"]).split(".")[:2])
            for firewall in firewalls
            if firewall.get("vendor") == "paloalto" and firewall.get("panos_version")
        }
    )
    if not versions:
        versions = [DEFAULT_PALO_MIB_VERSION]
        print(f"    No PAN-OS version discovered; using default Palo Alto MIB version {DEFAULT_PALO_MIB_VERSION}.")

    for version in versions:
        dot_version = version.replace("-", ".")
        zip_name = f"pan-{version}-snmp-mib-modules.zip"
        zip_path = MIB_DIR / zip_name
        # The vendor archive extracts files without a version in their names
        # (PAN-COMMON-MIB.my, ...), so a marker records which archive was
        # already extracted and avoids re-downloading it on every run.
        marker = MIB_DIR / f".{zip_name}.extracted"
        if marker.is_file() or glob.glob(str(MIB_DIR / f"PAN-*-{dot_version}*.my")):
            continue
        urls = [
            f"https://docs.paloaltonetworks.com/content/dam/techdocs/en_US/zip/snmp-mib/{zip_name}",
            f"https://docs.paloaltonetworks.com/content/dam/techdocs/en_US/snmp-mibs/{zip_name}",
        ]
        print(f"    Downloading {zip_name}")
        for url in urls:
            try:
                urllib.request.urlretrieve(url, zip_path)
                print(f"      Downloaded from {url}")
                break
            except urllib.error.URLError:
                continue
        else:
            raise SystemExit(f"ERROR: could not download {zip_name}")

        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(MIB_DIR)
        zip_path.unlink(missing_ok=True)
        marker.write_text(f"{zip_name}\n", encoding="utf-8")

    print(f"Palo Alto MIBs ready in {MIB_DIR}")


def convert_legacy_template(text):
    converted = []
    stack = []
    control_patterns = [
        (r'^\{\{-?\s*range\s+\$fw\s*:=\s*\.firewalls\s*\}\}$', lambda _m: ("for", "{% for fw in firewalls %}")),
        (r'^\{\{-?\s*if\s+eq\s+\(\$fw\.vendor\s+\|\s+default\s+"paloalto"\)\s+"paloalto"\s*\}\}$', lambda _m: ("if", "{% if fw.get('vendor', 'paloalto') == 'paloalto' %}")),
        (r'^\{\{-?\s*if\s+eq\s+\$fw\.vendor\s+"fortinet"\s*\}\}$', lambda _m: ("if", "{% if fw.get('vendor') == 'fortinet' %}")),
        (r'^\{\{-?\s*if\s+eq\s+\$fw\.snmp_version\s+2\s*\}\}$', lambda _m: ("if", "{% if fw.get('snmp_version') == 2 %}")),
        (r'^\{\{-?\s*else\s+if\s+eq\s+\$fw\.snmp_version\s+3\s*\}\}$', lambda _m: (None, "{% elif fw.get('snmp_version') == 3 %}")),
        (r'^\{\{-?\s*if\s+\(\$fw\.([a-zA-Z0-9_]+)\s+\|\s+default\s+""\)\s*\}\}$', lambda m: ("if", f"{{% if fw.get('{m.group(1)}') %}}")),
        (r'^\{\{-?\s*if\s+\(\$fw\.([a-zA-Z0-9_]+)\s+\|\s+default\s+false\)\s*\}\}$', lambda m: ("if", f"{{% if fw.get('{m.group(1)}') %}}")),
        (r'^\{\{-?\s*if\s+\$fw\.([a-zA-Z0-9_]+)\s*\}\}$', lambda m: ("if", f"{{% if fw.get('{m.group(1)}') %}}")),
        (r'^\{\{-?\s*if\s+or\s+\(\$fw\.panos_11_2_metrics\s+\|\s+default\s+false\)\s+\(\$fw\.panos_12_metrics\s+\|\s+default\s+false\)\s*\}\}$', lambda _m: ("if", "{% if fw.get('panos_11_2_metrics') or fw.get('panos_12_metrics') %}")),
        (r'^\{\{-?\s*if\s+and\s+\(\$fw\.vsys_total_cps\s+\|\s+default\s+false\)\s+\(\$fw\.panos_12_metrics\s+\|\s+default\s+false\)\s*\}\}$', lambda _m: ("if", "{% if fw.get('vsys_total_cps') and fw.get('panos_12_metrics') %}")),
    ]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.match(r"^\{\{-?\s*end\s*\}\}$", line):
            if not stack:
                raise SystemExit("ERROR: template has an unmatched end block.")
            block = stack.pop()
            converted.append("{% endfor %}" if block == "for" else "{% endif %}")
            continue

        handled = False
        for pattern, replacement in control_patterns:
            match = re.match(pattern, line)
            if not match:
                continue
            block, replacement_line = replacement(match)
            if block:
                stack.append(block)
            converted.append(replacement_line)
            handled = True
            break
        if handled:
            continue

        raw_line = re.sub(r"\{\{\s*strings\.ToUpper\s+\$fw\.([a-zA-Z0-9_]+)\s*\}\}", r"{{ fw.\1 | upper }}", raw_line)
        raw_line = re.sub(r"\{\{\s*-?\s*\$fw\.([a-zA-Z0-9_]+)\s*\}\}", r"{{ fw.\1 }}", raw_line)
        converted.append(raw_line)

    if stack:
        raise SystemExit("ERROR: template has an unclosed block.")
    return "\n".join(converted) + "\n"


def render_template(template_name, context):
    template_path = PROJECT_DIR / "telegraf" / template_name
    text = template_path.read_text(encoding="utf-8")
    if "{{-" in text or "$fw" in text:
        text = convert_legacy_template(text)
    env = Environment(trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    return env.from_string(text).render(context)


def render_telegraf(firewalls, vendors):
    conf_out = PROJECT_DIR / "telegraf" / "telegraf.conf"
    print("Generating telegraf.conf...")

    context = {"firewalls": firewalls}
    parts = [render_template("header.tmpl", context)]
    if "paloalto" in vendors:
        parts.append(render_template("inputs_paloalto.tmpl", context))
    if "fortinet" in vendors:
        parts.append(render_template("inputs_fortinet.tmpl", context))
    if any(firewall.get("api_monitoring", {}).get("enabled") for firewall in firewalls):
        parts.append(render_template("inputs_paloalto_api.tmpl", context))

    conf_out.write_text("\n".join(parts), encoding="utf-8")
    # The official image drops privileges before reading this bind mount. The
    # file is therefore world-readable, but contains environment references
    # rather than credential values.
    conf_out.chmod(0o644)
    print("telegraf/telegraf.conf ready")


def render_paloalto_api_inventory(firewalls):
    api_firewalls = []
    for firewall in firewalls:
        config = firewall.get("api_monitoring", {})
        if firewall.get("vendor") != "paloalto" or not config.get("enabled"):
            continue
        api_firewalls.append(
            {
                "hostname": firewall["hostname"],
                "host": config["host"],
                "api_key_env": config["runtime_api_key_env"],
                "port": config["port"],
                "verify_tls": config["verify_tls"],
                "timeout": config["timeout"],
                "interval": config["interval"],
                "resource_interval": config["resource_interval"],
                "counter_interval": config["counter_interval"],
                "counter_limit": config["counter_limit"],
                "system_interval": config["system_interval"],
            }
        )
    PALOALTO_API_INVENTORY.write_text(json.dumps(api_firewalls, indent=2) + "\n", encoding="utf-8")
    try:
        display_path = PALOALTO_API_INVENTORY.relative_to(PROJECT_DIR)
    except ValueError:
        display_path = PALOALTO_API_INVENTORY
    print(f"{display_path} ready ({len(api_firewalls)} API firewall(s))")


def load_dotenv(path):
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def compose_environment_value(value, field="monitoring credential"):
    """Encode a value for the Docker Compose env_file (compose-go dotenv parser).

    Encoding rules (the collector's load_environment_file must mirror them):
    the value is wrapped in double quotes, and each backslash, double quote
    and dollar sign is prefixed with one backslash. Every other character
    (single quotes, #, =, spaces, tabs, UTF-8) is written verbatim. This
    round-trips exactly through Docker Compose v2 (verified empirically);
    single quotes do not, because Compose keeps a doubled backslash literally
    and cannot represent a trailing backslash. Line breaks and NUL bytes
    cannot be represented and are rejected; the error names the field, never
    the value.
    """
    value = str(value)
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SystemExit(f"ERROR: {field} cannot contain newlines or NUL bytes.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def snmp_runtime_environment_name(firewall, field):
    hostname = re.sub(r"[^A-Za-z0-9]+", "_", str(firewall["hostname"])).strip("_").upper()
    identity = f"{firewall['hostname']}\0{firewall['host']}\0{field}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:8].upper()
    return f"FIREWALL_SNMP_{hostname}_{field.upper()}_{suffix}"


def render_paloalto_api_environment(firewalls, source=None):
    source_path = Path(source or PROJECT_DIR / ".env")
    source_values = load_dotenv(source_path)
    runtime_values = {}
    rendered_firewalls = copy.deepcopy(firewalls)
    missing = []
    for firewall, rendered_firewall in zip(firewalls, rendered_firewalls):
        secret_fields = ("community",) if firewall.get("snmp_version") == 2 else ("auth_password", "priv_password")
        for field in secret_fields:
            value = firewall.get(field)
            if value is None:
                continue
            runtime_name = snmp_runtime_environment_name(firewall, field)
            runtime_values[runtime_name] = str(value)
            rendered_firewall[field] = f"${runtime_name}"

        config = firewall.get("api_monitoring", {})
        if firewall.get("vendor") != "paloalto" or not config.get("enabled"):
            continue
        runtime_name = config["runtime_api_key_env"]
        if config.get("api_key"):
            value = config["api_key"]
        else:
            value = source_values.get(config["api_key_env"])
            if not value:
                missing.append(config["api_key_env"])
                continue
        existing = runtime_values.get(runtime_name)
        if existing is not None and existing != value:
            raise SystemExit(f"ERROR: conflicting Palo Alto API keys resolve to {runtime_name}.")
        runtime_values[runtime_name] = value
    if missing:
        raise SystemExit(
            "ERROR: missing Palo Alto API key environment variable(s) in .env: " + ", ".join(sorted(set(missing)))
        )
    content = "".join(
        f"{name}={compose_environment_value(runtime_values[name], name)}\n"
        for name in sorted(runtime_values)
    )
    PALOALTO_API_ENV.write_text(content, encoding="utf-8")
    PALOALTO_API_ENV.chmod(0o600)
    print(f"telegraf/paloalto-api.env ready ({len(runtime_values)} monitoring secret(s); values hidden)")
    return rendered_firewalls


def check_paloalto_api(config, api_key):
    """Run one read-only ``show system info``; return a one-line status, never the key."""
    request = urllib.request.Request(
        f"https://{config['host']}:{config['port']}/api/",
        data=urllib.parse.urlencode({"type": "op", "cmd": "<show><system><info></info></system></show>"}).encode(),
        headers={"X-PAN-KEY": api_key, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if config.get("verify_tls", True):
        context = ssl.create_default_context()
    else:
        context = ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
    timeout = min(API_CHECK_TIMEOUT, int(config.get("timeout", API_CHECK_TIMEOUT)))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return f"API key rejected (HTTP {exc.code}); regenerate it with paloalto_api_key.py"
        return f"API error (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "TLS certificate not trusted; install a trusted certificate or set api_monitoring.verify_tls: false"
        return f"API unreachable ({reason})"
    except OSError as exc:
        return f"API unreachable ({exc})"
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return "API returned invalid XML"
    if root.attrib.get("status") != "success":
        message = " ".join(text.strip() for text in root.itertext() if text.strip())
        return f"API request refused ({message or 'no detail'})"
    model = (root.findtext(".//system/model") or "").strip()
    version = (root.findtext(".//system/sw-version") or "").strip()
    return f"API OK{', model ' + model if model else ''}{', PAN-OS ' + version if version else ''}"


def check_paloalto_api_access(firewalls, source=None):
    """Best-effort reachability and key check for every enabled API firewall."""
    targets = [
        firewall for firewall in firewalls
        if firewall.get("vendor") == "paloalto" and firewall.get("api_monitoring", {}).get("enabled")
    ]
    if not targets:
        return
    if API_CHECK != "true":
        print(f"Palo Alto API check disabled (API_CHECK={API_CHECK})")
        return
    source_path = Path(source or PROJECT_DIR / ".env")
    source_values = load_dotenv(source_path) if source_path.exists() else {}

    def probe(firewall):
        config = firewall["api_monitoring"]
        api_key = config.get("api_key") or source_values.get(config.get("api_key_env", ""))
        if not api_key:
            return "API key not found, skipping"
        try:
            return check_paloalto_api(config, api_key)
        except Exception as exc:  # best effort: never stop generation
            return f"API check failed ({type(exc).__name__})"

    print("Checking Palo Alto XML API access...")
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        for firewall, status in zip(targets, executor.map(probe, targets)):
            print(f"    {firewall['hostname']}: {status}")


def start_stack():
    print("Building Telegraf image...")
    run(["docker", "compose", "build", "telegraf"])
    print("Starting or refreshing the Docker stack...")
    run(["docker", "compose", "up", "-d"])
    run(["docker", "compose", "ps"])
    print("Stack is operational.")


def main():
    check_docker()
    check_env_file()
    prepare_runtime_dirs()

    firewalls = load_inventory(PROJECT_DIR / "firewalls.yml")
    validate_inventory(firewalls)
    vendors = detect_vendors(firewalls)

    print("Enriching inventory automatically...")
    enrich_inventory(firewalls)
    discover_paloalto_devices(firewalls, vendors)
    discover_fortinet_devices(firewalls, vendors)
    enrich_inventory(firewalls)
    save_inventory(firewalls, ENRICHED_FIREWALLS)

    prepare_paloalto_mibs(firewalls, vendors)
    render_paloalto_api_inventory(firewalls)
    rendered_firewalls = render_paloalto_api_environment(firewalls)
    check_paloalto_api_access(firewalls)
    render_telegraf(rendered_firewalls, vendors)
    start_stack()


if __name__ == "__main__":
    _restore_logging = configure_logging()
    try:
        main()
    finally:
        _restore_logging()
