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
        (Path("/a"), False, None),
        (Path("/b"), False, None),
        (Path("/c"), True, None),
    ]


def test_load_paths_dest_tables(cfg_env, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    write_cfg(cfg_env, '''[paths]
rw = ["/a", { source = "/state/.claude", dest = "~/.claude" }]
ro = [{ source = "/state/data", dest = "/data" }]
''')
    entries = botbox.load_paths(botbox.read_doc())
    assert entries == [
        (Path("/a"), False, None),
        (Path("/state/.claude"), False, fake_home / ".claude"),
        (Path("/state/data"), True, Path("/data")),
    ]


def test_load_paths_expands_tilde(cfg_env, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / "repo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    write_cfg(cfg_env, '[paths]\nrw = ["~/repo"]\n')
    entries = botbox.load_paths(botbox.read_doc())
    assert entries[0] == (fake_home / "repo", False, None)


def test_load_venv(cfg_env):
    write_cfg(cfg_env, '[python]\nvenv = "/v"\n')
    assert botbox.load_venv(botbox.read_doc()) == Path("/v")


def test_load_env_and_resolve(cfg_env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    write_cfg(cfg_env, '''
[env]
GITHUB_TOKEN = "$GITHUB_TOKEN"
EDITOR = "vim"
HOME = "${HOME}/sub"
MISSING = "$MISSING_VAR"
TILDE = "~/thing"
''')
    env = botbox.load_env(botbox.read_doc())
    assert env == {
        "GITHUB_TOKEN": "$GITHUB_TOKEN",
        "EDITOR": "vim",
        "HOME": "${HOME}/sub",
        "MISSING": "$MISSING_VAR",
        "TILDE": "~/thing",
    }
    resolved = botbox.resolve_env(env)
    assert resolved == {"GITHUB_TOKEN": "gh-secret", "EDITOR": "vim",
                        "HOME": "/home/u/sub", "TILDE": "/home/u/thing"}
    # Missing host var is dropped (a warning is printed on stderr).
    assert "MISSING" not in resolved


def test_load_agents(cfg_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_cfg(cfg_env, """
[agents.claude]
command = "claude"
args = ["--foo", "--bar"]

[agents.codex]
command = "/usr/bin/codex"
""")
    doc = botbox.read_doc()
    agents = botbox.load_agents(doc)
    assert agents["claude"] == {
        "command": "claude",
        "args": ["--foo", "--bar"],
    }
    assert agents["codex"] == {
        "command": "/usr/bin/codex",
        "args": [],
    }


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
    assert (target.resolve(), False, None) in entries


def test_cli_add_ro(cfg_env, tmp_path):
    runner = CliRunner()
    target = tmp_path / "myrepo"
    target.mkdir()
    result = runner.invoke(botbox.app, ["add", "--ro", str(target)])
    assert result.exit_code == 0
    entries = botbox.load_paths(botbox.read_doc())
    assert (target.resolve(), True, None) in entries


def test_cli_add_multiple_paths(cfg_env, tmp_path):
    runner = CliRunner()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = runner.invoke(botbox.app, ["add", str(a), str(b)])
    assert result.exit_code == 0
    entries = botbox.load_paths(botbox.read_doc())
    assert (a.resolve(), False, None) in entries
    assert (b.resolve(), False, None) in entries


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
    assert sum(1 for p, _, _ in entries if p == a.resolve()) == 1
    assert (b.resolve(), False, None) in entries


def test_cli_add_idempotent(cfg_env, tmp_path):
    runner = CliRunner()
    target = tmp_path / "myrepo"
    target.mkdir()
    runner.invoke(botbox.app, ["add", str(target)])
    result = runner.invoke(botbox.app, ["add", str(target)])
    assert result.exit_code == 0
    assert "already present" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert sum(1 for p, _, _ in entries if p == target.resolve()) == 1


def test_cli_add_dest(cfg_env, tmp_path, monkeypatch):
    runner = CliRunner()
    source = tmp_path / "state" / ".claude"
    source.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = runner.invoke(botbox.app, ["add", str(source), "--dest", "~/.claude"])
    assert result.exit_code == 0
    assert f"added paths.rw: {source.resolve()}" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert (source.resolve(), False, (tmp_path / "home" / ".claude").resolve()) in entries
    # Config stores an inline table with source/dest.
    cfg_text = botbox.CONFIG_FILE.read_text()
    assert "source" in cfg_text and "dest" in cfg_text


def test_cli_add_dest_with_multiple_paths_fails(cfg_env, tmp_path):
    runner = CliRunner()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = runner.invoke(botbox.app, ["add", str(a), str(b), "--dest", "/x"])
    assert result.exit_code == 1
    assert "--dest" in (result.stderr or "")
    # Nothing was written.
    assert botbox.load_paths(botbox.read_doc()) == []


def test_cli_rm_removes_from_rw_and_ro(cfg_env, tmp_path):
    runner = CliRunner()
    write_cfg(cfg_env, '[paths]\nrw = ["/a"]\nro = ["/b", "/c"]\n')
    result = runner.invoke(botbox.app, ["rm", "/a", "/b"])
    assert result.exit_code == 0
    assert "removed paths.rw: /a" in result.stdout
    assert "removed paths.ro: /b" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert entries == [(Path("/c"), True, None)]
    assert "not in allowlist" not in result.stdout


def test_cli_rm_reports_unknown_path(cfg_env, tmp_path):
    runner = CliRunner()
    write_cfg(cfg_env, '[paths]\nrw = ["/a"]\n')
    result = runner.invoke(botbox.app, ["rm", "/zzz"])
    assert result.exit_code == 0
    assert "not in allowlist: /zzz" in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert entries == [(Path("/a"), False, None)]


def test_cli_rm_removes_dest_tables(cfg_env, tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_cfg(cfg_env, '[paths]\nrw = [{ source = "/state/.claude", dest = "~/.claude" }]\n')
    result = runner.invoke(botbox.app, ["rm", "/state/.claude"])
    assert result.exit_code == 0
    assert "removed paths.rw: /state/.claude" in result.stdout
    assert botbox.load_paths(botbox.read_doc()) == []


def test_cli_rm_required_args(cfg_env, tmp_path):
    runner = CliRunner()
    result = runner.invoke(botbox.app, ["rm"])
    assert result.exit_code == 1
    result = runner.invoke(botbox.app, ["rm", "--missing", "/a"])
    assert result.exit_code == 1
    assert "error" in (result.stderr or "")


def test_cli_rm_missing_prunes_stale_entries(cfg_env, tmp_path):
    runner = CliRunner()
    exists = tmp_path / "exists"
    exists.mkdir()
    write_cfg(cfg_env, f'[paths]\nrw = ["{exists}"]\nro = ["/opt/claude-code", "/gone"]\n')
    result = runner.invoke(botbox.app, ["rm", "--missing"])
    assert result.exit_code == 0
    assert "removed paths.ro: /opt/claude-code" in result.stdout
    assert f"removed paths.rw: {exists}" not in result.stdout
    entries = botbox.load_paths(botbox.read_doc())
    assert entries == [(exists, False, None)]


def test_cli_rm_missing_nothing_to_do(cfg_env, tmp_path):
    runner = CliRunner()
    exists = tmp_path / "exists"
    exists.mkdir()
    write_cfg(cfg_env, f'[paths]\nrw = ["{exists}"]\n')
    result = runner.invoke(botbox.app, ["rm", "--missing"])
    assert result.exit_code == 0
    assert "no missing entries" in result.stdout
    # Config untouched (still parseable, entry intact).
    assert botbox.load_paths(botbox.read_doc()) == [(exists, False, None)]


def test_cli_remove_alias(cfg_env, tmp_path):
    runner = CliRunner()
    write_cfg(cfg_env, '[paths]\nrw = ["/a"]\n')
    result = runner.invoke(botbox.app, ["remove", "/a"])
    assert result.exit_code == 0
    assert "removed paths.rw: /a" in result.stdout
    assert botbox.load_paths(botbox.read_doc()) == []


def test_build_bwrap_cmd_dest_binds_to_alternate_location(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    state = tmp_path / "state" / ".claude"
    state.mkdir(parents=True)
    write_cfg(cfg_env, '[paths]\nrw = [{ source = "%s", dest = "%s" }]\n' % (state, fake_home / ".claude"))

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])
    pairs = list(zip(cmd, cmd[1:], cmd[2:]))
    assert ("--bind", str(state), str(fake_home / ".claude")) in pairs
    # The host location is NOT bound at its own path.
    assert ("--bind", str(state), str(state)) not in pairs


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


def test_cli_env_set_list_unset(cfg_env):
    runner = CliRunner()
    r0 = runner.invoke(botbox.app, ["env"])
    assert r0.exit_code == 0
    assert "no env set" in r0.stdout

    r1 = runner.invoke(botbox.app, ["env-set", "EDITOR", "vim"])
    assert r1.exit_code == 0
    assert "set env.EDITOR=vim" in r1.stdout
    # Re-setting the same key updates rather than duplicating.
    r2 = runner.invoke(botbox.app, ["env-set", "EDITOR", "nano"])
    assert r2.exit_code == 0
    assert "updated env.EDITOR=nano" in r2.stdout

    r3 = runner.invoke(botbox.app, ["env"])
    assert r3.exit_code == 0
    assert "EDITOR=nano" in r3.stdout

    # `list` includes the env section too.
    r4 = runner.invoke(botbox.app, ["list"])
    assert "env:" in r4.stdout
    assert "EDITOR=nano" in r4.stdout

    r5 = runner.invoke(botbox.app, ["env-unset", "EDITOR"])
    assert r5.exit_code == 0
    assert "unset env.EDITOR" in r5.stdout
    assert "EDITOR" not in botbox.load_env(botbox.read_doc())
    r6 = runner.invoke(botbox.app, ["env-unset", "EDITOR"])
    assert r6.exit_code == 0
    assert "not set" in r6.stdout


def test_cli_env_set_warns_on_missing_host_var(cfg_env, monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    runner = CliRunner()
    result = runner.invoke(botbox.app, ["env-set", "NOPE", "$NOPE_VAR"])
    assert result.exit_code == 0
    assert "not currently set on the host" in result.stderr


def test_build_bwrap_cmd_sets_env_vars(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    write_cfg(cfg_env, '[env]\nGITHUB_TOKEN = "$GITHUB_TOKEN"\nEDITOR = "vim"\n[paths]\nrw = []\n')

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])

    pairs = list(zip(cmd, cmd[1:], cmd[2:]))
    assert ("--setenv", "GITHUB_TOKEN", "gh-secret") in pairs
    assert ("--setenv", "EDITOR", "vim") in pairs
    # Configured env comes after the built-ins so it can override them.
    last_builtin = max(i for i, a in enumerate(cmd) if a in ("HOME", "PATH", "TERM", "LANG"))
    first_custom = next(i for i, a in enumerate(cmd) if a == "GITHUB_TOKEN")
    last_builtin = max(i for i, a in enumerate(cmd) if a in ("HOME", "PATH", "TERM", "LANG"))
    assert first_custom > last_builtin


def test_build_bwrap_cmd_env_overrides_builtin(cfg_env, tmp_path, monkeypatch):
    repo = tmp_path / "work"
    repo.mkdir()
    monkeypatch.chdir(repo)
    write_cfg(cfg_env, '[env]\nLANG = "de_DE.UTF-8"\n[paths]\nrw = []\n')

    _, cmd = botbox.build_bwrap_cmd("/bin/ls", [])

    # Only one --setenv LANG: the configured value overrides the built-in.
    langs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--setenv" and cmd[i + 1] == "LANG"]
    assert langs == ["LANG"]
    assert cmd[cmd.index("LANG") + 1] == "de_DE.UTF-8"


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
        '12345 stat("/opt/claude-code/lib", 0x7fff...) = -1 ENOENT (No such file or directory)\n'
        '12345 access("/usr/share/kicad", F_OK) = -1 ENOENT (No such file or directory)\n'
        '12345 newfstatat(AT_FDCWD, "/opt/claude-code", 0x7fff..., 0) = -1 ENOENT (No such file or directory)\n'
        '12345 lstat("/opt/claude-code/lib/cli.js", 0x7fff...) = -1 ENOENT (No such file or directory)\n'
        '12345 faccessat(AT_FDCWD, "/home/user/.config/kicad", F_OK, 0) = -1 ENOENT (No such file or directory)\n'
        '12345 readlink("/proc/self/exe", 0x7fff..., 4096) = -1 ENOENT (No such file or directory)\n'
        '12345 readlinkat(AT_FDCWD, "/usr/bin/kicad-cli", 0x7fff..., 4096) = -1 ENOENT (No such file or directory)\n'
        '12345 open("/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)\n'
    )
    paths = botbox._parse_strace(sample)
    # original syscalls still work
    assert Path("/opt/claude-code/bin/claude") in paths
    assert Path("/opt/claude-code/lib/cli.js") in paths
    assert Path("/etc/resolv.conf") in paths
    assert not any(str(p).startswith("relative") for p in paths)
    # new syscalls
    assert Path("/opt/claude-code/lib") in paths
    assert Path("/usr/share/kicad") in paths
    assert Path("/opt/claude-code") in paths
    assert Path("/home/user/.config/kicad") in paths
    assert Path("/proc/self/exe") in paths
    assert Path("/usr/bin/kicad-cli") in paths
    assert Path("/etc/ld.so.cache") in paths


def test_is_default_covered_system_and_etc(tmp_path):
    home = tmp_path / "home"
    assert botbox._is_default_covered(Path("/usr/bin/claude"), home, None, [])
    assert botbox._is_default_covered(Path("/etc/resolv.conf"), home, None, [])
    assert botbox._is_default_covered(Path("/etc/ssl/certs/ca.pem"), home, None, [])
    assert not botbox._is_default_covered(Path("/opt/claude-code/bin/claude"), home, None, [])


def test_is_default_covered_home_and_extras():
    home = Path("/home/someone")
    # Agent state dirs like ~/.claude are no longer special-cased.
    assert not botbox._is_default_covered(home / ".claude" / "session.json", home, None, [])
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
    assert (bin_path, True, None) in entries
    assert (pkg, True, None) not in entries


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
    assert (bin_path, True, None) in entries


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
