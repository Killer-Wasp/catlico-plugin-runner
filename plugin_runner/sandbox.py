"""Sandbox execution model for plugin runs.

Each plugin run executes in an isolated sandbox. ``SandboxRunner`` is the single
interface; the rest of the runner does not know whether execution uses Docker,
Podman, or a trusted subprocess.

The default adapter here is the subprocess adapter: it runs the plugin in a
separate Python process (never importing plugin code into the long-running
runner), enforces a hard timeout by killing the process group, captures the
plugin's stdout/stderr as a bounded log tail, and returns a structured result.
A container adapter (Docker/Podman) implements the same interface for untrusted
third-party plugins.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Maximum captured stdout/stderr retained on the result (bytes).
LOG_TAIL_MAX_BYTES = 64 * 1024

#: Stable placeholder written in place of a secret in a captured log tail.
REDACTION_MARKER = "***REDACTED***"

#: Minimum length a secret value must have before we redact it. Values shorter
#: than this (e.g. ``""``, ``"1"``, ``"0"``, ``"true"``) are too generic: they
#: occur incidentally throughout normal log output, so redacting them would
#: corrupt or blank the log without protecting a real credential. Real API keys,
#: bearer tokens and run tokens are always comfortably longer than this.
MIN_SECRET_LEN = 5


@dataclass
class SandboxRunRequest:
    """Everything the sandbox needs to execute a plugin run."""

    run_id: str
    plugin_module: str
    plugin_class: str
    event: dict
    config: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)
    plugin_id: str = ""
    plugin_version: str = ""
    permissions: list[str] = field(default_factory=list)
    plugin_path: str = ""
    timeout_seconds: int = 60
    memory_limit_mb: int = 256
    cpu_limit: float = 1.0
    api_base_url: str = ""
    run_token: str = ""


@dataclass
class SandboxRunResult:
    """Output from a sandbox execution."""

    run_id: str
    status: str  # success | failure | timeout | skipped
    result_summary: dict | None = None
    operation_count: int = 0
    error: str | None = None
    error_kind: str | None = None
    skip_reason: str | None = None
    log_tail: str | None = None


class SandboxRunner:
    """Executes plugin runs in isolated environments (subclass interface)."""

    #: Isolation mode this adapter provides. Overridden by each concrete adapter
    #: so callers (e.g. the health endpoint) can report the mode actually in
    #: effect rather than a hardcoded guess.
    isolation_mode: str = "subprocess"

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:  # pragma: no cover
        raise NotImplementedError


def _tail(data: bytes, limit: int = LOG_TAIL_MAX_BYTES) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


def _redact(text: str, secrets: dict | None, run_token: str = "") -> str:
    """Replace every secret value (and the run token) found in ``text`` with a
    stable marker.

    Non-string secret values (ints, bools) are coerced with ``str(...)`` so the
    redactor never crashes on them; they are matched against the printed form a
    plugin would actually emit. Values shorter than ``MIN_SECRET_LEN`` are
    skipped (see that constant). Longer values are replaced first so a secret
    that contains a shorter secret as a substring is fully masked.
    """
    candidates: set[str] = set()
    values = list((secrets or {}).values())
    if run_token:
        values.append(run_token)
    for raw in values:
        value = raw if isinstance(raw, str) else str(raw)
        if len(value) >= MIN_SECRET_LEN:
            candidates.add(value)
    for value in sorted(candidates, key=len, reverse=True):
        text = text.replace(value, REDACTION_MARKER)
    return text


def _log_tail(
    data: bytes,
    secrets: dict | None = None,
    run_token: str = "",
    limit: int = LOG_TAIL_MAX_BYTES,
) -> str:
    """Produce the bounded, secret-redacted log tail for a run.

    Order is redact-then-tail: we redact the *full* decoded output before
    truncating to the last ``limit`` bytes. Tailing first would let a secret
    straddling the truncation boundary survive as a partial (unmatched) string
    and leak; redacting first guarantees no whole secret can reach the tail.
    The final byte-slice preserves the existing 64KB cap semantics.
    """
    text = _redact(data.decode("utf-8", errors="replace"), secrets, run_token)
    return _tail(text.encode("utf-8", errors="replace"), limit)


class SubprocessSandboxRunner(SandboxRunner):
    """Trusted-mode adapter: one plugin run per child Python process.

    The plugin is executed by ``plugin_runner._sandbox_worker`` in its own
    process group; on timeout the whole group is killed. The plugin is never
    imported into this runner process.
    """

    isolation_mode = "subprocess"

    def __init__(self, python_executable: str | None = None):
        self._python = python_executable or sys.executable

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        with tempfile.TemporaryDirectory(prefix="catlico-sandbox-") as tmp:
            result_path = str(Path(tmp) / "result.json")
            payload = {
                "run_id": request.run_id,
                "plugin_module": request.plugin_module,
                "plugin_class": request.plugin_class,
                "plugin_id": request.plugin_id,
                "plugin_version": request.plugin_version,
                "plugin_path": request.plugin_path,
                "permissions": list(request.permissions),
                "event": request.event,
                "config": request.config,
                "secrets": request.secrets,
                "api_base_url": request.api_base_url,
                "run_token": request.run_token,
                "result_path": result_path,
            }
            proc = await asyncio.create_subprocess_exec(
                self._python,
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
                return SandboxRunResult(
                    run_id=request.run_id,
                    status="timeout",
                    error=f"timed out after {request.timeout_seconds}s",
                    error_kind="timeout",
                    log_tail=_log_tail(stdout, request.secrets, request.run_token),
                )

            log_tail = _log_tail(stdout or b"", request.secrets, request.run_token)
            result = self._read_result(result_path)
            if result is None:
                return SandboxRunResult(
                    run_id=request.run_id,
                    status="failure",
                    error="sandbox produced no result",
                    error_kind="bug",
                    log_tail=log_tail,
                )
            return SandboxRunResult(
                run_id=request.run_id,
                status=result.get("status", "failure"),
                error=result.get("error"),
                error_kind=result.get("error_kind"),
                skip_reason=result.get("skip_reason"),
                result_summary=result.get("result_summary"),
                operation_count=result.get("operation_count", 0),
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


RESULT_SENTINEL = "__CATLICO_RESULT__"


def build_container_command(
    request: SandboxRunRequest,
    *,
    image: str,
    runtime: str = "docker",
    container_name: str,
    network: str = "bridge",
) -> list[str]:
    """Pure: the ``docker/podman run`` argv for an untrusted plugin run.

    Security posture: single-use container (``--rm``), read-only root filesystem
    with a writable ``/tmp`` tmpfs, non-root user, hard memory/CPU limits, dropped
    Linux capabilities, and no privilege escalation. Network defaults to a normal
    bridge (the plugin must reach the Catlico API); ``none`` fully isolates a
    plugin that declares no outbound needs.
    """
    return [
        runtime, "run", "--rm", "-i",
        "--name", container_name,
        "--network", network,
        "--memory", f"{request.memory_limit_mb}m",
        "--memory-swap", f"{request.memory_limit_mb}m",  # no swap headroom
        "--cpus", str(request.cpu_limit),
        "--pids-limit", "256",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",  # nobody
        image,
        "python", "-m", "catlico_plugin_sdk._worker",
    ]


def parse_sentinel_result(stdout: bytes) -> tuple[dict | None, str]:
    """Split the worker's sentinel result line from the plugin's log output."""
    result: dict | None = None
    kept: list[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith(RESULT_SENTINEL):
            try:
                result = json.loads(line[len(RESULT_SENTINEL):])
            except json.JSONDecodeError:
                result = None
        else:
            kept.append(line)
    return result, "\n".join(kept)


class ContainerSandboxRunner(SandboxRunner):
    """Untrusted-mode adapter: one plugin run per throwaway container.

    Requires a per-plugin image (built by the install pipeline) that has the SDK
    and the plugin installed. The plugin never touches the runner host: it runs in
    an isolated container killed on timeout.
    """

    isolation_mode = "container"

    def __init__(self, runtime: str = "docker", network: str = "bridge"):
        self._runtime = runtime
        self._network = network

    def _image_for(self, request: SandboxRunRequest) -> str:
        # Convention set by the install pipeline.
        return f"catlico-plugin/{request.plugin_id}:{request.plugin_version}"

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        name = f"catlico-run-{request.run_id}"
        payload = {
            "run_id": request.run_id,
            "plugin_module": request.plugin_module,
            "plugin_class": request.plugin_class,
            "plugin_id": request.plugin_id,
            "plugin_version": request.plugin_version,
            "permissions": list(request.permissions),
            "event": request.event,
            "config": request.config,
            "secrets": request.secrets,
            "api_base_url": request.api_base_url,
            "run_token": request.run_token,
            # No result_path -> worker emits the result on stdout (sentinel).
        }
        command = build_container_command(
            request,
            image=self._image_for(request),
            runtime=self._runtime,
            container_name=name,
            network=self._network,
        )
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode()),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._kill_container(name)
            stdout = await self._drain(proc)
            return SandboxRunResult(
                run_id=request.run_id, status="timeout",
                error=f"timed out after {request.timeout_seconds}s",
                error_kind="timeout",
                log_tail=_log_tail(stdout, request.secrets, request.run_token),
            )

        result, logs = parse_sentinel_result(stdout or b"")
        if result is None:
            return SandboxRunResult(
                run_id=request.run_id, status="failure",
                error="container produced no result", error_kind="bug",
                log_tail=_log_tail(logs.encode(), request.secrets, request.run_token),
            )
        return SandboxRunResult(
            run_id=request.run_id,
            status=result.get("status", "failure"),
            error=result.get("error"),
            error_kind=result.get("error_kind"),
            skip_reason=result.get("skip_reason"),
            result_summary=result.get("result_summary"),
            operation_count=result.get("operation_count", 0),
            log_tail=_log_tail(logs.encode(), request.secrets, request.run_token),
        )

    async def _kill_container(self, name: str) -> None:
        try:
            killer = await asyncio.create_subprocess_exec(
                self._runtime, "kill", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError, FileNotFoundError):
            pass

    @staticmethod
    async def _drain(proc) -> bytes:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return out or b""
        except (asyncio.TimeoutError, ProcessLookupError):
            return b""
