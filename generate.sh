#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_PYTHON="$VENV_DIR/bin/python"
DOCKER_GROUP="docker"

is_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]]
}

# The operator behind `sudo ./generate.sh`, or nothing for a plain root login.
sudo_operator() {
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    printf '%s\n' "$SUDO_USER"
  fi
}

# True when the account is a member of the docker group in the group database,
# even if the current login session does not carry that group yet.
user_in_docker_group() {
  id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qx "$DOCKER_GROUP"
}

install_docker_debian() {
  if ! is_root; then
    echo "ERROR: Docker is not installed." >&2
    echo "Rerun this wrapper with sudo so it can install Docker:" >&2
    echo "  sudo ./generate.sh" >&2
    exit 1
  fi

  if [[ ! -r /etc/os-release ]]; then
    echo "ERROR: Docker is not installed and this host is not a supported Debian/Ubuntu system." >&2
    echo "Install Docker manually, then rerun ./generate.sh." >&2
    exit 1
  fi

  # shellcheck disable=SC1091
  . /etc/os-release

  local docker_os=""
  case "${ID:-}" in
    debian|ubuntu)
      docker_os="$ID"
      ;;
    *)
      if [[ " ${ID_LIKE:-} " == *" debian "* ]]; then
        docker_os="debian"
      else
        echo "ERROR: Docker automatic install supports Debian/Ubuntu only." >&2
        echo "Install Docker manually, then rerun ./generate.sh." >&2
        exit 1
      fi
      ;;
  esac

  local codename="${VERSION_CODENAME:-}"
  if [[ -z "$codename" ]]; then
    echo "ERROR: Could not detect the Debian/Ubuntu codename for Docker repository setup." >&2
    echo "Install Docker manually, then rerun ./generate.sh." >&2
    exit 1
  fi

  echo "Installing Docker Engine and Compose plugin from the official Docker repository..."
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  rm -f /etc/apt/keyrings/docker.gpg
  curl -fsSL "https://download.docker.com/linux/${docker_os}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  local arch
  arch="$(dpkg --print-architecture)"
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${docker_os} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker
  elif command -v service >/dev/null 2>&1; then
    service docker start
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    install_docker_debian
  fi

  if ! docker compose version >/dev/null 2>&1; then
    if [[ -f /etc/debian_version ]] && is_root; then
      install_docker_debian
    else
      echo "ERROR: Docker Compose v2 is required." >&2
      echo "Install the Docker Compose plugin, or rerun this wrapper with sudo on Debian/Ubuntu:" >&2
      echo "  sudo ./generate.sh" >&2
      exit 1
    fi
  fi
}

# Add the sudo operator to the docker group, whether this wrapper installed
# Docker or it was already there, so the next run works without sudo.
grant_docker_group() {
  local user="$1"
  if user_in_docker_group "$user"; then
    echo "$user is already in the $DOCKER_GROUP group."
    return
  fi
  if ! getent group "$DOCKER_GROUP" >/dev/null 2>&1; then
    groupadd "$DOCKER_GROUP"
  fi
  usermod -aG "$DOCKER_GROUP" "$user"
  echo "Added $user to the $DOCKER_GROUP group."
}

# `sudo ./generate.sh` only prepares the host. Continuing as root would leave
# .venv and the generated files owned by root, and the next run as the
# operator would fail on them.
finish_sudo_setup() {
  local user="$1"
  grant_docker_group "$user"
  echo
  echo "Docker is ready. Now run the generator as $user, without sudo:"
  echo "  ./generate.sh"
  echo "No need to log out first: the wrapper picks up the new $DOCKER_GROUP group for that run."
  echo "Note: members of the $DOCKER_GROUP group have root-equivalent access to this host."
}

# Paths the wrapper or generate.py rewrite on every run. A root-owned one is
# left over from an older `sudo ./generate.sh` and makes the run fail midway.
# logs/telegraf content belongs to the Telegraf container and is not checked.
root_owned_paths() {
  local path
  for path in .venv telegraf/mibs; do
    if [[ -e "$SCRIPT_DIR/$path" && -n "$(find "$SCRIPT_DIR/$path" -user 0 -print -quit 2>/dev/null)" ]]; then
      printf '%s\n' "$path"
    fi
  done
  for path in .env .firewalls.generated.yml telegraf/telegraf.conf telegraf/paloalto-api.env \
    telegraf/paloalto-api.json logs logs/telegraf; do
    if [[ -e "$SCRIPT_DIR/$path" && -n "$(find "$SCRIPT_DIR/$path" -maxdepth 0 -user 0 2>/dev/null)" ]]; then
      printf '%s\n' "$path"
    fi
  done
  if [[ -d "$SCRIPT_DIR/logs" ]]; then
    (cd "$SCRIPT_DIR" && find logs -maxdepth 1 -name 'generate-*.log' -user 0 2>/dev/null) || true
  fi
}

check_file_ownership() {
  local owned
  owned="$(root_owned_paths)"
  if [[ -z "$owned" ]]; then
    return
  fi
  echo "ERROR: these project files belong to root, left over from an earlier 'sudo ./generate.sh':" >&2
  local -a paths
  mapfile -t paths <<<"$owned"
  printf '  %s\n' "${paths[@]}" >&2
  echo "Give them back to your user, then rerun ./generate.sh:" >&2
  echo "  cd $(printf '%q' "$SCRIPT_DIR") && sudo chown -R $(id -un): ${paths[*]}" >&2
  exit 1
}

# Make the Docker socket usable by the current user. A user added to the
# docker group keeps the old group list until the next login; `sg` starts this
# wrapper again with the group active so the first run works right away.
ensure_docker_access() {
  local error
  if error="$(docker info 2>&1 >/dev/null)"; then
    return
  fi
  if [[ "${error,,}" != *"permission denied"* ]]; then
    # Stopped daemon and other failures are explained by generate.py.
    return
  fi

  local user
  user="$(id -un)"
  if user_in_docker_group "$user" && [[ -z "${FW_MONITORING_DOCKER_SG:-}" ]] && command -v sg >/dev/null 2>&1; then
    echo "Using the $DOCKER_GROUP group for this run (log out and back in to make it permanent)."
    export FW_MONITORING_DOCKER_SG=1
    exec sg "$DOCKER_GROUP" -c "$(printf '%q ' bash "$SCRIPT_DIR/generate.sh" "$@")"
  fi

  echo "ERROR: $user cannot use Docker (permission denied on /var/run/docker.sock)." >&2
  if user_in_docker_group "$user"; then
    echo "$user is in the $DOCKER_GROUP group but this session does not have it yet." >&2
    echo "Log out and back in, check 'docker ps', then rerun ./generate.sh." >&2
  else
    echo "Run the wrapper once with sudo to add $user to the $DOCKER_GROUP group:" >&2
    echo "  sudo ./generate.sh" >&2
    echo "then run ./generate.sh again as $user." >&2
  fi
  exit 1
}

main() {
  ensure_docker

  if is_root; then
    local operator
    operator="$(sudo_operator)"
    if [[ -n "$operator" ]]; then
      finish_sudo_setup "$operator"
      return 0
    fi
  else
    check_file_ownership
    ensure_docker_access "$@"
  fi

  if [[ ! -x "$VENV_PYTHON" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  "$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$SCRIPT_DIR/requirements.txt"

  exec "$VENV_PYTHON" "$SCRIPT_DIR/generate.py" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
