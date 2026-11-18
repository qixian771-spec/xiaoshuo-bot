from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .base import CPAManagementError, LLMConfigurationError, redact_secrets

_OAUTH_ENDPOINT_ALIASES: Dict[str, str] = {
    "claude": "anthropic-auth-url",
    "anthropic": "anthropic-auth-url",
    "codex": "codex-auth-url",
    "openai": "codex-auth-url",
    "gemini": "gemini-cli-auth-url",
    "gemini-cli": "gemini-cli-auth-url",
    "antigravity": "antigravity-auth-url",
    "qwen": "qwen-auth-url",
    "iflow": "iflow-auth-url",
}


class CPAManagementClient:
    """Client for CLIProxyAPI (CPA) /v0/management/* account pool, usage, and OAuth endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        management_key: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        raw_base = (
            base_url
            if base_url is not None
            else (os.environ.get("CPA_MANAGEMENT_URL") or "")
        )
        raw_key = (
            management_key
            if management_key is not None
            else (os.environ.get("CPA_MANAGEMENT_KEY") or "")
        )

        self.base_url = raw_base.strip().rstrip("/")
        self.management_key = raw_key.strip()
        self.timeout = int(timeout)

        if not self.base_url:
            raise LLMConfigurationError(
                "Missing base_url for CPAManagementClient. "
                "Provide base_url explicitly (e.g. http://127.0.0.1:8317) or set CPA_MANAGEMENT_URL."
            )
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMConfigurationError(
                f"Invalid CPA management base_url: {redact_secrets(self.base_url, [self.management_key])!r}"
            )
        if not self.management_key:
            raise LLMConfigurationError(
                "Missing management_key for CPAManagementClient. "
                "Provide management_key explicitly or set CPA_MANAGEMENT_KEY."
            )
        if self.timeout <= 0:
            raise LLMConfigurationError("timeout must be a positive integer.")

    def _url(self, path: str) -> str:
        clean = path.lstrip("/")
        if self.base_url.endswith("/v0/management"):
            return f"{self.base_url}/{clean}"
        return f"{self.base_url}/v0/management/{clean}"

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=[self.management_key])

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Any:
        url = self._url(path)
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.management_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            err_raw = ""
            if exc.fp is not None:
                try:
                    err_raw = exc.fp.read(1024).decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    err_raw = ""
            msg = self._redact(err_raw.strip() or str(exc.reason or exc))[:500]
            raise CPAManagementError(
                f"CPA management {method} {path} failed with HTTP {exc.code}: {msg}",
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CPAManagementError(
                f"CPA management {method} {path} transport error: {self._redact(str(exc))}"
            ) from None

        if not raw:
            return {"status": "ok"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CPAManagementError(
                f"CPA management {method} {path} returned invalid JSON: {self._redact(str(exc))}"
            ) from None

    def list_auth_files(self) -> List[Dict[str, Any]]:
        """List registered CPA subscription credential files via GET /v0/management/auth-files."""
        data = self._request_json("GET", "auth-files")
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("files", "auth_files", "data"):
                items = data.get(key)
                if isinstance(items, list):
                    return [dict(item) for item in items if isinstance(item, dict)]
        raise CPAManagementError("Unexpected response shape from /v0/management/auth-files.")

    def upload_auth_file(
        self,
        file_path: Optional[str | Path] = None,
        *,
        filename: Optional[str] = None,
        content: Optional[bytes | str] = None,
    ) -> Dict[str, Any]:
        """Upload a .json credential file via multipart/form-data POST /v0/management/auth-files."""
        if file_path is not None:
            path_obj = Path(file_path)
            if not path_obj.is_file():
                raise LLMConfigurationError(f"Auth file does not exist: {path_obj}")
            upload_name = filename or path_obj.name
            raw_bytes = path_obj.read_bytes()
        elif content is not None:
            if not filename:
                raise LLMConfigurationError("filename is required when uploading in-memory content.")
            upload_name = filename
            raw_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        else:
            raise LLMConfigurationError("Provide either file_path or (filename + content) to upload_auth_file.")

        safe_name = Path(upload_name).name
        if not safe_name.lower().endswith(".json"):
            raise LLMConfigurationError(f"CPA auth file must be a .json file, got {safe_name!r}.")

        try:
            json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMConfigurationError(
                f"Auth file {safe_name!r} is not valid UTF-8 JSON: {exc}"
            ) from None

        boundary = f"----xiaoshuo-cpa-{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts = [
            f"--{boundary}".encode("ascii"),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"'.encode("utf-8"),
            b"Content-Type: application/json",
            b"",
            raw_bytes,
            f"--{boundary}--".encode("ascii"),
            b"",
        ]
        body = crlf.join(parts)

        res = self._request_json(
            "POST",
            "auth-files",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return dict(res) if isinstance(res, dict) else {"result": res}

    def delete_auth_file(self, name: str) -> Dict[str, Any]:
        """Delete an auth file by name via DELETE /v0/management/auth-files?name=..."""
        clean_name = (name or "").strip()
        if not clean_name:
            raise LLMConfigurationError("Auth file name cannot be empty.")
        encoded = urllib.parse.quote(clean_name, safe="")
        res = self._request_json("DELETE", f"auth-files?name={encoded}")
        return dict(res) if isinstance(res, dict) else {"result": res}

    def get_usage(self) -> Dict[str, Any]:
        """Fetch CPA gateway usage stats via GET /v0/management/usage."""
        res = self._request_json("GET", "usage")
        return dict(res) if isinstance(res, dict) else {"usage": res}

    def get_oauth_url(
        self,
        provider: str,
        *,
        project_id: Optional[str] = None,
        is_webui: bool = True,
    ) -> Dict[str, Any]:
        """Fetch remote OAuth authorization URL for a subscription provider."""
        key = (provider or "").strip().lower()
        if not key:
            raise LLMConfigurationError("OAuth provider cannot be empty.")
        if key in _OAUTH_ENDPOINT_ALIASES:
            endpoint = _OAUTH_ENDPOINT_ALIASES[key]
        elif key.endswith("-auth-url"):
            endpoint = key
        else:
            endpoint = f"{key}-auth-url"

        params: Dict[str, str] = {"is_webui": "true" if is_webui else "false"}
        if project_id:
            params["project_id"] = project_id.strip()
        query = urllib.parse.urlencode(params)
        res = self._request_json("GET", f"{endpoint}?{query}")
        return dict(res) if isinstance(res, dict) else {"result": res}

    def get_auth_status(self, state: str) -> Dict[str, Any]:
        """Poll OAuth callback status via GET /v0/management/get-auth-status?state=..."""
        clean_state = (state or "").strip()
        if not clean_state:
            raise LLMConfigurationError("OAuth state parameter cannot be empty.")
        encoded = urllib.parse.quote(clean_state, safe="")
        res = self._request_json("GET", f"get-auth-status?state={encoded}")
        return dict(res) if isinstance(res, dict) else {"result": res}
