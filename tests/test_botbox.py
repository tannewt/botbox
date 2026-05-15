from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

import botbox


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """Redirect botbox's config file at a tmp path and disable seed copy."""
    cfg_dir = tmp_path / "botbox"
    cfg_dir.mkdir()
    monkeypatch.setattr(botbox, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(botbox, "CONFIG_FILE", cfg_dir / "config.toml")
    monkeypatch.setattr(botbox, "SEED_CONFIG", tmp_path / "nope")
    return cfg_dir


def write_cfg(cfg_dir: Path, text: str) -> None:
    (cfg_dir / "config.toml").write_text(text)


def test_empty_config_returns_defaults(cfg_env):
    doc = botbox.read_doc()
    assert botbox.load_paths(doc) == []
    assert botbox.load_venv(doc) is None
    assert botbox.load_agents(doc) == {}
    assert botbox.default_agent(doc) is None


def test_load_paths_rw_and_ro(cfg_env):
    write_cfg(cfg_env, '[paths]\nrw = ["/a", "/b"]\nro = ["/c"]\n')
    entries = botbox.load_paths(botbox.read_doc())
    assert entries == [
        (Path("/a"), False),
        (Path("/b"), False),
        (Path("/c"), True),
    ]


def test_load_paths_expands_tilde(cfg_env, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / "repo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    write_cfg(cfg_env, '[paths]\nrw = ["~/repo"]\n')
    entries = botbox.load_paths(botbox.read_doc())
    assert entries[0] == (fake_home / "repo", False)


def test_load_venv(cfg_env):
    write_cfg(cfg_env, '[python]\nvenv = "/v"\n')
    assert botbox.load_venv(botbox.read_doc()) == Path("/v")


def test_load_agents_and_default(cfg_env):
    write_cfg(cfg_env, """
default_agent = "claude"

[agents.claude]
command = "claude"
args = ["--foo", "--bar"]

[agents.codex]
command = "/usr/bin/codex"
""")
    doc = botbox.read_doc()
    assert botbox.default_agent(doc) == "claude"
    agents = botbox.load_agents(doc)
    assert agents["claude"] == {"command": "claude", "args": ["--foo", "--bar"]}
    assert agents["codex"] == {"command": "/usr/bin/codex", "args": []}


def test_cli_list_empty(cfg_env):
    runner = CliRunner()
    result = runner.invoke(botbox.app, ["list"])
    assert result.exit_code == 0
    assert "empty" in result.stdout


def test_cli_add_rw(cfg_env, tmp_path):
    runner = CliRunner()
    target = tmp_path / "myrepo"
    target.mkdir()
    result = runner.invoke(botbox.app, ["add", str(target)])
    assert result.exit_code == 0
    entries = botbox.load_paths(botbox.read_doc())
    assert (target.resolve(), False) in entries


def test_cli_add_ro(cfg_env, tmp_path):
    runner = CliRunner()
    target = tmp_path / "myrepo"
    target.mkdir()
    result = runner.invoke(botbox.app, ["add", "--ro", str(target)])
    assert result.exit_code == 0
    entries = botbox.load_paths(botbox.read_doc())
    assert (target.resolve(), True) in entries


def test_cli_add_idempotent(cfg_env, tmp_path):
    runner = CliRunner()
    target = tmp_path / "myrepo"
    target.mkdir()
    runner.invoke(botbox.app, ["add", str(target)])
    result = runner.invoke(botbox.app, ["add", str(target)])
    assert result.exit_code == 0
    assert "already present" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert sum(1 for p, _ in entries if p == target.resolve()) == 1


def test_cli_venv_set_and_show(cfg_env, tmp_path):
    runner = CliRunner()
    v = tmp_path / "venv"
    v.mkdir()
    r1 = runner.invoke(botbox.app, ["venv", str(v)])
    assert r1.exit_code == 0
    assert "venv set" in r1.stdout
    r2 = runner.invoke(botbox.app, ["venv"])
    assert r2.exit_code == 0
    assert str(v.resolve()) in r2.stdout


def test_build_bwrap_cmd_minimal(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, '[paths]\nrw = []\n')

    bwrap, cmd = botbox.build_bwrap_cmd("/bin/ls", ["-l"])

    assert bwrap.endswith("bwrap")
    assert cmd[0] == bwrap
    # cwd is bound rw ephemerally
    assert "--bind" in cmd
    assert str(repo) in cmd
    # essential isolation flags
    assert "--unshare-pid" in cmd
    assert "--die-with-parent" in cmd
    assert "--share-net" in cmd
    # agent + its args come last
    assert cmd[-2] == "/bin/ls"
    assert cmd[-1] == "-l"


def test_build_bwrap_cmd_binds_venv(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, f'[python]\nvenv = "{venv}"\n[paths]\nrw = []\n')

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])

    # venv is bound read-only
    assert "--ro-bind" in cmd
    indices = [i for i, a in enumerate(cmd) if a == "--ro-bind" and cmd[i + 1] == str(venv)]
    assert indices, f"venv not bound; cmd was {cmd}"
    # bin/ is prepended to PATH
    path_idx = cmd.index("PATH") + 1
    assert cmd[path_idx].startswith(f"{venv}/bin:")
    # VIRTUAL_ENV is set
    assert "VIRTUAL_ENV" in cmd


def test_build_bwrap_cmd_missing_agent(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, '[paths]\nrw = []\n')

    with pytest.raises(click.exceptions.Exit):
        botbox.build_bwrap_cmd("definitely-not-a-real-binary-xyz", [])
