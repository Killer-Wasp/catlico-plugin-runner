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

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:  # pragma: no cover
        raise NotImplementedError


def _tail(data: bytes, limit: int = LOG_TAIL_MAX_BYTES) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


class SubprocessSandboxRunner(SandboxRunner):
    """Trusted-mode adapter: one plugin run per child Python process.

    The plugin is executed by ``plugin_runner._sandbox_worker`` in its own
    process group; on timeout the whole group is killed. The plugin is never
    imported into this runner process.
    """

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
                    log_tail=_tail(stdout),
                )

            log_tail = _tail(stdout or b"")
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
                error_kind="timeout", log_tail=_tail(stdout),
            )

        result, logs = parse_sentinel_result(stdout or b"")
        if result is None:
            return SandboxRunResult(
                run_id=request.run_id, status="failure",
                error="container produced no result", error_kind="bug",
                log_tail=_tail(logs.encode()),
            )
        return SandboxRunResult(
            run_id=request.run_id,
            status=result.get("status", "failure"),
            error=result.get("error"),
            error_kind=result.get("error_kind"),
            skip_reason=result.get("skip_reason"),
            result_summary=result.get("result_summary"),
            operation_count=result.get("operation_count", 0),
            log_tail=_tail(logs.encode()),
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
