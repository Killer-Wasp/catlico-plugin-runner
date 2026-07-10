"""Container sandbox adapter: command construction, result parsing, and (gated)
real-Docker enforcement of the security flags."""
import asyncio
import json
import shutil

import pytest

from plugin_runner.sandbox import (
    RESULT_SENTINEL,
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
