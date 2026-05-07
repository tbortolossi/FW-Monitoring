#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_PYTHON="$VENV_DIR/bin/python"

is_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]]
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
  if command -v docker >/dev/null 2>&1; then
    return
  fi

  install_docker_debian
}

ensure_docker

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

if [[ ! -x "$VENV_PYTHON" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$SCRIPT_DIR/requirements.txt"

exec "$VENV_PYTHON" "$SCRIPT_DIR/generate.py" "$@"
