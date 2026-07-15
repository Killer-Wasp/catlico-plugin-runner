"""`plugin-runner install <source>`: local path copy, git clone, name derivation."""
from pathlib import Path

from plugin_runner import main
from plugin_runner.settings import RunnerSettings

_TOML = 'id = "acme"\nversion = "1.0.0"\nentrypoint = "main:catlico"\ntriggers = ["observable.created"]\n'


def _settings(plugins_dir: Path) -> RunnerSettings:
    return RunnerSettings(plugins_dir=str(plugins_dir), shared_secret="s")


def _make_source(base: Path, name: str = "acme") -> Path:
    src = base / name
    src.mkdir(parents=True)
    (src / "catlico-plugin.toml").write_text(_TOML)
    (src / "main.py").write_text("catlico = object()\n")
    return src


def test_local_path_copy(tmp_path):
    plugins = tmp_path / "plugins"
    src = _make_source(tmp_path / "src", "acme")
    code = main.install_command(_settings(plugins), str(src))
    assert code == 0
    assert (plugins / "acme" / "catlico-plugin.toml").is_file()


def test_local_path_name_override(tmp_path):
    plugins = tmp_path / "plugins"
    src = _make_source(tmp_path / "src", "acme")
    code = main.install_command(_settings(plugins), str(src), name="renamed")
    assert code == 0
    assert (plugins / "renamed" / "catlico-plugin.toml").is_file()


def test_local_dir_without_manifest_is_rejected_and_removed(tmp_path):
    plugins = tmp_path / "plugins"
    bad = tmp_path / "src" / "nope"
    bad.mkdir(parents=True)
    (bad / "readme.txt").write_text("not a plugin")
    code = main.install_command(_settings(plugins), str(bad))
    assert code == 1
    assert not (plugins / "nope").exists()  # cleaned up


def test_git_clone_derives_name_from_url(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    captured = {}

    def fake_clone(url, ref, dest, **kw):
        captured["url"] = url
        captured["ref"] = ref
        captured["dest"] = dest
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "catlico-plugin.toml").write_text(_TOML)
        (Path(dest) / "main.py").write_text("catlico = object()\n")
        return "a" * 40

    monkeypatch.setattr(main, "clone_source", fake_clone)
    code = main.install_command(
        _settings(plugins), "https://example.test/my-plugin.git", ref="v1"
    )
    assert code == 0
    # ".git" stripped from the derived dir name.
    assert (plugins / "my-plugin" / "catlico-plugin.toml").is_file()
    assert captured["ref"] == "v1"


def test_git_clone_failure_returns_nonzero(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"

    def boom(url, ref, dest, **kw):
        raise main.GitCloneError("clone failed")

    monkeypatch.setattr(main, "clone_source", boom)
    code = main.install_command(_settings(plugins), "https://example.test/x.git")
    assert code == 1


def test_unknown_source_kind_returns_nonzero(tmp_path):
    plugins = tmp_path / "plugins"
    code = main.install_command(_settings(plugins), "not-a-dir-nor-url")
    assert code == 1


def test_name_from_git_url_strips_dot_git():
    assert main._name_from_git_url("https://x/acme.git") == "acme"
    assert main._name_from_git_url("git@github.com:org/acme.git") == "acme"
    assert main._name_from_git_url("https://x/acme") == "acme"
