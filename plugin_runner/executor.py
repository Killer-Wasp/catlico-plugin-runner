"""Plugin execution — one plugin run per child process, bound to the plugin's venv.

There is **no sandbox**: plugins are trusted first-party code that may legitimately
need full system access (awscli, tooling on PATH), so a run is a plain subprocess
that inherits the runner's environment. ``PluginExecutor`` is the single interface
the rest of the runner uses; ``SubprocessExecutor`` is the only implementation.
The boundary is kept clean (the runner never imports plugin code) so a stronger
container/bwrap adapter could slot in later if untrusted plugins ever arrive.

What this layer still provides — as execution mechanics, not a containment
boundary — is crash isolation (the plugin runs in its own process, never in the
long-running runner), a hard timeout enforced by killing the process group, and a
secret-redacted, bounded log tail (protects stored logs).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

#: Maximum captured stdout/stderr retained on the result (bytes).
LOG_TAIL_MAX_BYTES = 64 * 1024

#: Stable placeholder written in place of a secret in a captured log tail.
REDACTION_MARKER = "***REDACTED***"

#: Minimum length a secret value must have before we redact it. Values shorter
#: than this (e.g. ``""``, ``"1"``, ``"0"``, ``"true"``) are too generic: they
#: occur incidentally throughout normal log output, so redacting them would
#: corrupt or blank the log without protecting a real credential.
MIN_SECRET_LEN = 5


@dataclass
class RunRequest:
    """Everything the executor needs to run one plugin execution."""

    run_id: str
    plugin_module: str
    plugin_object: str
    event: dict
    config: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)
    plugin_id: str = ""
    plugin_version: str = ""
    permissions: list[str] = field(default_factory=list)
    declared_triggers: list[str] = field(default_factory=list)
    plugin_path: str = ""
    python_executable: str = ""  # the plugin's venv python; falls back to the runner's
    timeout_seconds: int = 60
    api_base_url: str = ""
    run_token: str = ""
    action: str = "event"  # "event" | "health"


@dataclass
class RunResult:
    """Output from a plugin execution."""

    run_id: str
    status: str  # success | failure | timeout | skipped
    result_summary: dict | None = None
    operation_count: int = 0
    error: str | None = None
    error_kind: str | None = None
    skip_reason: str | None = None
    health: dict | None = None
    log_tail: str | None = None


class PluginExecutor:
    """Executes plugin runs (subclass interface)."""

    #: Reported to callers (e.g. the health endpoint). Always "subprocess" — there
    #: is no other execution model.
    isolation_mode: str = "subprocess"

    async def run(self, request: RunRequest) -> RunResult:  # pragma: no cover
        raise NotImplementedError


def _tail(data: bytes, limit: int = LOG_TAIL_MAX_BYTES) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


def _secret_variants(value: str) -> set[str]:
    """Expand a raw secret into itself plus the encoded forms a plugin might log.

    Covers standard and URL-safe base64 (with and without padding) and both
    URL percent-encodings. Callers must only invoke this once ``value`` has passed
    the ``MIN_SECRET_LEN`` gate.
    """
    variants: set[str] = {value}
    raw_bytes = value.encode("utf-8", errors="ignore")
    if raw_bytes:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        variants.add(b64)
        variants.add(b64.rstrip("="))
        urlsafe = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
        variants.add(urlsafe)
        variants.add(urlsafe.rstrip("="))
    variants.add(urllib.parse.quote(value))
    variants.add(urllib.parse.quote_plus(value))
    variants.discard("")
    return variants


def _redact(text: str, secrets: dict | None, run_token: str = "") -> str:
    """Replace every secret value (and the run token) in ``text`` with a marker,
    in raw and common encoded forms. Non-string secrets are coerced with ``str``;
    values shorter than ``MIN_SECRET_LEN`` are skipped. Longer candidates are
    replaced first so any shorter substring is still fully masked."""
    candidates: set[str] = set()
    values = list((secrets or {}).values())
    if run_token:
        values.append(run_token)
    for raw in values:
        value = raw if isinstance(raw, str) else str(raw)
        if len(value) >= MIN_SECRET_LEN:
            candidates |= _secret_variants(value)
    for value in sorted(candidates, key=len, reverse=True):
        text = text.replace(value, REDACTION_MARKER)
    return text


def _log_tail(
    data: bytes,
    secrets: dict | None = None,
    run_token: str = "",
    limit: int = LOG_TAIL_MAX_BYTES,
) -> str:
    """Bounded, secret-redacted log tail. Redact the full output first, then tail —
    so a secret straddling the truncation boundary can never survive as a partial."""
    text = _redact(data.decode("utf-8", errors="replace"), secrets, run_token)
    return _tail(text.encode("utf-8", errors="replace"), limit)


class SubprocessExecutor(PluginExecutor):
    """The execution adapter: one plugin run per child Python process.

    The plugin is executed by ``catlico_plugin_sdk._worker`` in its own process
    group (killed as a group on timeout), run with the plugin's own venv
    interpreter (``request.python_executable``) so its dependencies come solely
    from that venv. The environment is inherited (plugins may need PATH, awscli,
    AWS creds). The plugin is never imported into this runner process.
    """

    isolation_mode = "subprocess"

    def __init__(self, python_executable: str | None = None):
        #: Fallback interpreter when a request carries no venv python (e.g. tests).
        self._python = python_executable or sys.executable

    async def run(self, request: RunRequest) -> RunResult:
        python = request.python_executable or self._python
        with tempfile.TemporaryDirectory(prefix="catlico-run-") as tmp:
            result_path = str(Path(tmp) / "result.json")
            payload = {
                "run_id": request.run_id,
                "action": request.action,
                "plugin_module": request.plugin_module,
                "plugin_object": request.plugin_object,
                "plugin_id": request.plugin_id,
                "plugin_version": request.plugin_version,
                "plugin_path": request.plugin_path,
                "declared_triggers": list(request.declared_triggers),
                "permissions": list(request.permissions),
                "event": request.event,
                "config": request.config,
                "secrets": request.secrets,
                "api_base_url": request.api_base_url,
                "run_token": request.run_token,
                "result_path": result_path,
            }
            proc = await asyncio.create_subprocess_exec(
                python,
                "-m",
                "catlico_plugin_sdk._worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group for group-kill on timeout
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(json.dumps(payload).encode()),
                    timeout=request.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._kill_group(proc)
                stdout = await self._drain(proc)
                return RunResult(
                    run_id=request.run_id,
                    status="timeout",
                    error=f"timed out after {request.timeout_seconds}s",
                    error_kind="timeout",
                    log_tail=_log_tail(stdout, request.secrets, request.run_token),
                )

            log_tail = _log_tail(stdout or b"", request.secrets, request.run_token)
            result = self._read_result(result_path)
            if result is None:
                return RunResult(
                    run_id=request.run_id,
                    status="failure",
                    error="plugin process produced no result",
                    error_kind="bug",
                    log_tail=log_tail,
                )
            return RunResult(
                run_id=request.run_id,
                status=result.get("status", "failure"),
                error=result.get("error"),
                error_kind=result.get("error_kind"),
                skip_reason=result.get("skip_reason"),
                result_summary=result.get("result_summary"),
                operation_count=result.get("operation_count", 0),
                health=result.get("health"),
                log_tail=log_tail,
            )

    @staticmethod
    def _kill_group(proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    @staticmethod
    async def _drain(proc) -> bytes:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return out or b""
        except (asyncio.TimeoutError, ProcessLookupError):
            return b""

    @staticmethod
    def _read_result(path: str) -> dict | None:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
