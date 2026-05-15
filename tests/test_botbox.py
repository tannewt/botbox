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


def test_load_agents_and_default(cfg_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_cfg(cfg_env, """
default_agent = "claude"

[agents.claude]
command = "claude"
args = ["--foo", "--bar"]
state_dir = "/var/sandbox/claude"

[agents.codex]
command = "/usr/bin/codex"

[agents.shared]
command = "shared"
state_dir = "host"
""")
    doc = botbox.read_doc()
    assert botbox.default_agent(doc) == "claude"
    agents = botbox.load_agents(doc)
    # explicit path is respected
    assert agents["claude"] == {
        "command": "claude",
        "args": ["--foo", "--bar"],
        "state_dir": Path("/var/sandbox/claude"),
    }
    # unset -> auto sandbox dir under ~/.local/share/botbox/<name>
    assert agents["codex"] == {
        "command": "/usr/bin/codex",
        "args": [],
        "state_dir": tmp_path / ".local" / "share" / "botbox" / "codex",
    }
    # "host" sentinel -> None (binds host's real ~/.claude)
    assert agents["shared"]["state_dir"] is None


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


def test_cli_add_multiple_paths(cfg_env, tmp_path):
    runner = CliRunner()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = runner.invoke(botbox.app, ["add", str(a), str(b)])
    assert result.exit_code == 0
    entries = botbox.load_paths(botbox.read_doc())
    assert (a.resolve(), False) in entries
    assert (b.resolve(), False) in entries


def test_cli_add_multiple_paths_partial_dedup(cfg_env, tmp_path):
    runner = CliRunner()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    runner.invoke(botbox.app, ["add", str(a)])
    result = runner.invoke(botbox.app, ["add", str(a), str(b)])
    assert result.exit_code == 0
    assert "already present" in result.stdout
    assert f"added paths.rw: {b.resolve()}" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert sum(1 for p, _ in entries if p == a.resolve()) == 1
    assert (b.resolve(), False) in entries


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


def test_build_bwrap_cmd_state_dir_overlays_claude_home(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    write_cfg(cfg_env, '[paths]\nrw = []\n')

    state = tmp_path / "sandbox-state"
    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [], state_dir=state)

    sc = state / ".claude"
    sj = state / ".claude.json"
    assert sc.is_dir()
    assert sj.is_file()
    assert sj.read_text() == "{}\n"

    # The bind maps the state_dir paths onto the sandbox's ~/.claude locations.
    pairs = list(zip(cmd, cmd[1:], cmd[2:]))
    assert ("--bind", str(sc), str(fake_home / ".claude")) in pairs
    assert ("--bind", str(sj), str(fake_home / ".claude.json")) in pairs
    # And the host's real ~/.claude is NOT bound.
    assert str(fake_home / ".claude") not in [c for c, n in zip(cmd, cmd[1:]) if c == n]


def test_build_bwrap_cmd_without_state_dir_binds_host_claude(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))
    write_cfg(cfg_env, '[paths]\nrw = []\n')

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])
    pairs = list(zip(cmd, cmd[1:], cmd[2:]))
    assert ("--bind", str(fake_home / ".claude"), str(fake_home / ".claude")) in pairs
    assert ("--bind", str(fake_home / ".claude.json"), str(fake_home / ".claude.json")) in pairs


def test_build_bwrap_cmd_dev_path_uses_dev_bind(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    # /dev/null exists on every Linux host; use it as a stand-in for any
    # device path the user might allowlist (e.g. /dev/sandbox for USB).
    write_cfg(cfg_env, '[paths]\nrw = ["/dev/null"]\nro = ["/dev/zero"]\n')

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])

    pairs = list(zip(cmd, cmd[1:], cmd[2:]))
    # Both rw and ro entries under /dev get --dev-bind (no --ro-dev-bind exists).
    assert ("--dev-bind", "/dev/null", "/dev/null") in pairs
    assert ("--dev-bind", "/dev/zero", "/dev/zero") in pairs
    # And they are NOT bound with the regular --bind / --ro-bind flags.
    assert ("--bind", "/dev/null", "/dev/null") not in pairs
    assert ("--ro-bind", "/dev/zero", "/dev/zero") not in pairs
    # --dev /dev must come BEFORE any --dev-bind /dev/* so the fresh /dev
    # tmpfs doesn't mask the binds.
    dev_idx = next(i for i, a in enumerate(cmd) if a == "--dev" and cmd[i + 1] == "/dev")
    null_idx = next(i for i, a in enumerate(cmd) if a == "--dev-bind" and cmd[i + 1] == "/dev/null")
    assert dev_idx < null_idx


def test_build_bwrap_cmd_missing_agent(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, '[paths]\nrw = []\n')

    with pytest.raises(click.exceptions.Exit):
        botbox.build_bwrap_cmd("definitely-not-a-real-binary-xyz", [])


def test_parse_strace_extracts_openat_and_execve(tmp_path):
    sample = tmp_path / "trace.log"
    sample.write_text(
        '12345 execve("/opt/claude-code/bin/claude", ["claude"], 0x7ffe /* 30 vars */) = 0\n'
        '12345 openat(AT_FDCWD, "/opt/claude-code/lib/cli.js", O_RDONLY|O_CLOEXEC) = 3\n'
        '12345 openat(AT_FDCWD, "/etc/resolv.conf", O_RDONLY) = 4\n'
        '12345 openat(AT_FDCWD, "relative/path", O_RDONLY) = 5\n'
    )
    paths = botbox._parse_strace(sample)
    assert Path("/opt/claude-code/bin/claude") in paths
    assert Path("/opt/claude-code/lib/cli.js") in paths
    assert Path("/etc/resolv.conf") in paths
    assert not any(str(p).startswith("relative") for p in paths)


def test_is_default_covered_system_and_etc(tmp_path):
    home = tmp_path / "home"
    assert botbox._is_default_covered(Path("/usr/bin/claude"), home, None, [])
    assert botbox._is_default_covered(Path("/etc/resolv.conf"), home, None, [])
    assert botbox._is_default_covered(Path("/etc/ssl/certs/ca.pem"), home, None, [])
    assert not botbox._is_default_covered(Path("/opt/claude-code/bin/claude"), home, None, [])


def test_is_default_covered_home_and_extras():
    home = Path("/home/someone")
    assert botbox._is_default_covered(home / ".claude" / "session.json", home, None, [])
    assert botbox._is_default_covered(home / ".gitconfig", home, None, [])
    assert not botbox._is_default_covered(home / ".cache" / "thing", home, None, [])
    repo = Path("/srv/myrepo")
    assert botbox._is_default_covered(repo / "src" / "x.py", home, None, [repo])
    assert not botbox._is_default_covered(Path("/srv/other"), home, None, [repo])


def test_autofix_target_picks_package_root():
    assert botbox._autofix_target(Path("/opt/claude-code/bin/claude")) == Path("/opt/claude-code")
    assert botbox._autofix_target(Path("/srv/foo/data/bar")) == Path("/srv/foo")
    assert botbox._autofix_target(Path("/var/lib/postgres/db/x")) == Path("/var/lib/postgres")


def test_autofix_target_falls_back_to_parent(tmp_path):
    f = tmp_path / "thing.txt"
    f.write_text("")
    assert botbox._autofix_target(f) == tmp_path


def test_dedupe_targets_drops_subpaths():
    out = botbox._dedupe_targets([
        Path("/opt/claude-code"),
        Path("/opt/claude-code/bin"),
        Path("/opt/claude-code/lib/foo"),
        Path("/srv/other"),
    ])
    assert out == [Path("/opt/claude-code"), Path("/srv/other")]


def test_consume_trace_flags_strips_leading():
    rest, override = botbox._consume_trace_flags(["--no-trace", "claude", "--resume"])
    assert rest == ["claude", "--resume"]
    assert override is False
    rest, override = botbox._consume_trace_flags(["--trace", "bash"])
    assert rest == ["bash"]
    assert override is True
    rest, override = botbox._consume_trace_flags(["claude", "--no-trace"])
    assert rest == ["claude", "--no-trace"]
    assert override is None


def test_trace_enabled_default_and_config(cfg_env, monkeypatch):
    monkeypatch.setattr(botbox, "_TRACE_OVERRIDE", None)
    assert botbox._trace_enabled() is False
    write_cfg(cfg_env, "trace = true\n")
    assert botbox._trace_enabled() is True
    monkeypatch.setattr(botbox, "_TRACE_OVERRIDE", False)
    assert botbox._trace_enabled() is False


def test_run_under_sandbox_no_trace_skips_strace(cfg_env, monkeypatch):
    monkeypatch.setattr(botbox, "_TRACE_OVERRIDE", False)

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(botbox.subprocess, "run", fake_run)

    rc = botbox.run_under_sandbox(["/usr/bin/bwrap", "echo", "hi"])
    assert rc == 0
    assert captured["cmd"][0] == "/usr/bin/bwrap"
    assert "strace" not in captured["cmd"][0]


def test_run_under_sandbox_offers_autofix_on_failure(cfg_env, tmp_path, monkeypatch):
    pkg = tmp_path / "opt" / "claude-code"
    (pkg / "bin").mkdir(parents=True)
    bin_path = pkg / "bin" / "claude"
    bin_path.write_text("")

    fake_strace = tmp_path / "fake-strace"
    fake_strace.write_text("")
    monkeypatch.setattr(botbox.shutil, "which", lambda name: str(fake_strace) if name == "strace" else None)
    monkeypatch.setattr(botbox, "_TRACE_OVERRIDE", True)
    # Drop /tmp from default-covered set so the tmp_path candidate survives.
    monkeypatch.setattr(botbox, "_SYSTEM_PREFIXES",
                        ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev"))

    def fake_run(cmd):
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(
            f'1 openat(AT_FDCWD, "{bin_path}", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
        )
        class R:
            returncode = 1
        return R()

    monkeypatch.setattr(botbox.subprocess, "run", fake_run)
    # Auto-accept the prompt.
    monkeypatch.setattr(botbox.typer, "prompt", lambda *a, **kw: "a")

    rc = botbox.run_under_sandbox(["/usr/bin/bwrap", str(bin_path)])
    assert rc == 1
    entries = botbox.load_paths(botbox.read_doc())
    # [a]dd all adds each failed path as-is (no rollup).
    assert (bin_path, True) in entries
    assert (pkg, True) not in entries


def test_run_under_sandbox_offers_autofix_on_success(cfg_env, tmp_path, monkeypatch):
    pkg = tmp_path / "opt" / "claude-code"
    (pkg / "bin").mkdir(parents=True)
    bin_path = pkg / "bin" / "claude"
    bin_path.write_text("")

    fake_strace = tmp_path / "fake-strace"
    fake_strace.write_text("")
    monkeypatch.setattr(botbox.shutil, "which", lambda name: str(fake_strace) if name == "strace" else None)
    monkeypatch.setattr(botbox, "_TRACE_OVERRIDE", True)
    monkeypatch.setattr(botbox, "_SYSTEM_PREFIXES",
                        ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev"))

    def fake_run(cmd):
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(
            f'1 openat(AT_FDCWD, "{bin_path}", O_RDONLY) = -1 ENOENT\n'
        )
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(botbox.subprocess, "run", fake_run)
    monkeypatch.setattr(botbox.typer, "prompt", lambda *a, **kw: "a")

    rc = botbox.run_under_sandbox(["/usr/bin/bwrap", "/bin/true"])
    assert rc == 0
    # Even though the command succeeded, the prompt fired and the path was added.
    entries = botbox.load_paths(botbox.read_doc())
    assert (bin_path, True) in entries


def test_main_passes_unknown_command_to_bwrap(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, '[paths]\nrw = []\n')
    monkeypatch.setattr(botbox.sys, "argv", ["botbox", "--no-trace", "ls", "-l"])

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(botbox.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        botbox.main()

    cmd = captured["cmd"]
    assert cmd[0].endswith("bwrap")
    assert cmd[-2].endswith("/ls")
    assert cmd[-1] == "-l"
