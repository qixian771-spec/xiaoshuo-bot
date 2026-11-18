from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"

CPA_CONFIG = DEPLOY_DIR / "cpa.config.example.yaml"
CPA_SERVICE = DEPLOY_DIR / "cpa.service"
BOT_SERVICE = DEPLOY_DIR / "xiaoshuo-bot.service"
CADDYFILE = DEPLOY_DIR / "Caddyfile.example"
ENV_EXAMPLE = DEPLOY_DIR / "env.example"
DEPLOY_SCRIPT = DEPLOY_DIR / "deploy_remote.sh"
TUNNEL_SCRIPT = DEPLOY_DIR / "ssh_cpa_tunnel.ps1"

ALL_DEPLOY_FILES = (
    CPA_CONFIG,
    CPA_SERVICE,
    BOT_SERVICE,
    CADDYFILE,
    ENV_EXAMPLE,
    DEPLOY_SCRIPT,
    TUNNEL_SCRIPT,
)

_REAL_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
)
_CONCRETE_43_IP_RE = re.compile(r"\b43\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _find_working_bash() -> str | None:
    candidates: list[str] = []
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.is_file():
            candidates.append(str(git_bash))
        git_bash_x86 = Path(r"C:\Program Files (x86)\Git\bin\bash.exe")
        if git_bash_x86.is_file():
            candidates.append(str(git_bash_x86))
    which_bash = shutil.which("bash")
    if which_bash and which_bash not in candidates:
        candidates.append(which_bash)

    for candidate in candidates:
        try:
            res = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and "GNU bash" in res.stdout:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


class TestDeployConfigs(unittest.TestCase):
    def test_all_deploy_files_exist_and_non_empty(self) -> None:
        for path in ALL_DEPLOY_FILES:
            self.assertTrue(path.is_file(), f"Missing deploy file: {path}")
            self.assertGreater(path.stat().st_size, 100, f"Deploy file unexpectedly small: {path}")

    def test_cpa_config_loopback_and_no_preview_downgrade(self) -> None:
        data = yaml.safe_load(CPA_CONFIG.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)

        self.assertEqual(data.get("host"), "127.0.0.1")
        self.assertEqual(data.get("port"), 8317)
        self.assertIs(data.get("disable-image-generation"), False)

        quota = data.get("quota-exceeded")
        self.assertIsInstance(quota, dict)
        self.assertIs(quota.get("switch-project"), True)
        self.assertIs(quota.get("switch-preview-model"), False)

        streaming = data.get("streaming")
        self.assertIsInstance(streaming, dict)
        self.assertEqual(streaming.get("keepalive-seconds"), 15)
        self.assertIs(streaming.get("send-keepalive-before-ft"), True)

        remote_mgmt = data.get("remote-management")
        self.assertIsInstance(remote_mgmt, dict)
        self.assertIs(remote_mgmt.get("allow-remote"), False)

        secret_key = str(remote_mgmt.get("secret-key") or "")
        self.assertGreaterEqual(len(secret_key), 24)
        self.assertIn("__REPLACE_WITH_", secret_key)
        self.assertNotIn(secret_key.lower(), {"changeme", "secret", "admin", "password", "123456"})

        api_keys = data.get("api-keys")
        self.assertIsInstance(api_keys, list)
        self.assertGreaterEqual(len(api_keys), 1)
        for key in api_keys:
            self.assertIn("__REPLACE_WITH_", str(key))

    def test_systemd_units_hardening_and_zero_cli_secrets(self) -> None:
        forbidden_cli_secret_markers = (
            "--api-key",
            "--token",
            "--secret",
            "--password",
            "Bearer ",
            "XIAOSHUO_ADMIN_TOKEN",
            "XIAOSHUO_COMPANION_SECRET",
            "CPA_MANAGEMENT_KEY",
            "TELEGRAM_BOT_TOKEN",
        )

        for unit_path in (CPA_SERVICE, BOT_SERVICE):
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str  # preserve case
            parser.read_string(unit_path.read_text(encoding="utf-8"))

            self.assertTrue(parser.has_section("Service"), f"{unit_path.name} missing [Service]")
            svc = parser["Service"]

            self.assertEqual(svc.get("Restart"), "on-failure")
            self.assertEqual(svc.get("EnvironmentFile"), "/etc/xiaoshuo-bot/xiaoshuo.env")
            self.assertNotEqual((svc.get("User") or "root").strip(), "root")
            self.assertEqual(svc.get("NoNewPrivileges"), "true")
            self.assertEqual(svc.get("PrivateTmp"), "true")
            self.assertEqual(svc.get("ProtectSystem"), "strict")

            exec_start = svc.get("ExecStart") or ""
            self.assertTrue(exec_start, f"{unit_path.name} missing ExecStart")
            for marker in forbidden_cli_secret_markers:
                self.assertNotIn(
                    marker,
                    exec_start,
                    f"{unit_path.name} ExecStart must not carry secret/token flag {marker!r}",
                )
            for pat in _REAL_SECRET_PATTERNS:
                self.assertIsNone(pat.search(exec_start), f"Real secret pattern in {unit_path.name} ExecStart")

        bot_parser = configparser.ConfigParser(interpolation=None)
        bot_parser.optionxform = str
        bot_parser.read_string(BOT_SERVICE.read_text(encoding="utf-8"))
        bot_exec = bot_parser["Service"]["ExecStart"]
        self.assertIn("xiaoshuo-bot serve", bot_exec)
        self.assertIn("--host 127.0.0.1", bot_exec)

    def test_caddyfile_reverse_proxy_and_sse_safety(self) -> None:
        text = CADDYFILE.read_text(encoding="utf-8")
        self.assertIn("reverse_proxy 127.0.0.1:8080", text)
        self.assertIn("flush_interval -1", text)
        self.assertIn("request_body", text)
        self.assertIn("max_size", text)
        self.assertIn("X-Real-IP", text)
        self.assertNotIn("reverse_proxy 127.0.0.1:8317", text)
        self.assertNotIn("reverse_proxy localhost:8317", text)

    def test_caddyfile_https_only_with_hsts_and_csp(self) -> None:
        text = CADDYFILE.read_text(encoding="utf-8")
        # Active (non-comment) lines only
        active = "\n".join(
            ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
        )
        # Production site is the domain block (Caddy auto-HTTPS)
        self.assertRegex(active, r"\{\$XIAOSHUO_DOMAIN\}\s*\{\s*import xiaoshuo_proxy\s*\}")
        # :80 block must ONLY redirect to HTTPS, never proxy the console in plaintext
        m = re.search(r"^:80\s*\{(.*?)^\}", active, re.S | re.M)
        self.assertIsNotNone(m, "Caddyfile must define a :80 redirect block")
        body = m.group(1).strip()
        self.assertEqual(body, "redir https://{host}{uri} permanent")
        self.assertNotIn("import", body)
        self.assertNotIn("reverse_proxy", body)
        self.assertIn('Strict-Transport-Security "max-age=31536000"', active)
        self.assertIn("Content-Security-Policy", active)
        self.assertIn("frame-ancestors 'none'", active)
        # deploy script must substitute XIAOSHUO_DOMAIN before caddy validate
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        subst_idx = script.find("XIAOSHUO_DOMAIN}/${xiaoshuo_domain}/g")
        validate_idx = script.find('caddy validate --config "${stage_dir}/${caddy_name}"')
        self.assertNotEqual(subst_idx, -1)
        self.assertLess(subst_idx, validate_idx)
        env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("XIAOSHUO_DOMAIN=__REPLACE_WITH_", env_text)

    def test_deploy_remote_sh_requires_strong_admin_token(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("XIAOSHUO_ADMIN_TOKEN=", text)
        self.assertIn('"${#admin_token_val}" -lt 24', text)
        self.assertIn("*REPLACE*", text)

    def test_env_example_documents_required_vars_with_placeholders_only(self) -> None:
        env_map: dict[str, str] = {}
        for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertIn("=", line, f"Malformed env line: {raw_line!r}")
            key, val = line.split("=", 1)
            env_map[key.strip()] = val.strip()

        required_keys = (
            "XIAOSHUO_HOST",
            "XIAOSHUO_PORT",
            "XIAOSHUO_ADMIN_TOKEN",
            "XIAOSHUO_COMPANION_SECRET",
            "XIAOSHUO_COMPANION_HMAC_SECRET",
            "CPA_MANAGEMENT_KEY",
            "XIAOSHUO_API_BASE",
            "XIAOSHUO_BASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_OWNER_USER_ID",
        )
        for key in required_keys:
            self.assertIn(key, env_map, f"env.example missing {key}")
            self.assertTrue(env_map[key], f"env.example has empty value for {key}")

        self.assertEqual(env_map["XIAOSHUO_HOST"], "127.0.0.1")
        self.assertEqual(env_map["XIAOSHUO_API_BASE"], "http://127.0.0.1:8317/v1")
        self.assertEqual(env_map["XIAOSHUO_BASE_URL"], "http://127.0.0.1:8317/v1")
        secret_keys = (
            "XIAOSHUO_ADMIN_TOKEN",
            "XIAOSHUO_COMPANION_SECRET",
            "XIAOSHUO_COMPANION_HMAC_SECRET",
            "CPA_MANAGEMENT_KEY",
            "XIAOSHUO_API_KEY",
            "XIAOSHUO_IMAGE_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_OWNER_USER_ID",
        )
        for sk in secret_keys:
            self.assertIn("__REPLACE_WITH_", env_map[sk], f"{sk} in env.example must be a placeholder")

    def test_deploy_remote_sh_strict_no_fallback_and_permissions(self) -> None:
        raw_bytes = DEPLOY_SCRIPT.read_bytes()
        self.assertNotIn(b"\r\n", raw_bytes, "deploy_remote.sh must use LF line endings, not CRLF")
        text = raw_bytes.decode("utf-8")

        lines = text.splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/env bash")
        self.assertIn("set -euo pipefail", lines[:5])

        # Strictly prohibited error-masking or destructive patterns
        self.assertNotIn("|| true", text)
        self.assertNotIn("2>/dev/null", text)
        self.assertNotIn("2> /dev/null", text)
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("rm -fr", text)

        # Required verification & hardening checks
        self.assertIn("0600", text)
        self.assertIn("/etc/xiaoshuo-bot/xiaoshuo.env", text)
        self.assertIn("0700", text)
        self.assertIn("/opt/cli-proxy-api/auth-files", text)
        self.assertIn("/opt/cli-proxy-api/cli-proxy-api", text)
        self.assertIn("__REPLACE_WITH_' /opt/cli-proxy-api/config.yaml", text)
        self.assertIn("switch-preview-model", text)
        self.assertIn("disable-image-generation", text)
        self.assertIn("/opt/xiaoshuo-bot/.venv", text)
        self.assertIn("unittest", text)
        self.assertIn("-u PYTHONPATH", text)
        self.assertNotIn("PYTHONPATH=${stage_dir}", text)
        self.assertNotIn('PYTHONPATH="${stage_dir}', text)
        self.assertIn("/api/health", text)
        self.assertIn("127.0.0.1:8317", text)
        self.assertIn("(3, 10)", text)
        self.assertIn("df -Pk", text)

        # Caddyfile must be validated from staging BEFORE overwriting /etc/caddy/Caddyfile
        validate_idx = text.find('caddy validate --config "${stage_dir}/${caddy_name}"')
        install_caddy_idx = text.find('install -m 0644 -o root -g root "${stage_dir}/${caddy_name}" /etc/caddy/Caddyfile')
        self.assertNotEqual(validate_idx, -1, "Missing pre-install caddy validate on staged Caddyfile")
        self.assertNotEqual(install_caddy_idx, -1, "Missing /etc/caddy/Caddyfile install command")
        self.assertLess(validate_idx, install_caddy_idx, "caddy validate must run BEFORE overwriting /etc/caddy/Caddyfile")

        # Required fixtures synced to stage
        for fixture_rel in ("deploy", "projects/baiwu_dukou", "pyproject.toml", ".gitignore", "src"):
            self.assertIn(fixture_rel, text, f"deploy_remote.sh must stage {fixture_rel}")
            self.assertTrue((REPO_ROOT / fixture_rel).exists(), f"Missing repo fixture {fixture_rel}")

    def test_deploy_remote_sh_cloud_test_list_matches_repo_tests(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"cloud_tests=\(\s*(.*?)\s*\)", text, re.DOTALL)
        self.assertIsNotNone(m, "deploy_remote.sh must define explicit cloud_tests=(...) array")
        assert m is not None

        listed = [line.strip() for line in m.group(1).splitlines() if line.strip() and not line.strip().startswith("#")]
        self.assertGreaterEqual(len(listed), 16, f"Expected >=16 cloud test modules, got {len(listed)}")
        self.assertEqual(len(listed), len(set(listed)), "Duplicate entries in cloud_tests")

        listed_names: set[str] = set()
        for rel in listed:
            self.assertTrue(rel.startswith("tests/test_") and rel.endswith(".py"), f"Malformed test entry: {rel}")
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Missing listed test file: {rel}")
            listed_names.add(Path(rel).name)

        companion_only_tests = {"test_companion_and_font.py", "test_full_system_e2e.py"}
        # Local-PC companion audit regression suites (test_audit_companion_*.py) run on the edge PC only.
        companion_only_tests |= {
            p.name for p in (REPO_ROOT / "tests").glob("test_audit_companion_*.py")
        }
        self.assertEqual(
            listed_names & companion_only_tests,
            set(),
            "Local-PC Playwright/companion tests must not be in headless Korea cloud_tests",
        )

        repo_test_names = {p.name for p in (REPO_ROOT / "tests").glob("test_*.py")}
        uncovered = repo_test_names - listed_names - companion_only_tests
        self.assertEqual(
            uncovered,
            set(),
            f"Repo test files missing from deploy_remote.sh cloud_tests: {sorted(uncovered)}",
        )

    def test_deploy_remote_sh_staged_fixture_layout_simulation(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            for rel_dir in ("tests", "deploy", "projects/baiwu_dukou", "src"):
                shutil.copytree(
                    REPO_ROOT / rel_dir,
                    stage / rel_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for rel_file in ("pyproject.toml", ".gitignore"):
                shutil.copy2(REPO_ROOT / rel_file, stage / rel_file)

            self.assertTrue((stage / "deploy" / "Caddyfile.example").is_file())
            self.assertTrue((stage / "projects" / "baiwu_dukou" / "project.yaml").is_file())
            self.assertTrue((stage / "src" / "xiaoshuo_bot" / "server" / "static" / "index.html").is_file())
            self.assertTrue((stage / "src" / "xiaoshuo_bot" / "llm" / "openai_compat.py").is_file())
            self.assertTrue((stage / "pyproject.toml").is_file())
            self.assertTrue((stage / ".gitignore").is_file())

    def test_deploy_remote_sh_bash_syntax(self) -> None:
        bash_bin = _find_working_bash()
        if bash_bin is None:
            self.skipTest("No working GNU bash executable available on host")
        res = subprocess.run(
            [bash_bin, "-n", str(DEPLOY_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"bash -n failed on {DEPLOY_SCRIPT}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}",
        )

    def test_ssh_cpa_tunnel_ps1_strict_and_no_fallback(self) -> None:
        text = TUNNEL_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("$ServerIp", text)
        self.assertIn("$IdentityFile", text)
        self.assertIn("8317", text)
        self.assertIn("127.0.0.1:", text)
        self.assertIn("$LASTEXITCODE", text)

        # Strictly prohibited silent error suppression or semicolon chaining
        self.assertNotIn("SilentlyContinue", text)
        self.assertNotIn("-ErrorAction Ignore", text)
        self.assertNotIn("2>$null", text)
        self.assertNotIn(";", text, "ssh_cpa_tunnel.ps1 must not use ';' command chaining")
        self.assertIn("_*REPLACE", text, "ssh_cpa_tunnel.ps1 must reject __REPLACE_... placeholders")

        # Validate PowerShell parser & placeholder rejection if powershell.exe is available on Windows
        if os.name == "nt":
            ps_bin = shutil.which("powershell")
            if ps_bin:
                ps_check = (
                    "$errs = $null; $toks = $null; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{TUNNEL_SCRIPT}', [ref]$toks, [ref]$errs); "
                    "if ($errs.Count -gt 0) { $errs | ForEach-Object { Write-Error $_.ToString() }; exit 1 }"
                )
                res = subprocess.run(
                    [ps_bin, "-NoProfile", "-Command", ps_check],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"PowerShell parser failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}",
                )

                for bad_ip in ("43.x.x.x", "__REPLACE_WITH_SERVER_IP__"):
                    bad_res = subprocess.run(
                        [
                            ps_bin,
                            "-NoProfile",
                            "-File",
                            str(TUNNEL_SCRIPT),
                            "-ServerIp",
                            bad_ip,
                            "-IdentityFile",
                            str(DEPLOY_SCRIPT),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertNotEqual(bad_res.returncode, 0, f"Tunnel script failed to reject {bad_ip!r}")
                    self.assertIn("Refusing placeholder ServerIp", bad_res.stderr + bad_res.stdout)

    def test_no_real_secrets_or_real_server_ips_across_deploy_kit(self) -> None:
        for path in ALL_DEPLOY_FILES:
            text = path.read_text(encoding="utf-8")
            for pat in _REAL_SECRET_PATTERNS:
                self.assertIsNone(
                    pat.search(text),
                    f"Detected real secret pattern {pat.pattern!r} in {path}",
                )
            self.assertIsNone(
                _CONCRETE_43_IP_RE.search(text),
                f"Detected concrete 43.x.x.x IP address in {path}; must use parameters/placeholders only",
            )


if __name__ == "__main__":
    unittest.main()
