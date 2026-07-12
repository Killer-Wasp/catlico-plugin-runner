"""Container sandbox adapter: command construction, result parsing, and (gated)
real-Docker enforcement of the security flags."""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from plugin_runner.sandbox import (
    RESULT_SENTINEL,
    SECRETS_CONTAINER_PATH,
    ContainerSandboxRunner,
    SandboxRunRequest,
    build_container_command,
    parse_sentinel_result,
)


def _request(**kw) -> SandboxRunRequest:
    base = dict(
        run_id="run-1", plugin_module="acme.plugin", plugin_class="Plugin",
        event={}, plugin_id="acme", plugin_version="1.0.0",
        memory_limit_mb=128, cpu_limit=0.5, timeout_seconds=30,
    )
    base.update(kw)
    return SandboxRunRequest(**base)


def test_command_has_security_flags():
    cmd = build_container_command(
        _request(), image="catlico-plugin/acme:1.0.0", container_name="c1",
    )
    joined = " ".join(cmd)
    assert "--rm" in cmd
    assert "--network bridge" in joined
    assert "--memory 128m" in joined
    assert "--memory-swap 128m" in joined  # no swap headroom
    assert "--cpus 0.5" in joined
    assert "--read-only" in cmd
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--user 65534:65534" in joined
    assert cmd[-3:] == ["python", "-m", "catlico_plugin_sdk._worker"]
    assert "catlico-plugin/acme:1.0.0" in cmd


def test_network_none_isolation():
    cmd = build_container_command(
        _request(), image="img", container_name="c", network="none",
    )
    assert "--network none" in " ".join(cmd)


def test_command_includes_readonly_secret_mount():
    cmd = build_container_command(
        _request(), image="img", container_name="c",
        secrets_file="/host/tmp/secrets.json",
    )
    # The host file is bind-mounted read-only at the fixed in-container path.
    assert "-v" in cmd
    mount = cmd[cmd.index("-v") + 1]
    assert mount == f"/host/tmp/secrets.json:{SECRETS_CONTAINER_PATH}:ro"
    # Mount is added before the image + entrypoint (i.e. it's a `run` flag).
    assert cmd.index("-v") < cmd.index("img")


def test_command_has_no_mount_without_secret_file():
    cmd = build_container_command(_request(), image="img", container_name="c")
    assert "-v" not in cmd


def test_parse_sentinel_splits_result_from_logs():
    result = {"run_id": "r1", "status": "success"}
    stdout = (
        b"plugin log line 1\n"
        b"plugin log line 2\n"
        + f"{RESULT_SENTINEL}{json.dumps(result)}".encode()
        + b"\n"
    )
    parsed, logs = parse_sentinel_result(stdout)
    assert parsed == result
    assert "plugin log line 1" in logs
    assert RESULT_SENTINEL not in logs


def test_parse_sentinel_missing_returns_none():
    parsed, logs = parse_sentinel_result(b"just logs, no result\n")
    assert parsed is None
    assert "just logs" in logs


# --- ContainerSandboxRunner.run: secret file lifecycle (fake exec, no Docker) ---


class _FakeProc:
    """Stands in for the container process: records the stdin payload and, while
    the run is still in flight (before cleanup), captures the host secret file's
    perms/contents so the test can assert them after the file is gone."""

    def __init__(self, command, captured):
        self._command = command
        self._captured = captured
        self.pid = 4242

    async def communicate(self, data=None):
        self._captured["stdin"] = data
        mount = self._command[self._command.index("-v") + 1]
        host_file = mount.split(":")[0]
        self._captured["host_file"] = host_file
        st = os.stat(host_file)
        self._captured["file_mode"] = stat.S_IMODE(st.st_mode)
        self._captured["dir_mode"] = stat.S_IMODE(
            os.stat(os.path.dirname(host_file)).st_mode
        )
        with open(host_file) as fh:
            self._captured["file_contents"] = json.load(fh)
        result = {"run_id": "run-1", "status": "success"}
        out = f"\n{RESULT_SENTINEL}{json.dumps(result)}\n".encode()
        return out, b""


async def test_run_mounts_secret_file_and_cleans_up(monkeypatch):
    captured: dict = {}

    async def fake_exec(*command, **kwargs):
        return _FakeProc(list(command), captured)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    secrets = {"api_key": "shhh-super-secret-value-123"}
    req = _request(secrets=secrets)
    result = await ContainerSandboxRunner().run(req)

    assert result.status == "success", result.error
    # Secrets travelled out-of-band: stdin payload carries secrets_path, not secrets.
    payload = json.loads(captured["stdin"].decode())
    assert payload["secrets_path"] == SECRETS_CONTAINER_PATH
    assert "secrets" not in payload
    # The mounted host file held exactly the run's secrets...
    assert captured["file_contents"] == secrets
    # ...was world-readable (container uid 65534 can read it)...
    assert captured["file_mode"] & 0o004, oct(captured["file_mode"])
    # ...inside a private 0700 parent dir (protects it on the host).
    assert captured["dir_mode"] == 0o700, oct(captured["dir_mode"])
    # ...and the host file/dir are removed after the run (no leaked secret).
    assert not os.path.exists(captured["host_file"])
    assert not os.path.exists(os.path.dirname(captured["host_file"]))


async def test_run_cleans_up_secret_file_on_failure(monkeypatch):
    """Cleanup happens even when the container path raises mid-run."""
    captured: dict = {}

    async def boom_exec(*command, **kwargs):
        # Record the host file the runner just wrote, then blow up the run.
        mount = list(command)[list(command).index("-v") + 1]
        captured["host_file"] = mount.split(":")[0]
        raise RuntimeError("exec failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom_exec)

    with pytest.raises(RuntimeError):
        await ContainerSandboxRunner().run(_request(secrets={"k": "v-really-long"}))
    assert not os.path.exists(captured["host_file"])
    assert not os.path.exists(os.path.dirname(captured["host_file"]))


async def test_run_cleans_up_when_secret_write_fails(monkeypatch):
    """Cleanup must hold even if writing the secret file itself raises (e.g. a
    non-JSON-serializable secret value, or ENOSPC mid-write) -- the temp dir is
    acquired before the guarded block, so it never leaks on the host."""
    import plugin_runner.sandbox as sandbox

    created: list[str] = []
    real_mkdtemp = sandbox.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", recording_mkdtemp)

    # A non-JSON-serializable secret value makes json.dump raise mid-write.
    req = _request(secrets={"bad": object()})
    with pytest.raises(TypeError):
        await ContainerSandboxRunner().run(req)

    assert created, "expected a temp secrets dir to be created"
    assert not os.path.exists(created[0])  # no leaked (partially-written) secret dir


# --- Gated: real Docker enforcement ---

_DOCKER = shutil.which("docker")


async def _docker_probe(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    return proc.returncode, (out or b"").decode()


@pytest.mark.skipif(not _DOCKER, reason="docker not available")
async def test_security_flags_are_enforced_by_docker():
    """Reuse the adapter's flags but swap the entrypoint for a probe: the run must
    be non-root (uid 65534) and the root filesystem read-only."""
    req = _request(memory_limit_mb=64, cpu_limit=0.5)
    cmd = build_container_command(req, image="busybox:latest", container_name="catlico-probe")
    # Replace the trailing `python -m worker` with a shell probe.
    probe = cmd[:-3] + ["sh", "-c", "id -u; touch /root/x 2>/dev/null && echo WRITABLE || echo READONLY"]
    try:
        code, out = await _docker_probe(probe[1:])  # drop leading "docker"
    except asyncio.TimeoutError:
        pytest.skip("docker pull/run too slow in this environment")
    if code != 0 and "Unable to find image" in out and "error" in out.lower():
        pytest.skip(f"docker unavailable: {out[:120]}")
    assert "65534" in out  # ran as nobody, not root
    assert "READONLY" in out  # root filesystem is read-only


# --- Gated: real Docker end-to-end secret delivery via the mounted file ---

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SDK_SRC = _REPO_ROOT / "catlico-plugin-sdk"

_SECRET_PROBE_PLUGIN = '''
from catlico_plugin_sdk import CatlicoPlugin


class Plugin(CatlicoPlugin):
    async def should_process(self, event, ctx):
        return True

    async def process(self, event, ctx):
        # Prove the secret arrived via the read-only mount. Never print the value
        # itself (it would be redacted) -- print only a match marker.
        got = ctx.secrets.get("api_key")
        assert got == "expected-mounted-secret-value-XYZ", (
            "secret not delivered through the mount: %r" % (got,)
        )
        print("SECRET_DELIVERED_OK", flush=True)
'''

_SDK_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "*.egg-info", "dist", "build",
    ".pytest_cache", ".mypy_cache", "node_modules",
)


@pytest.mark.skipif(not _DOCKER, reason="docker not available")
@pytest.mark.skipif(
    not (_SDK_SRC / "pyproject.toml").is_file(), reason="sibling SDK checkout absent"
)
async def test_secret_delivered_via_mounted_file_e2e(tmp_path):
    """Build a real plugin image (SDK + a probe plugin) and run it through the
    ContainerSandboxRunner: the plugin must receive its secret from the mounted
    read-only file, end-to-end. This proves the mount + worker read path."""
    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    (ctx_dir / "secret_probe.py").write_text(textwrap.dedent(_SECRET_PROBE_PLUGIN))
    shutil.copytree(_SDK_SRC, ctx_dir / ".catlico-sdk", ignore=_SDK_COPY_IGNORE)
    (ctx_dir / "Dockerfile.catlico").write_text(
        "FROM python:3.14-slim\n"
        "RUN useradd --uid 65534 --no-create-home nobodyplugin || true\n"
        "WORKDIR /plugin\n"
        "COPY . /plugin\n"
        "RUN pip install --no-cache-dir /plugin/.catlico-sdk && rm -rf /plugin/.catlico-sdk\n"
        "ENV PYTHONPATH=/plugin/src:/plugin\n"
        "USER 65534:65534\n"
    )
    tag = "catlico-plugin/secretprobe:1.0.0"
    try:
        build = subprocess.run(
            ["docker", "build", "-f", str(ctx_dir / "Dockerfile.catlico"),
             "-t", tag, str(ctx_dir)],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("docker build too slow in this environment")
    if build.returncode != 0:
        pytest.skip(f"docker build failed: {build.stdout[-300:]}{build.stderr[-300:]}")

    try:
        req = _request(
            plugin_id="secretprobe", plugin_version="1.0.0",
            plugin_module="secret_probe", plugin_class="Plugin",
            event={"event_id": "e:1", "event_type": "observable.created",
                   "organisation_id": "org-a",
                   "object": {"type": "observable", "id": "o1"}},
            secrets={"api_key": "expected-mounted-secret-value-XYZ"},
            timeout_seconds=120,
        )
        # network=none: the probe needs no outbound access; keeps the test isolated.
        result = await ContainerSandboxRunner(network="none").run(req)
    finally:
        subprocess.run(["docker", "rmi", "-f", tag],
                       capture_output=True, text=True)

    assert result.status == "success", f"{result.error}\n{result.log_tail}"
    assert "SECRET_DELIVERED_OK" in (result.log_tail or "")
