#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# CLIProxyAPI (CPA v8.0.69) + cpa-workbuddy Source-Build Server Installer
#
# Owner invariants enforced by this script (refuse & abort on any mismatch):
#   1. CPA binary pinned strictly to v8.0.69, verified against BOTH official
#      GitHub release checksums.txt AND pinned SHA256 digests in this script.
#   2. cpa-workbuddy plugin built strictly FROM SOURCE via git clone +
#      git checkout 2a1a608 + exact full-SHA check against
#      2a1a608fb0ff28c51996393822a7f36df4367d5b (NEVER downloads prebuilt zips).
#   3. CPA listens strictly on 127.0.0.1:8317 with remote-management.allow-remote: false.
#   4. Management secret-key & local gateway api-key are generated on-server via
#      openssl rand -hex 32 (NEVER stored in git).
#   5. auto_checkin_enabled is forced to false (owner claims credits on Mac only).
#
# Run on target Linux server as root:
#   sudo bash deploy/install_cpa_workbuddy.sh
# ==============================================================================

readonly CPA_VERSION="8.0.69"
readonly CPA_TAG="v${CPA_VERSION}"
readonly CPA_RELEASE_BASE="https://github.com/router-for-me/CLIProxyAPI/releases/download/${CPA_TAG}"

# Official SHA256 digests from CLIProxyAPI v8.0.69 checksums.txt (plugin-enabled builds ONLY):
readonly CPA_SHA256_LINUX_AMD64="4213723d57096bc97ad32d810b89653ba3edeb384087cecc90956c4c33e8af22"
readonly CPA_SHA256_LINUX_AARCH64="19f13e62643ea9f6275553622ad13f945e551397166f099a78c32a2e0f85d4c3"

readonly PLUGIN_REPO_URL="https://github.com/ygq-future/cpa-workbuddy.git"
readonly PLUGIN_SHORT_COMMIT="2a1a608"
readonly PLUGIN_EXPECTED_COMMIT="2a1a608fb0ff28c51996393822a7f36df4367d5b"
readonly PLUGIN_REQUIRED_GO_VERSION="1.27.1"

readonly INSTALL_DIR="/opt/cli-proxy-api"
readonly AUTH_DIR="${INSTALL_DIR}/auth-files"
readonly LOG_DIR="/var/log/cli-proxy-api"
readonly CONFIG_PATH="${INSTALL_DIR}/config.yaml"
readonly SERVICE_USER="xiaoshuo"
readonly SERVICE_GROUP="xiaoshuo"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_CONFIG="${SCRIPT_DIR}/cpa.workbuddy.config.example.yaml"
UNIT_TEMPLATE="${SCRIPT_DIR}/cpa-workbuddy.service"

ROTATE_KEYS="false"
for arg in "$@"; do
  case "${arg}" in
    --rotate-keys)
      ROTATE_KEYS="true"
      ;;
    *)
      echo "ERROR: Unknown argument: ${arg}" >&2
      echo "Usage: sudo bash $0 [--rotate-keys]" >&2
      exit 1
      ;;
  esac
done

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null; then
    echo "ERROR: Required command not found in PATH: ${cmd}" >&2
    exit 1
  fi
}

version_gte() {
  local have="$1"
  local want="$2"
  local lowest
  lowest="$(printf '%s\n%s\n' "${have}" "${want}" | sort -V | head -n 1)"
  [[ "${lowest}" == "${want}" ]]
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: This installer must run as root (use sudo)." >&2
  exit 1
fi

for cmd in curl tar sha256sum git go gcc openssl install systemctl grep sed awk; do
  require_cmd "${cmd}"
done

if [[ ! -f "${TEMPLATE_CONFIG}" ]]; then
  echo "ERROR: Missing config template: ${TEMPLATE_CONFIG}" >&2
  exit 1
fi
if [[ ! -f "${UNIT_TEMPLATE}" ]]; then
  echo "ERROR: Missing systemd unit template: ${UNIT_TEMPLATE}" >&2
  exit 1
fi

# Verify Go toolchain version satisfies cpa-workbuddy go.mod (go 1.27.1)
raw_go_version="$(go env GOVERSION)"
clean_go_version="${raw_go_version#go}"
if ! version_gte "${clean_go_version}" "${PLUGIN_REQUIRED_GO_VERSION}"; then
  echo "ERROR: cpa-workbuddy requires Go >= ${PLUGIN_REQUIRED_GO_VERSION}; found ${raw_go_version}." >&2
  echo "Install Go ${PLUGIN_REQUIRED_GO_VERSION}+ first, then re-run this script." >&2
  exit 1
fi

# Detect Linux CPU architecture
uname_arch="$(uname -m)"
case "${uname_arch}" in
  x86_64|amd64)
    cpa_asset="CLIProxyAPI_${CPA_VERSION}_linux_amd64.tar.gz"
    expected_cpa_sha256="${CPA_SHA256_LINUX_AMD64}"
    go_arch="amd64"
    ;;
  aarch64|arm64)
    cpa_asset="CLIProxyAPI_${CPA_VERSION}_linux_aarch64.tar.gz"
    expected_cpa_sha256="${CPA_SHA256_LINUX_AARCH64}"
    go_arch="arm64"
    ;;
  *)
    echo "ERROR: Unsupported Linux architecture: ${uname_arch}" >&2
    exit 1
    ;;
esac

# Create service user/group if absent
if ! getent group "${SERVICE_GROUP}" >/dev/null; then
  groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null; then
  useradd --system --gid "${SERVICE_GROUP}" --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

WORK_DIR="$(mktemp -d /tmp/cpa-workbuddy-install.XXXXXX)"
cleanup() {
  if [[ -n "${WORK_DIR:-}" && -d "${WORK_DIR}" && "${WORK_DIR}" == /tmp/cpa-workbuddy-install.* ]]; then
    find "${WORK_DIR}" -depth -delete
  fi
}
trap cleanup EXIT

echo "[1/6] Downloading CLIProxyAPI ${CPA_TAG} (${cpa_asset}) and official checksums.txt..."
curl -fsSL --proto '=https' --tlsv1.2 -o "${WORK_DIR}/${cpa_asset}" "${CPA_RELEASE_BASE}/${cpa_asset}"
curl -fsSL --proto '=https' --tlsv1.2 -o "${WORK_DIR}/checksums.txt" "${CPA_RELEASE_BASE}/checksums.txt"

echo "[2/6] Verifying CLIProxyAPI ${CPA_TAG} SHA256 checksum (official checksums.txt + pinned digest)..."
official_line=""
if ! official_line="$(grep -E "  ${cpa_asset}\$" "${WORK_DIR}/checksums.txt")"; then
  echo "ERROR: Official checksums.txt does not list ${cpa_asset}; aborting." >&2
  exit 1
fi
if [[ -z "${official_line}" ]]; then
  echo "ERROR: Official checksums.txt returned empty line for ${cpa_asset}; aborting." >&2
  exit 1
fi
official_sha256="$(awk '{print $1}' <<< "${official_line}")"
actual_sha256="$(sha256sum "${WORK_DIR}/${cpa_asset}" | awk '{print $1}')"

if [[ "${official_sha256}" != "${expected_cpa_sha256}" ]]; then
  echo "ERROR: Official checksums.txt digest (${official_sha256}) disagrees with pinned v${CPA_VERSION} digest (${expected_cpa_sha256}); aborting." >&2
  exit 1
fi
if [[ "${actual_sha256}" != "${expected_cpa_sha256}" ]]; then
  echo "ERROR: Downloaded ${cpa_asset} SHA256 mismatch! expected=${expected_cpa_sha256} actual=${actual_sha256}; aborting." >&2
  exit 1
fi
(cd "${WORK_DIR}" && printf '%s  %s\n' "${expected_cpa_sha256}" "${cpa_asset}" | sha256sum -c -)

echo "[3/6] Extracting CLIProxyAPI ${CPA_TAG} binary..."
mkdir -p "${WORK_DIR}/cpa-extract"
tar -xzf "${WORK_DIR}/${cpa_asset}" -C "${WORK_DIR}/cpa-extract"
cpa_bin="$(find "${WORK_DIR}/cpa-extract" -maxdepth 2 -type f \( -name 'cli-proxy-api' -o -name 'CLIProxyAPI' \) | head -n 1)"
if [[ -z "${cpa_bin}" || ! -f "${cpa_bin}" ]]; then
  echo "ERROR: Could not locate cli-proxy-api binary inside ${cpa_asset}; aborting." >&2
  exit 1
fi

echo "[4/6] Cloning and building cpa-workbuddy strictly from source at commit ${PLUGIN_SHORT_COMMIT}..."
plugin_src_dir="${WORK_DIR}/cpa-workbuddy-src"
git clone "${PLUGIN_REPO_URL}" "${plugin_src_dir}"
git -C "${plugin_src_dir}" checkout "${PLUGIN_SHORT_COMMIT}"

actual_plugin_commit="$(git -C "${plugin_src_dir}" rev-parse HEAD)"
if [[ "${actual_plugin_commit}" != "${PLUGIN_EXPECTED_COMMIT}" ]]; then
  echo "ERROR: cpa-workbuddy commit SHA mismatch! expected=${PLUGIN_EXPECTED_COMMIT} actual=${actual_plugin_commit}; aborting." >&2
  exit 1
fi
echo "Verified cpa-workbuddy HEAD == ${actual_plugin_commit}"

plugin_build_out="${WORK_DIR}/cpa-workbuddy.so"
(
  cd "${plugin_src_dir}"
  CGO_ENABLED=1 GOOS=linux GOARCH="${go_arch}" \
    go build -trimpath -buildmode=c-shared -o "${plugin_build_out}" ./cmd/cpa-workbuddy
)
if [[ ! -s "${plugin_build_out}" ]]; then
  echo "ERROR: c-shared plugin build produced missing or empty artifact: ${plugin_build_out}" >&2
  exit 1
fi
if ! grep -q "cliproxy_plugin_init" "${WORK_DIR}/cpa-workbuddy.h"; then
  echo "ERROR: Generated C header missing required CPA ABI symbol cliproxy_plugin_init; aborting." >&2
  exit 1
fi

echo "[5/6] Installing binaries, plugin, directories, and on-server generated secrets..."
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${INSTALL_DIR}"
install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${AUTH_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${INSTALL_DIR}/plugins/linux/${go_arch}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"

install -m 0755 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${cpa_bin}" "${INSTALL_DIR}/cli-proxy-api"
install -m 0755 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${plugin_build_out}" "${INSTALL_DIR}/plugins/linux/${go_arch}/cpa-workbuddy.so"

if [[ ! -f "${CONFIG_PATH}" || "${ROTATE_KEYS}" == "true" ]]; then
  mgmt_key="$(openssl rand -hex 32)"
  gateway_key="$(openssl rand -hex 32)"
  rendered_cfg="${WORK_DIR}/config.yaml"
  sed \
    -e "s|__REPLACE_WITH_64_HEX_CPA_MANAGEMENT_SECRET_KEY__|${mgmt_key}|g" \
    -e "s|__REPLACE_WITH_64_HEX_CPA_LOCAL_GATEWAY_KEY__|${gateway_key}|g" \
    "${TEMPLATE_CONFIG}" > "${rendered_cfg}"

  if grep -q "__REPLACE_WITH_" "${rendered_cfg}"; then
    echo "ERROR: Unresolved placeholder remains in rendered ${rendered_cfg}; aborting." >&2
    exit 1
  fi
  if ! grep -Eq '^[[:space:]]*host:[[:space:]]*"127\.0\.0\.1"[[:space:]]*$' "${rendered_cfg}"; then
    echo "ERROR: Rendered config.yaml is not bound to 127.0.0.1; aborting." >&2
    exit 1
  fi
  if ! grep -Eq '^[[:space:]]*allow-remote:[[:space:]]*false[[:space:]]*$' "${rendered_cfg}"; then
    echo "ERROR: Rendered config.yaml does not set allow-remote: false; aborting." >&2
    exit 1
  fi
  if ! grep -Eq '^[[:space:]]*auto_checkin_enabled:[[:space:]]*false[[:space:]]*$' "${rendered_cfg}"; then
    echo "ERROR: Rendered config.yaml does not enforce auto_checkin_enabled: false; aborting." >&2
    exit 1
  fi
  if grep -Eq 'auto_checkin_enabled:[[:space:]]*true' "${rendered_cfg}"; then
    echo "ERROR: Rendered config.yaml must disable auto_checkin_enabled; aborting." >&2
    exit 1
  fi

  install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${rendered_cfg}" "${CONFIG_PATH}"
  echo "Generated fresh loopback-only ${CONFIG_PATH} (mode 0600) with on-server random keys."
else
  echo "Preserving existing ${CONFIG_PATH} (pass --rotate-keys to regenerate keys)."
  chmod 0600 "${CONFIG_PATH}"
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${CONFIG_PATH}"
fi

echo "[6/6] Installing hardened systemd unit and starting cpa.service..."
install -m 0644 -o root -g root "${UNIT_TEMPLATE}" /etc/systemd/system/cpa.service
systemctl daemon-reload
systemctl enable cpa.service
systemctl restart cpa.service
systemctl is-active --quiet cpa.service

echo "DONE: CLIProxyAPI ${CPA_TAG} + cpa-workbuddy (${actual_plugin_commit}) installed on 127.0.0.1:8317."
echo "Next step: Follow deploy/WORKBUDDY_LOGIN_GUIDE.md to open SSH tunnel on Mac and complete WorkBuddy OAuth login."
