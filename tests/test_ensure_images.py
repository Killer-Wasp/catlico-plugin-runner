"""Startup image-build orchestration (installer.ensure_images) and the main.py
wiring that only invokes it when the active adapter needs per-plugin images.

No real Docker: existence checks and builds are injected fakes.
"""
from plugin_runner import main
from plugin_runner.installer import (
    STATE_FAILED,
    STATE_INSTALLED,
    ensure_images,
    image_tag,
)
from plugin_runner.registry import InstalledPlugin, Registry
from plugin_runner.sandbox import ContainerSandboxRunner, SubprocessSandboxRunner

_GOOD_MANIFEST = {
    "id": "acme", "version": "1.0.0", "entrypoint": "acme.plugin:Plugin",
    "triggers": ["observable.created"], "permissions": ["read:observable"],
    "timeout_seconds": 60,
}


def _plugin(pid="acme", version="1.0.0", manifest=None) -> InstalledPlugin:
    m = {**(manifest or _GOOD_MANIFEST), "id": pid, "version": version}
    return InstalledPlugin(
        id=pid, version=version, manifest=m,
        module="acme.plugin", cls="Plugin", path=f"/plugins/{pid}/src",
    )


class _Spy:
    """Records build invocations; configurable per-tag outcome."""

    def __init__(self, ok_by_tag=None, default_ok=True):
        self.calls = []
        self._ok_by_tag = ok_by_tag or {}
        self._default_ok = default_ok

    async def build(self, plugin, tag):
        self.calls.append(tag)
        return self._ok_by_tag.get(tag, self._default_ok)


async def _never_exists(tag):
    return False


async def _always_exists(tag):
    return True


async def test_existing_image_is_not_rebuilt():
    spy = _Spy()
    states = await ensure_images(
        [_plugin()], runtime="docker", exists=_always_exists, build=spy.build
    )
    assert states == {"acme": STATE_INSTALLED}
    assert spy.calls == []  # build skipped because image already exists


async def test_missing_image_is_built_once_with_correct_tag():
    spy = _Spy()
    states = await ensure_images(
        [_plugin()], runtime="docker", exists=_never_exists, build=spy.build
    )
    assert states == {"acme": STATE_INSTALLED}
    assert spy.calls == [image_tag("acme", "1.0.0")]


async def test_one_build_failure_does_not_block_healthy_plugins():
    good = _plugin("good", "1.0.0")
    bad = _plugin("bad", "2.0.0")
    bad_tag = image_tag("bad", "2.0.0")
    spy = _Spy(ok_by_tag={bad_tag: False})
    states = await ensure_images(
        [good, bad], runtime="docker", exists=_never_exists, build=spy.build
    )
    assert states == {"good": STATE_INSTALLED, "bad": STATE_FAILED}
    # both were attempted; the bad one did not raise
    assert set(spy.calls) == {image_tag("good", "1.0.0"), bad_tag}


async def test_a_raising_build_is_isolated_and_marked_failed():
    async def _boom(plugin, tag):
        raise RuntimeError("docker daemon down")

    states = await ensure_images(
        [_plugin("good"), _plugin("boom", "3.0.0")],
        runtime="docker",
        exists=_never_exists,
        build=_boom,
    )
    # Both plugins route through the raising build; neither propagates.
    assert states["good"] == STATE_FAILED
    assert states["boom"] == STATE_FAILED


async def test_invalid_manifest_is_failed_and_never_built():
    spy = _Spy()
    bad = _plugin("noent", manifest={"id": "noent", "version": "1.0.0"})  # no entrypoint/triggers
    states = await ensure_images(
        [bad], runtime="docker", exists=_never_exists, build=spy.build
    )
    assert states == {"noent": STATE_FAILED}
    assert spy.calls == []  # validation failed -> existence never checked, never built


async def test_main_skips_ensure_for_subprocess_adapter(monkeypatch):
    called = {"n": 0}

    async def _tracker(*args, **kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(main, "ensure_images", _tracker)
    registry = Registry()
    registry.add(_plugin())
    await main._ensure_plugin_images(SubprocessSandboxRunner(), registry)
    assert called["n"] == 0


async def test_main_runs_ensure_for_container_adapter(monkeypatch):
    seen = {}

    async def _tracker(plugins, *, runtime, **kwargs):
        seen["plugins"] = list(plugins)
        seen["runtime"] = runtime
        return {p.id: STATE_INSTALLED for p in plugins}

    monkeypatch.setattr(main, "ensure_images", _tracker)
    registry = Registry()
    registry.add(_plugin())
    await main._ensure_plugin_images(ContainerSandboxRunner(runtime="docker"), registry)
    assert [p.id for p in seen["plugins"]] == ["acme"]
    assert seen["runtime"] == "docker"
