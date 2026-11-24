#!/usr/bin/env bash
set -euo pipefail

# Safe Wheel Release Script for Korea Cloud Server (aligned with wheel-only release method).
# Strictly deploys xiaoshuo-bot wheel into /opt/xiaoshuo-bot/.venv ONLY:
#   - Creates pre-change backup under /root/backup-<timestamp>/ (data, /etc/xiaoshuo-bot, previous release)
#   - Installs wheel via: /opt/xiaoshuo-bot/.venv/bin/pip install --force-reinstall --no-deps <wheel>
#   - Restarts ONLY xiaoshuo-bot.service and verifies is-active + http://127.0.0.1:8080/api/health == 200
#   - Automatically rolls back to the previous release wheel on verification failure
#
# HARD PROHIBITIONS (enforced by tests/test_deploy_configs.py):
#   - NEVER touches, overwrites, enables, or restarts CPA service or /opt/cli-proxy-api/config.yaml
#   - NEVER installs or touches any reverse proxy or TLS port (leaves existing proxy/VPN untouched)
#
# Usage:
#   ./deploy/deploy_remote.sh --host <SSH_HOST_OR_ALIAS> --wheel <PATH_TO_WHL> [--key <SSH_KEY>] [options]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_HOST=""
REMOTE_USER="root"
SSH_PORT="22"
SSH_KEY=""
WHEEL_PATH=""
TESTS_DIR="${REPO_ROOT}/tests"
MIN_DISK_GB="5"

usage() {
  cat <<'EOF'
Usage: deploy_remote.sh --host <HOST_OR_ALIAS> --wheel <WHEEL_FILE> [options]

Required:
  --host <host>        Target server IP, hostname, or SSH config alias (no hardcoded default)
  --wheel <path>       Path to built xiaoshuo_bot-*.whl package (built with: pip wheel --no-deps)

Optional:
  --key <path>         Path to SSH private key (optional when using SSH config host alias)
  --user <name>        Remote SSH user (default: root)
  --port <num>         Remote SSH port (default: 22)
  --tests-dir <dir>    Local tests directory to run in remote venv (default: <repo>/tests)
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

if [[ -z "${REMOTE_HOST}" || -z "${WHEEL_PATH}" ]]; then
  echo "ERROR: --host and --wheel are strictly required." >&2
  usage >&2
  exit 1
fi

if [[ -n "${SSH_KEY}" && ! -f "${SSH_KEY}" ]]; then
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

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(
  -p "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)
SCP_OPTS=(
  -P "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)
if [[ -n "${SSH_KEY}" ]]; then
  SSH_OPTS+=(-i "${SSH_KEY}")
  SCP_OPTS+=(-i "${SSH_KEY}")
fi

RELEASE_ID="$(date +%Y%m%d-%H%M%S)"
REMOTE_STAGE="/opt/xiaoshuo-bot/_releases/${RELEASE_ID}"
BACKUP_DIR="/root/backup-${RELEASE_ID}"
WHEEL_BASENAME="$(basename "${WHEEL_PATH}")"

echo "[1/7] Running remote read-only pre-flight checks (disk >= ${MIN_DISK_GB}G, python3 >= 3.10, permissions 0600/0700)..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${MIN_DISK_GB}" <<'REMOTE_PREFLIGHT'
set -euo pipefail
min_gb="$1"

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
  echo "ERROR: System user xiaoshuo does not exist on target server." >&2
  exit 1
fi

if [[ ! -f /etc/xiaoshuo-bot/xiaoshuo.env ]]; then
  echo "ERROR: Missing /etc/xiaoshuo-bot/xiaoshuo.env; provision secrets with 0600 permissions before deploying." >&2
  exit 1
fi

if grep -q '__REPLACE_WITH_' /etc/xiaoshuo-bot/xiaoshuo.env; then
  echo "ERROR: /etc/xiaoshuo-bot/xiaoshuo.env still contains unreplaced __REPLACE_WITH_ placeholders." >&2
  exit 1
fi

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

env_mode="$(stat -c '%a' /etc/xiaoshuo-bot/xiaoshuo.env)"
if [[ "${env_mode}" != "600" ]]; then
  echo "ERROR: /etc/xiaoshuo-bot/xiaoshuo.env mode is ${env_mode}, expected 0600." >&2
  exit 1
fi

# Read-only CPA safety checks (NEVER modify CPA binary, config, or service unit here)
if [[ ! -x /opt/cli-proxy-api/cli-proxy-api ]]; then
  echo "ERROR: Missing or non-executable CPA binary at /opt/cli-proxy-api/cli-proxy-api." >&2
  exit 1
fi

if [[ ! -f /opt/cli-proxy-api/config.yaml ]]; then
  echo "ERROR: Missing /opt/cli-proxy-api/config.yaml." >&2
  exit 1
fi

if grep -q '__REPLACE_WITH_' /opt/cli-proxy-api/config.yaml; then
  echo "ERROR: /opt/cli-proxy-api/config.yaml still contains unreplaced __REPLACE_WITH_ placeholders." >&2
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

auth_mode="$(stat -c '%a' /opt/cli-proxy-api/auth-files)"
if [[ "${auth_mode}" != "700" ]]; then
  echo "ERROR: /opt/cli-proxy-api/auth-files mode is ${auth_mode}, expected 0700." >&2
  exit 1
fi

if [[ ! -x /opt/xiaoshuo-bot/.venv/bin/pip || ! -x /opt/xiaoshuo-bot/.venv/bin/python ]]; then
  echo "ERROR: Missing isolated venv at /opt/xiaoshuo-bot/.venv." >&2
  exit 1
fi
REMOTE_PREFLIGHT

echo "[2/7] Creating mandatory pre-change server backup at ${BACKUP_DIR}..."
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${BACKUP_DIR}" "${REMOTE_STAGE}" <<'REMOTE_BACKUP'
set -euo pipefail
backup_dir="$1"
stage_dir="$2"

mkdir -p "${backup_dir}"
cp -a /opt/xiaoshuo-bot/data "${backup_dir}/data"
cp -a /etc/xiaoshuo-bot "${backup_dir}/etc-xiaoshuo-bot"

prev_rel=""
prev_whl=""
if [[ -d /opt/xiaoshuo-bot/_releases ]]; then
  prev_rel="$(ls -1 /opt/xiaoshuo-bot/_releases | sort | tail -n 1 || echo '')"
  if [[ -n "${prev_rel}" ]]; then
    prev_whl="$(ls -1 /opt/xiaoshuo-bot/_releases/"${prev_rel}"/xiaoshuo_bot-*.whl | head -n 1 || echo '')"
  fi
fi
printf '%s\n' "${prev_rel}" > "${backup_dir}/previous_release.txt"
printf '%s\n' "${prev_whl}" > "${backup_dir}/previous_wheel.txt"

mkdir -p "${stage_dir}"
REMOTE_BACKUP

echo "[3/7] Uploading wheel ${WHEEL_BASENAME}, tests, and read-only test fixtures to ${REMOTE_STAGE}..."
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

scp "${SCP_OPTS[@]}" "${WHEEL_PATH}" "${SSH_TARGET}:${REMOTE_STAGE}/"

tar --exclude='__pycache__' --exclude='*.pyc' \
  -C "$(dirname "${TESTS_DIR}")" -cf - "$(basename "${TESTS_DIR}")" \
  | ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "tar -C '${REMOTE_STAGE}' -xf -"

tar --exclude='__pycache__' --exclude='*.pyc' \
  -C "${REPO_ROOT}" -cf - deploy projects/baiwu_dukou pyproject.toml .gitignore src \
  | ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "tar -C '${REMOTE_STAGE}' -xf -"

rollback_previous_wheel() {
  echo "ERROR: Deployment failed; attempting automatic rollback to previous release wheel..." >&2
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${BACKUP_DIR}" <<'REMOTE_ROLLBACK'
set -euo pipefail
backup_dir="$1"
prev_whl="$(cat "${backup_dir}/previous_wheel.txt")"
if [[ -z "${prev_whl}" || ! -f "${prev_whl}" ]]; then
  echo "ERROR: No previous wheel found in ${backup_dir}/previous_wheel.txt for rollback." >&2
  exit 1
fi
/opt/xiaoshuo-bot/.venv/bin/pip install --force-reinstall --no-deps "${prev_whl}"
systemctl restart xiaoshuo-bot.service
systemctl is-active --quiet xiaoshuo-bot.service
echo "ROLLBACK_OK: restored ${prev_whl}" >&2
REMOTE_ROLLBACK
}

echo "[4/7] Installing ${WHEEL_BASENAME} into /opt/xiaoshuo-bot/.venv (--force-reinstall --no-deps)..."
if ! ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${REMOTE_STAGE}" "${WHEEL_BASENAME}" <<'REMOTE_INSTALL'
set -euo pipefail
stage_dir="$1"
wheel_name="$2"

/opt/xiaoshuo-bot/.venv/bin/pip install --force-reinstall --no-deps "${stage_dir}/${wheel_name}"
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
then
  rollback_previous_wheel
  exit 1
fi

echo "[5/7] Running cloud-compatible test suite inside isolated remote venv before restarting xiaoshuo-bot..."
if ! ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- "${REMOTE_STAGE}" <<'REMOTE_TESTS'
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
  tests/test_blueprint.py
  tests/test_planner_deconstruct.py
  tests/test_checker.py
  tests/test_tomato_checker.py
  tests/test_t5a_rules_and_cooldown.py
  tests/test_models_loader.py
  tests/test_genres_namer.py
  tests/test_world_state_v2.py
  tests/test_t6_memory_and_state.py
  tests/test_llm_router_cpa.py
  tests/test_compiler_polisher.py
  tests/test_pipeline.py
  tests/test_e2e_matrix_engine.py
  tests/test_server_stockpile.py
  tests/test_telegram_bot.py
  tests/test_audit_server_security.py
  tests/test_audit_engine_e1e2.py
  tests/test_public_and_wizard.py
  tests/test_export_only_scheduler.py
  tests/test_length_target.py
  tests/test_quality_rules.py
  tests/test_judge.py
  tests/test_opening_rules.py
  tests/test_staged_planner.py
  tests/test_workbuddy_routing.py
  tests/test_workbuddy_config_and_tracking.py
  tests/test_workbuddy_deploy.py
  tests/test_factory_v2_upgrades.py
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
then
  rollback_previous_wheel
  exit 1
fi

echo "[6/7] Restarting ONLY xiaoshuo-bot.service (leaving CPA, proxy, and VPN untouched)..."
if ! ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s <<'REMOTE_RESTART'
set -euo pipefail
systemctl restart xiaoshuo-bot.service
REMOTE_RESTART
then
  rollback_previous_wheel
  exit 1
fi

echo "[7/7] Verifying xiaoshuo-bot active status, /api/health HTTP 200, loopback 127.0.0.1:8317, and 0600/0700 perms..."
if ! ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s <<'REMOTE_VERIFY'
set -euo pipefail

systemctl is-active --quiet xiaoshuo-bot.service

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
  systemctl status xiaoshuo-bot.service --no-pager >&2
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
REMOTE_VERIFY
then
  rollback_previous_wheel
  exit 1
fi

echo "[OK] Wheel release ${RELEASE_ID} deployed cleanly to ${REMOTE_HOST} (backup: ${BACKUP_DIR})."
