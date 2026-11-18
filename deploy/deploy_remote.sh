#!/usr/bin/env bash
set -euo pipefail

# Zero-Fallback Remote Deployment Script for Korea Cloud Server
# Usage:
#   ./deploy/deploy_remote.sh --host <SERVER_IP> --key <SSH_PRIVATE_KEY> --wheel <PATH_TO_WHL> [options]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_HOST=""
REMOTE_USER="root"
SSH_PORT="22"
SSH_KEY=""
WHEEL_PATH=""
TESTS_DIR="${REPO_ROOT}/tests"
CADDYFILE_PATH="${SCRIPT_DIR}/Caddyfile.example"
CPA_UNIT_PATH="${SCRIPT_DIR}/cpa.service"
BOT_UNIT_PATH="${SCRIPT_DIR}/xiaoshuo-bot.service"
MIN_DISK_GB="5"

usage() {
  cat <<'EOF'
Usage: deploy_remote.sh --host <SERVER_IP> --key <SSH_KEY> --wheel <WHEEL_FILE> [options]

Required:
  --host <ip>          Target Korea server IP or hostname (no hardcoded default)
  --key <path>         Path to SSH private key
  --wheel <path>       Path to built xiaoshuo_bot-*.whl release package

Optional:
  --user <name>        Remote SSH user (default: root)
  --port <num>         Remote SSH port (default: 22)
  --tests-dir <dir>    Local tests directory to run in remote venv (default: <repo>/tests)
  --caddyfile <path>   Caddyfile to install (default: deploy/Caddyfile.example)
  --min-disk-gb <num>  Minimum free GiB required on /opt (default: 5)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?Missing value for --host}"
      shift 2
      ;;
    --user)
      REMOTE_USER="${2:?Missing value for --user}"
      shift 2
      ;;
    --port)
      SSH_PORT="${2:?Missing value for --port}"
      shift 2
      ;;
    --key)
      SSH_KEY="${2:?Missing value for --key}"
      shift 2
      ;;
    --wheel)
      WHEEL_PATH="${2:?Missing value for --wheel}"
      shift 2
      ;;
    --tests-dir)
      TESTS_DIR="${2:?Missing value for --tests-dir}"
      shift 2
      ;;
    --caddyfile)
      CADDYFILE_PATH="${2:?Missing value for --caddyfile}"
      shift 2
      ;;
    --min-disk-gb)
      MIN_DISK_GB="${2:?Missing value for --min-disk-gb}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${REMOTE_HOST}" || -z "${SSH_KEY}" || -z "${WHEEL_PATH}" ]]; then
  echo "ERROR: --host, --key, and --wheel are strictly required." >&2
  usage >&2
  exit 1
fi

if [[ ! -f "${SSH_KEY}" ]]; then
  echo "ERROR: SSH key file not found: ${SSH_KEY}" >&2
  exit 1
fi

if [[ ! -f "${WHEEL_PATH}" ]]; then
  echo "ERROR: Release wheel not found: ${WHEEL_PATH}" >&2
  exit 1
fi

if [[ ! -d "${TESTS_DIR}" ]]; then
  echo "ERROR: Tests directory not found: ${TESTS_DIR}" >&2
  exit 1
fi

for required_file in "${CPA_UNIT_PATH}" "${BOT_UNIT_PATH}" "${CADDYFILE_PATH}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "ERROR: Required deploy artifact not found: ${required_file}" >&2
    exit 1
  fi
done

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(
  -i "${SSH_KEY}"
  -p "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)
SCP_OPTS=(
  -i "${SSH_KEY}"
  -P "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)

RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_STAGE="/opt/xiaoshuo-bot/_releases/${RELEASE_ID}"
WHEEL_BASENAME="$(basename "${WHEEL_PATH}")"

echo "[1/7] Running remote pre-flight checks (disk >= ${MIN_DISK_GB}G, python3 >= 3.10, permissions 0600/0700)..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${MIN_DISK_GB}" "${REMOTE_STAGE}" <<'REMOTE_PREFLIGHT'
set -euo pipefail
min_gb="$1"
stage_dir="$2"

mkdir -p /opt/xiaoshuo-bot /opt/xiaoshuo-bot/data /opt/xiaoshuo-bot/projects /opt/xiaoshuo-bot/_releases /opt/xiaoshuo-bot/_archive
mkdir -p /opt/cli-proxy-api /opt/cli-proxy-api/auth-files
mkdir -p /var/log/xiaoshuo-bot /var/log/cli-proxy-api
mkdir -p "${stage_dir}"

avail_kb="$(df -Pk /opt | awk 'NR==2 {print $4}')"
min_kb="$((min_gb * 1024 * 1024))"
if [[ "${avail_kb}" -lt "${min_kb}" ]]; then
  echo "ERROR: Insufficient disk space on /opt: ${avail_kb} KB available, require >= ${min_kb} KB (${min_gb} GiB)." >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "ERROR: python3 is not installed on target server." >&2
  exit 1
fi

python3 -c 'import sys; assert sys.version_info >= (3, 10), f"Need Python >= 3.10, got {sys.version}"'

if ! getent passwd xiaoshuo >/dev/null; then
  useradd --system --home-dir /opt/xiaoshuo-bot --shell /usr/sbin/nologin xiaoshuo
fi

if [[ ! -f /etc/xiaoshuo-bot/xiaoshuo.env ]]; then
  echo "ERROR: Missing /etc/xiaoshuo-bot/xiaoshuo.env; provision secrets with 0600 permissions before deploying." >&2
  exit 1
fi

if grep -q '__REPLACE_WITH_' /etc/xiaoshuo-bot/xiaoshuo.env; then
  echo "ERROR: /etc/xiaoshuo-bot/xiaoshuo.env still contains unreplaced __REPLACE_WITH_ placeholders." >&2
  exit 1
fi

# The admin API is reachable through Caddy even though it binds 127.0.0.1, so a strong
# XIAOSHUO_ADMIN_TOKEN is mandatory (server/auth.py refuses to start without one as well).
admin_token_line="$(grep -E '^[[:space:]]*XIAOSHUO_ADMIN_TOKEN=' /etc/xiaoshuo-bot/xiaoshuo.env | tail -n 1 || echo '')"
admin_token_val="$(printf '%s' "${admin_token_line#*=}" | tr -d "[:space:]\"'")"
if [[ -z "${admin_token_line}" || "${#admin_token_val}" -lt 24 ]]; then
  echo "ERROR: /etc/xiaoshuo-bot/xiaoshuo.env must set XIAOSHUO_ADMIN_TOKEN with >= 24 characters." >&2
  exit 1
fi
if [[ "${admin_token_val^^}" == *REPLACE* ]]; then
  echo "ERROR: XIAOSHUO_ADMIN_TOKEN in /etc/xiaoshuo-bot/xiaoshuo.env is still a REPLACE placeholder." >&2
  exit 1
fi

if [[ ! -f /opt/cli-proxy-api/config.yaml ]]; then
  echo "ERROR: Missing /opt/cli-proxy-api/config.yaml; install CPA config before deploying." >&2
  exit 1
fi

if grep -q '__REPLACE_WITH_' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml still contains unreplaced __REPLACE_WITH_ placeholders." >&2
  exit 1
fi

if [[ ! -x /opt/cli-proxy-api/cli-proxy-api ]]; then
  echo "ERROR: Missing or non-executable CPA binary at /opt/cli-proxy-api/cli-proxy-api." >&2
  exit 1
fi

if ! grep -Eq '^[[:space:]]*host:[[:space:]]*"?127\.0\.0\.1"?' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml must bind host: \"127.0.0.1\"." >&2
  exit 1
fi

if ! grep -Eq '^[[:space:]]*allow-remote:[[:space:]]*false([[:space:]]*(#.*)?)?$' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml must set remote-management.allow-remote: false." >&2
  exit 1
fi

if ! grep -Eq '^[[:space:]]*switch-preview-model:[[:space:]]*false([[:space:]]*(#.*)?)?$' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml must set quota-exceeded.switch-preview-model: false." >&2
  exit 1
fi

if ! grep -Eq '^[[:space:]]*disable-image-generation:[[:space:]]*false([[:space:]]*(#.*)?)?$' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml must set disable-image-generation: false." >&2
  exit 1
fi

chown root:root /etc/xiaoshuo-bot/xiaoshuo.env
chmod 0600 /etc/xiaoshuo-bot/xiaoshuo.env
env_mode="$(stat -c '%a' /etc/xiaoshuo-bot/xiaoshuo.env)"
if [[ "${env_mode}" != "600" ]]; then
  echo "ERROR: /etc/xiaoshuo-bot/xiaoshuo.env mode is ${env_mode}, expected 600." >&2
  exit 1
fi

chown xiaoshuo:xiaoshuo /opt/cli-proxy-api/auth-files
chmod 0700 /opt/cli-proxy-api/auth-files
auth_mode="$(stat -c '%a' /opt/cli-proxy-api/auth-files)"
if [[ "${auth_mode}" != "700" ]]; then
  echo "ERROR: /opt/cli-proxy-api/auth-files mode is ${auth_mode}, expected 700." >&2
  exit 1
fi

chmod 0600 /opt/cli-proxy-api/config.yaml
chown xiaoshuo:xiaoshuo /opt/cli-proxy-api/config.yaml /var/log/cli-proxy-api
chown -R xiaoshuo:xiaoshuo /opt/xiaoshuo-bot/data /opt/xiaoshuo-bot/projects /var/log/xiaoshuo-bot
REMOTE_PREFLIGHT

echo "[2/7] Syncing release wheel, systemd units, Caddyfile, tests, and read-only test fixtures to ${REMOTE_STAGE}..."
for required_fixture in \
  "${REPO_ROOT}/deploy" \
  "${REPO_ROOT}/projects/baiwu_dukou" \
  "${REPO_ROOT}/src/xiaoshuo_bot" \
  "${REPO_ROOT}/pyproject.toml" \
  "${REPO_ROOT}/.gitignore"
do
  if [[ ! -e "${required_fixture}" ]]; then
    echo "ERROR: Required test fixture not found: ${required_fixture}" >&2
    exit 1
  fi
done

scp "${SCP_OPTS[@]}" \
  "${WHEEL_PATH}" \
  "${CPA_UNIT_PATH}" \
  "${BOT_UNIT_PATH}" \
  "${CADDYFILE_PATH}" \
  "${SSH_TARGET}:${REMOTE_STAGE}/"

tar --exclude='__pycache__' --exclude='*.pyc' \
  -C "$(dirname "${TESTS_DIR}")" -cf - "$(basename "${TESTS_DIR}")" \
  | ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "tar -C '${REMOTE_STAGE}' -xf -"

tar --exclude='__pycache__' --exclude='*.pyc' \
  -C "${REPO_ROOT}" -cf - deploy projects/baiwu_dukou pyproject.toml .gitignore src \
  | ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "tar -C '${REMOTE_STAGE}' -xf -"

echo "[3/7] Installing ${WHEEL_BASENAME} into isolated /opt/xiaoshuo-bot/.venv..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${REMOTE_STAGE}" "${WHEEL_BASENAME}" <<'REMOTE_INSTALL'
set -euo pipefail
stage_dir="$1"
wheel_name="$2"

if [[ ! -x /opt/xiaoshuo-bot/.venv/bin/python ]]; then
  python3 -m venv /opt/xiaoshuo-bot/.venv
fi

/opt/xiaoshuo-bot/.venv/bin/python -m pip install --upgrade pip
/opt/xiaoshuo-bot/.venv/bin/python -m pip install --upgrade --force-reinstall "${stage_dir}/${wheel_name}"
if [[ -f "${stage_dir}/requirements.txt" ]]; then
    /opt/xiaoshuo-bot/.venv/bin/python -m pip install -r "${stage_dir}/requirements.txt"
fi
/opt/xiaoshuo-bot/.venv/bin/xiaoshuo-bot --help >/dev/null

(
  cd /tmp
  env -u PYTHONPATH /opt/xiaoshuo-bot/.venv/bin/python -c '
from pathlib import Path
import xiaoshuo_bot
import xiaoshuo_bot.cli
import xiaoshuo_bot.cover
import xiaoshuo_bot.engine
import xiaoshuo_bot.llm.router
import xiaoshuo_bot.server.app
import xiaoshuo_bot.telegram_bot

pkg_dir = Path(xiaoshuo_bot.__file__).resolve().parent
assert "/opt/xiaoshuo-bot/.venv/" in str(pkg_dir), f"Expected wheel inside .venv, got {pkg_dir}"
for rel in ("fonts/OFL.txt", "server/static/index.html", "server/static/app.js", "server/static/styles.css"):
    assert (pkg_dir / rel).is_file(), f"Missing bundled package-data: {pkg_dir / rel}"
fonts_dir = pkg_dir / "fonts"
font_files = list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf"))
assert font_files, f"No bundled font files in {fonts_dir}"
'
)
REMOTE_INSTALL

echo "[4/7] Running cloud-compatible test suite inside isolated remote venv before switching services..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${REMOTE_STAGE}" <<'REMOTE_TESTS'
set -euo pipefail
stage_dir="$1"

# Explicit list of ALL cloud/core/deploy/static/TG/M1 unit & integration tests.
# Excluded ONLY local-PC [companion] tests that require fontTools/Playwright Chromium:
#   - tests/test_companion_and_font.py (T14 local Chrome + WOFF2 fontTools)
#   - tests/test_full_system_e2e.py (T15 local Playwright browser E2E)
cloud_tests=(
  tests/test_deploy_configs.py
  tests/test_web_static_contract.py
  tests/test_cover_exporter.py
  tests/test_evolution.py
  tests/test_planner_deconstruct.py
  tests/test_checker.py
  tests/test_tomato_checker.py
  tests/test_models_loader.py
  tests/test_genres_namer.py
  tests/test_world_state_v2.py
  tests/test_llm_router_cpa.py
  tests/test_compiler_polisher.py
  tests/test_pipeline.py
  tests/test_e2e_matrix_engine.py
  tests/test_server_stockpile.py
  tests/test_telegram_bot.py
  tests/test_audit_server_security.py
  tests/test_audit_engine_e1e2.py
)

for tf in "${cloud_tests[@]}"; do
  if [[ ! -f "${stage_dir}/${tf}" ]]; then
    echo "ERROR: Expected cloud test module missing from stage: ${stage_dir}/${tf}" >&2
    exit 1
  fi
done

(
  cd "${stage_dir}"
  env \
    -u XIAOSHUO_API_KEY \
    -u OPENAI_API_KEY \
    -u ANTHROPIC_API_KEY \
    -u PYTHONPATH \
    /opt/xiaoshuo-bot/.venv/bin/python -m unittest -v "${cloud_tests[@]}"
)
REMOTE_TESTS

echo "[5/7] Validating Caddyfile, installing systemd units & Caddyfile, and reloading systemd..."
CADDY_BASENAME="$(basename "${CADDYFILE_PATH}")"
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${REMOTE_STAGE}" "${CADDY_BASENAME}" "${RELEASE_ID}" <<'REMOTE_UNITS'
set -euo pipefail
stage_dir="$1"
caddy_name="$2"
rel_id="$3"

# Substitute the HTTPS site address from the env file (Caddy's systemd unit does not load it).
domain_line="$(grep -E '^[[:space:]]*XIAOSHUO_DOMAIN=' /etc/xiaoshuo-bot/xiaoshuo.env | tail -n 1 || echo '')"
xiaoshuo_domain="$(printf '%s' "${domain_line#*=}" | tr -d "[:space:]\"'")"
if grep -q '{\$XIAOSHUO_DOMAIN}' "${stage_dir}/${caddy_name}"; then
  if [[ ! "${xiaoshuo_domain}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "ERROR: XIAOSHUO_DOMAIN in /etc/xiaoshuo-bot/xiaoshuo.env must be a valid DNS name for Caddy auto-HTTPS." >&2
    exit 1
  fi
  sed -i "s/{\\\$XIAOSHUO_DOMAIN}/${xiaoshuo_domain}/g" "${stage_dir}/${caddy_name}"
fi

caddy validate --config "${stage_dir}/${caddy_name}" --adapter caddyfile

install -m 0644 -o root -g root "${stage_dir}/cpa.service" /etc/systemd/system/cpa.service
install -m 0644 -o root -g root "${stage_dir}/xiaoshuo-bot.service" /etc/systemd/system/xiaoshuo-bot.service

if [[ -f /etc/caddy/Caddyfile ]]; then
  cp /etc/caddy/Caddyfile "/opt/xiaoshuo-bot/_archive/Caddyfile.${rel_id}.bak"
fi
mkdir -p /etc/caddy
install -m 0644 -o root -g root "${stage_dir}/${caddy_name}" /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable cpa.service xiaoshuo-bot.service caddy.service
REMOTE_UNITS

echo "[6/7] Atomically restarting cpa, xiaoshuo-bot, and caddy..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s <<'REMOTE_RESTART'
set -euo pipefail
systemctl restart cpa.service
systemctl restart xiaoshuo-bot.service
systemctl restart caddy.service
REMOTE_RESTART

echo "[7/7] Verifying /api/health, systemd active status, 0600/0700 perms, and 127.0.0.1:8317 loopback lock..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s <<'REMOTE_VERIFY'
set -euo pipefail

systemctl is-active --quiet cpa.service
systemctl is-active --quiet xiaoshuo-bot.service
systemctl is-active --quiet caddy.service

health_ok=0
for attempt in $(seq 1 20); do
  if /opt/xiaoshuo-bot/.venv/bin/python -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=3) as resp:
    assert resp.status == 200
    data = json.loads(resp.read().decode("utf-8"))
    assert data.get("status") == "ok", data
'; then
    health_ok=1
    break
  fi
  sleep 1
done

if [[ "${health_ok}" -ne 1 ]]; then
  echo "ERROR: /api/health failed to report status=ok within 20 seconds." >&2
  systemctl status cpa.service xiaoshuo-bot.service --no-pager >&2
  exit 1
fi

listen_table="$(ss -tlnp)"
if ! grep -Eq '127\.0\.0\.1:8317\b' <<<"${listen_table}"; then
  echo "ERROR: Expected 127.0.0.1:8317 to be listening, got:" >&2
  echo "${listen_table}" >&2
  exit 1
fi
if grep -Eq '(0\.0\.0\.0|\*|\[::\]):8317\b' <<<"${listen_table}"; then
  echo "ERROR: SECURITY VIOLATION: Port 8317 is bound to a non-loopback address!" >&2
  echo "${listen_table}" >&2
  exit 1
fi

env_mode="$(stat -c '%a' /etc/xiaoshuo-bot/xiaoshuo.env)"
auth_mode="$(stat -c '%a' /opt/cli-proxy-api/auth-files)"
if [[ "${env_mode}" != "600" || "${auth_mode}" != "700" ]]; then
  echo "ERROR: Permission check failed (xiaoshuo.env=${env_mode}, auth-files=${auth_mode})." >&2
  exit 1
fi

echo "SUCCESS: Deployment verified. /api/health=ok, 8317 loopback-only, perms 0600/0700."
REMOTE_VERIFY
