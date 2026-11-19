from __future__ import annotations

import configparser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"

INSTALL_SCRIPT = DEPLOY_DIR / "install_cpa_workbuddy.sh"
WB_CONFIG_TEMPLATE = DEPLOY_DIR / "cpa.workbuddy.config.example.yaml"
WB_SERVICE = DEPLOY_DIR / "cpa-workbuddy.service"
WB_LOGIN_GUIDE = DEPLOY_DIR / "WORKBUDDY_LOGIN_GUIDE.md"
ENV_EXAMPLE = DEPLOY_DIR / "env.example"

EXPECTED_CPA_VERSION = "8.0.69"
EXPECTED_CPA_SHA256_AMD64 = "4213723d57096bc97ad32d810b89653ba3edeb384087cecc90956c4c33e8af22"
EXPECTED_CPA_SHA256_ARM64 = "19f13e62643ea9f6275553622ad13f945e551397166f099a78c32a2e0f85d4c3"
EXPECTED_PLUGIN_SHORT_COMMIT = "2a1a608"
EXPECTED_PLUGIN_FULL_COMMIT = "2a1a608fb0ff28c51996393822a7f36df4367d5b"

_SECRET_LEAK_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b[0-9a-f]{64}\b"),
)


def _strip_expected_digests(text: str) -> str:
    """Remove the two pinned official CPA tarball SHA256 digests before scanning for leaked 64-hex secrets."""
    cleaned = text.replace(EXPECTED_CPA_SHA256_AMD64, "<PINNED_CPA_AMD64_SHA256>")
    cleaned = cleaned.replace(EXPECTED_CPA_SHA256_ARM64, "<PINNED_CPA_ARM64_SHA256>")
    return cleaned


def validate_install_script_text(script_text: str) -> list[str]:
    """Validate all owner security invariants on deploy/install_cpa_workbuddy.sh. Returns list of violations."""
    errors: list[str] = []
    lines = script_text.splitlines()
    if not lines or lines[0] != "#!/usr/bin/env bash":
        errors.append("Missing #!/usr/bin/env bash shebang")
    if "set -euo pipefail" not in lines[:5]:
        errors.append("Missing set -euo pipefail in script header")

    # Forbidden shell masking / destructive commands
    for forbidden in ("|| true", "2>/dev/null", "2> /dev/null", "rm -rf", "rm -fr"):
        if forbidden in script_text:
            errors.append(f"Forbidden shell pattern found: {forbidden!r}")

    # 1. CPA pinned strictly to v8.0.69 + official checksum verification
    if not re.search(rf'readonly\s+CPA_VERSION="{re.escape(EXPECTED_CPA_VERSION)}"', script_text):
        errors.append(f"CPA_VERSION is not pinned to {EXPECTED_CPA_VERSION!r}")
    if "checksums.txt" not in script_text or "sha256sum" not in script_text:
        errors.append("Missing official checksums.txt / sha256sum verification")
    if EXPECTED_CPA_SHA256_AMD64 not in script_text:
        errors.append("Missing pinned official CPA v8.0.69 linux_amd64 SHA256 digest")
    if EXPECTED_CPA_SHA256_ARM64 not in script_text:
        errors.append("Missing pinned official CPA v8.0.69 linux_aarch64 SHA256 digest")
    if "_no-plugin" in script_text:
        errors.append("Install script must not reference _no-plugin CPA archive")

    # 2. cpa-workbuddy compiled strictly from source at 2a1a608 -> 2a1a608fb0ff28c51996393822a7f36df4367d5b
    if "https://github.com/ygq-future/cpa-workbuddy" not in script_text:
        errors.append("Missing official cpa-workbuddy repository URL")
    if not re.search(rf'readonly\s+PLUGIN_SHORT_COMMIT="{EXPECTED_PLUGIN_SHORT_COMMIT}"', script_text):
        errors.append(f"PLUGIN_SHORT_COMMIT must be pinned to {EXPECTED_PLUGIN_SHORT_COMMIT!r}")
    if not re.search(rf'readonly\s+PLUGIN_EXPECTED_COMMIT="{EXPECTED_PLUGIN_FULL_COMMIT}"', script_text):
        errors.append(f"PLUGIN_EXPECTED_COMMIT must be pinned to {EXPECTED_PLUGIN_FULL_COMMIT!r}")
    if "git clone" not in script_text or "git -C" not in script_text or "checkout" not in script_text:
        errors.append("Plugin must be installed via git clone + git checkout")
    if "rev-parse HEAD" not in script_text:
        errors.append("Missing exact git rev-parse HEAD full-commit verification")
    if "go build" not in script_text or "-buildmode=c-shared" not in script_text:
        errors.append("Missing Go c-shared source compilation command")
    if re.search(r"cpa-workbuddy[^\n]*\.zip|cpa-workbuddy/releases/download", script_text, re.IGNORECASE):
        errors.append("Forbidden prebuilt cpa-workbuddy zip download detected")

    # 3. Loopback-only & runtime secret generation (never hardcoded)
    if '127\\.0\\.0\\.1' not in script_text and "127.0.0.1" not in script_text:
        errors.append("Install script must verify loopback 127.0.0.1 binding")
    if "openssl rand -hex 32" not in script_text:
        errors.append("Install script must generate CPA keys on-server via openssl rand -hex 32")
    if "allow-remote" not in script_text:
        errors.append("Install script must verify allow-remote: false")

    # 4. Auto check-in strictly disabled
    if "auto_checkin_enabled" not in script_text:
        errors.append("Install script must verify auto_checkin_enabled: false")
    if re.search(r'^\s*auto_checkin_enabled:\s*true\b', script_text, re.MULTILINE):
        errors.append("Install script must not set auto_checkin_enabled: true")

    # 5. No leaked secrets
    scrubbed = _strip_expected_digests(script_text)
    for pat in _SECRET_LEAK_PATTERNS:
        if pat.search(scrubbed):
            errors.append(f"Potential secret literal matched {pat.pattern!r} in install script")

    return errors


def validate_workbuddy_config_yaml(yaml_text: str) -> list[str]:
    """Validate all owner security invariants on deploy/cpa.workbuddy.config.example.yaml."""
    errors: list[str] = []
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        return ["Config template did not parse as a YAML mapping"]

    # Loopback-only
    if data.get("host") != "127.0.0.1":
        errors.append(f"CPA host must be '127.0.0.1', got {data.get('host')!r}")
    if data.get("port") != 8317:
        errors.append(f"CPA port must be 8317, got {data.get('port')!r}")

    # Anti-silent-downgrade
    quota = data.get("quota-exceeded") or {}
    if quota.get("switch-preview-model") is not False:
        errors.append("quota-exceeded.switch-preview-model must be false")
    if quota.get("antigravity-credits") is not False:
        errors.append("quota-exceeded.antigravity-credits must be false")

    # Remote management loopback + placeholder secret only
    rm = data.get("remote-management") or {}
    if rm.get("allow-remote") is not False:
        errors.append("remote-management.allow-remote must be false")
    secret_key = str(rm.get("secret-key") or "")
    if "__REPLACE_WITH_" not in secret_key:
        errors.append("remote-management.secret-key must use __REPLACE_WITH_ placeholder (no hardcoded secret)")

    api_keys = data.get("api-keys") or []
    if not isinstance(api_keys, list) or not api_keys:
        errors.append("api-keys must be a non-empty list of placeholders")
    else:
        for k in api_keys:
            if "__REPLACE_WITH_" not in str(k):
                errors.append(f"api-keys entry must use __REPLACE_WITH_ placeholder, got non-placeholder")

    # Plugin enabled & auto_checkin_enabled STRICTLY false
    plugins = data.get("plugins") or {}
    if plugins.get("enabled") is not True:
        errors.append("plugins.enabled must be true")
    wb_cfg = (plugins.get("configs") or {}).get("cpa-workbuddy") or {}
    if wb_cfg.get("enabled") is not True:
        errors.append("plugins.configs.cpa-workbuddy.enabled must be true")
    if wb_cfg.get("auto_checkin_enabled") is not False:
        errors.append(
            f"plugins.configs.cpa-workbuddy.auto_checkin_enabled MUST be false, got {wb_cfg.get('auto_checkin_enabled')!r}"
        )

    for pat in _SECRET_LEAK_PATTERNS:
        if pat.search(yaml_text):
            errors.append(f"Potential secret literal matched {pat.pattern!r} in config template")

    return errors


class TestWorkBuddyDeployScripts(unittest.TestCase):
    def test_install_script_syntax_and_all_security_invariants(self) -> None:
        raw = INSTALL_SCRIPT.read_bytes()
        self.assertNotIn(b"\r\n", raw, "install_cpa_workbuddy.sh must use LF line endings")
        text = raw.decode("utf-8")
        self.assertEqual(validate_install_script_text(text), [])

        bash_bin = shutil.which("bash")
        self.assertIsNotNone(bash_bin, "bash is required for syntax validation")
        assert bash_bin is not None
        res = subprocess.run(
            [bash_bin, "-n", str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"bash -n failed: {res.stderr}")

    def test_workbuddy_config_template_and_systemd_unit_and_login_guide(self) -> None:
        yaml_text = WB_CONFIG_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(validate_workbuddy_config_yaml(yaml_text), [])

        # Systemd unit checks
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(WB_SERVICE.read_text(encoding="utf-8"))
        self.assertTrue(parser.has_section("Service"))
        svc = parser["Service"]
        self.assertEqual(svc.get("User"), "xiaoshuo")
        self.assertEqual(svc.get("NoNewPrivileges"), "true")
        self.assertEqual(svc.get("ProtectSystem"), "strict")
        self.assertEqual(svc.get("PrivateTmp"), "true")
        self.assertEqual(svc.get("Restart"), "on-failure")
        exec_start = svc.get("ExecStart") or ""
        self.assertIn("/opt/cli-proxy-api/cli-proxy-api", exec_start)
        self.assertIn("/opt/cli-proxy-api/config.yaml", exec_start)
        for bad_flag in ("--api-key", "--secret", "--token", "--password"):
            self.assertNotIn(bad_flag, exec_start)

        # env.example includes all 4 WorkBuddy whitelist placeholders (zero hardcoded guessed IDs)
        env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for env_key in (
            "WORKBUDDY_MODEL_HY4_FLASH",
            "WORKBUDDY_MODEL_HY4",
            "WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH",
            "WORKBUDDY_MODEL_MIMO_V2_6_PRO",
        ):
            self.assertRegex(
                env_text,
                re.compile(rf"^{env_key}=__REPLACE_WITH_", re.MULTILINE),
                f"Missing placeholder for {env_key}",
            )

        # Chinese login guide for husband checks
        guide_text = WB_LOGIN_GUIDE.read_text(encoding="utf-8")
        self.assertIn("ssh -N -L 8317:127.0.0.1:8317", guide_text)
        self.assertIn("v8.0.69", guide_text)
        self.assertIn(EXPECTED_PLUGIN_FULL_COMMIT, guide_text)
        self.assertIn("auto_checkin_enabled", guide_text)
        self.assertIn("WorkBuddy国内", guide_text)
        self.assertIn("WorkBuddy国际", guide_text)
        self.assertIn("/v1/models", guide_text)
        for pat in _SECRET_LEAK_PATTERNS:
            self.assertIsNone(pat.search(guide_text))

    def test_zero_auto_checkin_enabled_anywhere_in_code_or_deploy(self) -> None:
        # Ensure neither src/ nor deploy/ enables auto_checkin anywhere
        for root in (REPO_ROOT / "src", DEPLOY_DIR):
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                # Ignore the explicit negative grep guard inside install_cpa_workbuddy.sh
                filtered_lines = [
                    ln for ln in text.splitlines() if "grep" not in ln
                ]
                filtered = "\n".join(filtered_lines)
                self.assertIsNone(
                    re.search(r"auto_checkin_enabled\s*[:=]\s*true\b", filtered, re.IGNORECASE),
                    f"auto_checkin_enabled must NEVER be true in {path.relative_to(REPO_ROOT)}",
                )
                self.assertNotIn(
                    "/v0/management/plugins/cpa-workbuddy/checkin",
                    filtered,
                    f"Code/deploy must never invoke automatic check-in endpoint ({path.relative_to(REPO_ROOT)})",
                )
                self.assertNotIn(
                    "/v0/management/plugins/cpa-workbuddy/daily-bonus",
                    filtered,
                    f"Code/deploy must never invoke automatic daily-bonus endpoint ({path.relative_to(REPO_ROOT)})",
                )

    def test_tamper_mutations_fail_validation(self) -> None:
        """Prove that breaking ANY single security invariant causes validation to fail."""
        good_script = INSTALL_SCRIPT.read_text(encoding="utf-8")
        good_yaml = WB_CONFIG_TEMPLATE.read_text(encoding="utf-8")

        # 1. Tamper CPA version
        bad_ver = good_script.replace('CPA_VERSION="8.0.69"', 'CPA_VERSION="8.0.70"')
        self.assertTrue(validate_install_script_text(bad_ver))

        # 2. Tamper plugin full commit hash by 1 char
        tampered_sha = EXPECTED_PLUGIN_FULL_COMMIT[:-1] + "a"
        bad_commit = good_script.replace(EXPECTED_PLUGIN_FULL_COMMIT, tampered_sha)
        self.assertTrue(validate_install_script_text(bad_commit))

        # 3. Tamper plugin source build into prebuilt zip download
        bad_zip = good_script + "\ncurl -fsSL https://github.com/ygq-future/cpa-workbuddy/releases/download/v0.1.2/cpa-workbuddy_0.1.2_linux_amd64.zip\n"
        self.assertTrue(validate_install_script_text(bad_zip))

        # 4. Tamper loopback host to 0.0.0.0
        bad_host = good_yaml.replace('host: "127.0.0.1"', 'host: "0.0.0.0"')
        self.assertTrue(validate_workbuddy_config_yaml(bad_host))

        # 5. Tamper allow-remote to true
        bad_remote = good_yaml.replace("allow-remote: false", "allow-remote: true")
        self.assertTrue(validate_workbuddy_config_yaml(bad_remote))

        # 6. Tamper auto_checkin_enabled from false to true
        bad_checkin = good_yaml.replace("auto_checkin_enabled: false", "auto_checkin_enabled: true")
        self.assertTrue(validate_workbuddy_config_yaml(bad_checkin))

        # 7. Tamper hardcoded secret into config template
        fake_64_hex = "a1b2c3d4" * 8
        bad_secret = good_yaml.replace("__REPLACE_WITH_64_HEX_CPA_MANAGEMENT_SECRET_KEY__", fake_64_hex)
        self.assertTrue(validate_workbuddy_config_yaml(bad_secret))


if __name__ == "__main__":
    unittest.main()
