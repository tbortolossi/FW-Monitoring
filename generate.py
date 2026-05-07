#!/usr/bin/env python3
import datetime as _datetime
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
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
os.chdir(PROJECT_DIR)

LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"generate-{_datetime.datetime.now():%Y%m%d-%H%M%S}.log"


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


_original_stdout = sys.stdout
_original_stderr = sys.stderr
_log_handle = LOG_FILE.open("a", encoding="utf-8")
sys.stdout = Tee(_original_stdout, _log_handle)
sys.stderr = Tee(_original_stderr, _log_handle)

print(f"Logging to {LOG_FILE}")

MIB_DIR = PROJECT_DIR / "telegraf" / "mibs" / "paloalto"
ENRICHED_FIREWALLS = PROJECT_DIR / ".firewalls.generated.yml"
SNMP_DISCOVERY = os.environ.get("SNMP_DISCOVERY", "true").lower()
DEFAULT_PALO_MIB_VERSION = os.environ.get("PALO_MIB_VERSION", "11-2")
SNMP_IMAGE = ""


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


def check_env_file():
    if not (PROJECT_DIR / ".env").is_file():
        raise SystemExit(
            "ERROR: .env is missing.\n"
            "Copy .env.example to .env and change every secret before running this script:\n"
            "  cp .env.example .env"
        )


def prepare_runtime_dirs():
    grafana_data = PROJECT_DIR / "grafana-data"
    telegraf_logs = PROJECT_DIR / "logs" / "telegraf"
    if not grafana_data.is_dir():
        print(f"Creating {grafana_data}")
        grafana_data.mkdir(parents=True)
    telegraf_logs.mkdir(parents=True, exist_ok=True)

    if is_root():
        print(f"Fixing permissions on {grafana_data} (UID 472)...")
        for root, dirs, files in os.walk(grafana_data):
            for name in dirs + files:
                os.chown(Path(root) / name, 472, 472)
        os.chown(grafana_data, 472, 472)
    else:
        print(f"Not running as root; allowing Grafana container UID 472 to write to {grafana_data}.")
        grafana_data.chmod(0o777)
    telegraf_logs.chmod(0o777)

    MIB_DIR.mkdir(parents=True, exist_ok=True)


def load_inventory(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or []
    if not isinstance(data, list):
        raise SystemExit("ERROR: firewalls.yml must be a YAML list of firewall objects.")
    for index, firewall in enumerate(data):
        if not isinstance(firewall, dict):
            raise SystemExit(f"ERROR: firewalls.yml entry #{index + 1} must be a mapping.")
    return data


def save_inventory(firewalls, path):
    with Path(path).open("w", encoding="utf-8") as handle:
        yaml.safe_dump(firewalls, handle, sort_keys=False, default_flow_style=False)


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


def enrich_inventory(firewalls):
    for firewall in firewalls:
        if normalize_vendor(firewall.get("vendor")) != "paloalto":
            continue

        panos_version = firewall.get("panos_version")
        if panos_version:
            firewall["panos_11_2_metrics"] = pan_at_least(panos_version, 11, 2)
            firewall["panos_12_metrics"] = pan_at_least(panos_version, 12, 1)
            firewall["vsys_total_cps"] = pan_at_least(panos_version, 12, 1)
            firewall["interface_utilization"] = pan_at_least(panos_version, 12, 1)

        model = firewall.get("model") or firewall.get("chassis_model") or firewall.get("hostname")
        if "chassis" not in firewall:
            firewall["chassis"] = is_palo_chassis(model)
        if "pan_entity_ext" not in firewall:
            firewall["pan_entity_ext"] = bool(firewall.get("chassis"))
        if not firewall.get("chassis_family"):
            firewall["chassis_family"] = chassis_family(model)


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


def ensure_snmp_image():
    global SNMP_IMAGE
    if SNMP_IMAGE:
        return True
    run(["docker", "compose", "build", "telegraf"], stdout=subprocess.DEVNULL)
    images = capture(["docker", "compose", "images", "-q", "telegraf"]).splitlines()
    SNMP_IMAGE = images[0].strip() if images else ""
    if not SNMP_IMAGE:
        SNMP_IMAGE = capture(
            ["docker", "image", "inspect", "fw-monitoring-telegraf", "--format", "{{.Id}}"],
            check=False,
        ).strip()
    return bool(SNMP_IMAGE)


def snmp_get(host, oid, snmp_args):
    if not SNMP_IMAGE:
        return ""
    result = subprocess.run(
        ["docker", "run", "--rm", SNMP_IMAGE, "snmpget", "-Oqv", "-t", "1", "-r", "1", *snmp_args, host, oid],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip().strip('"')


def snmp_walk_first(host, oid, snmp_args):
    if not SNMP_IMAGE:
        return ""
    result = subprocess.run(
        ["docker", "run", "--rm", SNMP_IMAGE, "snmpwalk", "-Oqv", "-t", "1", "-r", "0", *snmp_args, host, oid],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
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

        snmp_args = build_snmp_args(firewall)
        sys_descr = snmp_get(host, ".1.3.6.1.2.1.1.1.0", snmp_args)
        if not sys_descr:
            print(f"    {hostname}: no SNMP response, keeping declared configuration.")
            continue

        sys_object_id = snmp_get(host, ".1.3.6.1.2.1.1.2.0", snmp_args)
        panos_version = snmp_get(host, ".1.3.6.1.4.1.25461.2.1.2.1.1.0", snmp_args)
        serial = snmp_get(host, ".1.3.6.1.4.1.25461.2.1.2.1.3.0", snmp_args)
        vsys_probe = snmp_walk_first(host, ".1.3.6.1.4.1.25461.2.1.2.3.9.1.2", snmp_args)
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

        snmp_args = build_snmp_args(firewall)
        sys_descr = snmp_get(host, ".1.3.6.1.2.1.1.1.0", snmp_args)
        if not sys_descr:
            print(f"    {hostname}: no SNMP response, keeping declared configuration.")
            continue

        sys_object_id = snmp_get(host, ".1.3.6.1.2.1.1.2.0", snmp_args)
        fortios_version = snmp_get(host, ".1.3.6.1.4.1.12356.101.4.1.1.0", snmp_args)
        serial = snmp_get(host, ".1.3.6.1.4.1.12356.100.1.1.1.0", snmp_args)
        vdom_probe = snmp_walk_first(host, ".1.3.6.1.4.1.12356.101.3.2.1.1.2", snmp_args)
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
        if glob.glob(str(MIB_DIR / f"PAN-*-{dot_version}*.my")):
            continue

        zip_name = f"pan-{version}-snmp-mib-modules.zip"
        zip_path = MIB_DIR / zip_name
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

    conf_out.write_text("\n".join(parts), encoding="utf-8")
    print("telegraf/telegraf.conf ready")


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
    render_telegraf(firewalls, vendors)
    start_stack()


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout = _original_stdout
        sys.stderr = _original_stderr
        _log_handle.close()
