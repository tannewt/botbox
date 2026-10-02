"""Run agents (claude, etc.) inside a bubblewrap sandbox with an opt-in allowlist.

Config lives at ~/.config/botbox/config.toml. Each agents.<name> table becomes
a subcommand (e.g. `botbox claude`) that runs the configured command with its
default args, plus any extra args passed on the CLI.

Any unknown subcommand is forwarded straight to bwrap (e.g. `botbox bash` runs
bash inside the sandbox), so arbitrary one-off commands work without config.

Pass `--trace` (before the subcommand) to wrap the invocation in
`strace -e trace=openat,open,stat,lstat,newfstatat,access,faccessat,readlink,readlinkat,execve --status=failed`.
After the command exits botbox shows which host paths it tried to open but couldn't reach inside
the sandbox and offers to add them to the allowlist — regardless of whether
the command succeeded. Set `trace = true` in the config to enable on every
invocation.

Pass `--no-venv` (before the subcommand) to skip binding and setting PATH/VIRTUAL_ENV
for the configured Python venv for this invocation. Useful for running commands that
shouldn't see the venv.

Env variables for the sandbox live in the [env] table of the config and are
managed with `botbox env` (list), `botbox env-set KEY value` and
`botbox env-unset KEY`. A value starting with `$` (e.g. "$GITHUB_TOKEN") is
read from the host environment when the sandbox starts.

See the README at https://github.com/... or the source for a full config example.
"""

# Example config.toml:
#
#   default_agent = "claude"
#
#   [python]
#   venv = "/home/tannewt/repos/venv"   # bound RO; bin/ prepended to PATH
#
#   [paths]
#   rw = ["/home/tannewt/repos/circuitpython"]
#   ro = []
#
#   [agents.claude]
#   command = "claude"
#   args = ["--dangerously-skip-permissions"]
#
# Design follows
# https://patrickmccanna.net/a-detailed-writeup-of-claude-code-constrained-by-bubblewrap/
# with: a TOML config so repos, a default venv, and agents can be opted in once;
# no --new-session (would break the interactive TTY).

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import tomlkit
import typer

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "botbox"
CONFIG_FILE = CONFIG_DIR / "config.toml"
SEED_CONFIG = Path(__file__).resolve().parent / "default-config.toml"

PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=__doc__,
)


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def read_doc() -> tomlkit.TOMLDocument:
    if not CONFIG_FILE.exists() and SEED_CONFIG.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SEED_CONFIG, CONFIG_FILE)
        typer.echo(f"seeded {CONFIG_FILE} from {SEED_CONFIG}", err=True)
    if not CONFIG_FILE.exists():
        return tomlkit.document()
    return tomlkit.parse(CONFIG_FILE.read_text())


def write_doc(doc: tomlkit.TOMLDocument) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(tomlkit.dumps(doc))


def load_paths(doc) -> list[tuple[Path, bool, Optional[Path]]]:
    """Return (source, read_only, dest) triples. Entries are strings (mounted
    at the same path) or inline tables { source = "...", dest = "..." } (mounted
    at a different path inside the sandbox)."""
    paths = doc.get("paths") or {}
    out: list[tuple[Path, bool, Optional[Path]]] = []
    for p in paths.get("rw") or []:
        out.append(_parse_path_entry(p, False))
    for p in paths.get("ro") or []:
        out.append(_parse_path_entry(p, True))
    return out


def _parse_path_entry(p, ro: bool) -> tuple[Path, bool, Optional[Path]]:
    if isinstance(p, dict):
        src = expand(str(p["source"]))
        dest = expand(str(p["dest"])) if p.get("dest") else None
        return src, ro, dest
    return expand(str(p)), ro, None


def load_venv(doc) -> Optional[Path]:
    v = (doc.get("python") or {}).get("venv")
    return expand(str(v)) if v else None


def load_env(doc) -> dict[str, str]:
    """Parse the [env] table: KEY = "value" pairs to set inside the sandbox.
    A value starting with `$` (e.g. "$GITHUB_TOKEN" or "${VSCODE_GIT_ASKPASS}")
    is read from the host environment at launch time."""
    env = doc.get("env") or {}
    return {str(k): str(v) for k, v in env.items()}


# $NAME or ${NAME} at the start of an env value references a host variable.
_HOST_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(.*)$|^\$([A-Za-z_][A-Za-z0-9_]*)(.*)$")


def resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Expand $VAR / ${VAR} references from the host environment and '~' in
    values. Missing host vars are warned about and dropped."""
    out: dict[str, str] = {}
    for k, v in env.items():
        m = _HOST_VAR_RE.match(v)
        if m:
            name, rest = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
            host = os.environ.get(name)
            if host is None:
                typer.echo(f"warning: env {k}={v}: '{name}' not set on host; skipping", err=True)
                continue
            out[k] = host + rest
        elif v.startswith("~"):
            out[k] = os.path.expanduser(v)
        else:
            out[k] = v
    return out


def redact_env_value(v: str) -> str:
    """Redact an env value for display: first 8 characters plus total length,
    so secrets are not printed."""
    return f"{v[:8]}… (length {len(v)})"


def load_agents(doc) -> dict[str, dict]:
    """Parse agent tables into {name: {command, args}}."""
    agents = doc.get("agents") or {}
    out: dict[str, dict] = {}
    for name, cfg in agents.items():
        out[str(name)] = {
            "command": str(cfg.get("command", name)),
            "args": [str(a) for a in (cfg.get("args") or [])],
        }
    return out


def default_agent(doc) -> Optional[str]:
    v = doc.get("default_agent")
    return str(v) if v else None


def die(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(1)


def build_bwrap_cmd(
    agent_command: str,
    agent_args: list[str],
    disable_venv: bool = False,
) -> tuple[str, list[str]]:
    home = Path.home()
    user = os.environ.get("USER", "")
    pwd = Path.cwd().resolve()

    doc = read_doc()
    entries = load_paths(doc)
    venv = load_venv(doc)

    # cwd is always bound rw for the current invocation. If it's not already
    # covered by a configured rw path, add an ephemeral entry (not persisted).
    rw_paths = [p for p, ro, _ in entries if not ro]
    if not any(pwd == p or p in pwd.parents for p in rw_paths):
        entries.append((pwd, False, None))

    bwrap = shutil.which("bwrap") or die("error: bwrap not found; install bubblewrap")
    agent_bin = shutil.which(agent_command) or die(f"error: '{agent_command}' not found on PATH")

    cmd = [bwrap]

    for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(d).exists():
            cmd += ["--ro-bind", d, d]

    # Arch's /lib is a symlink to /usr/lib and we need a /lib32
    cmd += ["--ro-bind", "/usr/lib32", "/lib32"]

    for f in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl",
              "/etc/ca-certificates", "/etc/pki",
              "/etc/passwd", "/etc/group", "/etc/nsswitch.conf",
              "/etc/localtime", "/etc/alternatives"):
        if Path(f).exists():
            cmd += ["--ro-bind", f, f]

    for p in (home / ".gitconfig", home / ".config" / "git"):
        if p.exists():
            cmd += ["--ro-bind", str(p), str(p)]

    known_hosts = home / ".ssh" / "known_hosts"
    if known_hosts.exists():
        cmd += ["--ro-bind", str(known_hosts), str(known_hosts)]

    # if (home / ".nvm").is_dir():
    #     cmd += ["--ro-bind", str(home / ".nvm"), str(home / ".nvm")]

    if (home / ".local").is_dir():
        cmd += ["--ro-bind", str(home / ".local"), str(home / ".local")]

    # if (home / ".npm").is_dir():
    #     cmd += ["--bind", str(home / ".npm"), str(home / ".npm")]

    if venv and not disable_venv:
        if not venv.exists():
            typer.echo(f"warning: venv {venv} missing", err=True)
        elif venv not in [e[0] for e in entries]:
            cmd += ["--ro-bind", str(venv), str(venv)]

    # Mount a fresh /dev before any --dev-bind entries from the allowlist,
    # otherwise the tmpfs overlay would mask binds we just set up.
    cmd += ["--dev", "/dev"]

    for p, ro, dest in entries:
        if not p.exists():
            typer.echo(f"warning: {p} missing; skipping", err=True)
            continue
        s = str(p)
        if s == "/dev" or s.startswith("/dev/"):
            flag = "--dev-bind"
        else:
            flag = "--ro-bind" if ro else "--bind"
        cmd += [flag, s, str(dest) if dest is not None else s]

    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    if not disable_venv and venv and venv.exists():
        path_env = f"{venv}/bin:{path_env}"

    cmd += [
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--share-net",
        "--unshare-pid",
        "--die-with-parent",
        "--chdir", str(pwd),
    ]

    # Built-in env first; configured [env] vars override them (and can add more).
    sandbox_env = {
        "HOME": str(home),
        "USER": user,
        "PATH": path_env,
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if not disable_venv and venv and venv.exists():
        sandbox_env["VIRTUAL_ENV"] = str(venv)
    sandbox_env.update(resolve_env(load_env(doc)))
    for k, v in sandbox_env.items():
        cmd += ["--setenv", k, v]

    cmd += [
        agent_bin,
    ] + agent_args

    return bwrap, cmd


@app.command("list")
def list_cmd() -> None:
    """Print the configured venv, allowlist, and agents."""
    doc = read_doc()
    venv = load_venv(doc)
    entries = load_paths(doc)
    agents = load_agents(doc)
    default = default_agent(doc)
    env = load_env(doc)

    if env:
        typer.echo("env:")
        for k, v in env.items():
            typer.echo(f"  {k}={redact_env_value(v)}")
    if venv:
        missing = "  [MISSING]" if not venv.exists() else ""
        typer.echo(f"venv  {venv}{missing}")
    for p, ro, dest in entries:
        marker = "ro " if ro else "rw "
        missing = "  [MISSING]" if not p.exists() else ""
        dest_str = f" -> {dest}" if dest is not None else ""
        typer.echo(f"{marker}  {p}{dest_str}{missing}")
    if agents:
        typer.echo("agents:")
        for name, cfg in agents.items():
            mark = " (default)" if name == default else ""
            argstr = " " + " ".join(shlex.quote(a) for a in cfg["args"]) if cfg["args"] else ""
            typer.echo(f"  {name}{mark}: {cfg['command']}{argstr}")
    if not env and not venv and not entries and not agents:
        typer.echo(f"(empty; edit {CONFIG_FILE} or run: botbox add)")


@app.command()
def add(
    paths: Optional[list[Path]] = typer.Argument(None, help="Paths to add (default: cwd)."),
    ro: bool = typer.Option(False, "--ro", help="Bind read-only."),
    dest: Optional[Path] = typer.Option(
        None, "--dest", help="Mount point inside the sandbox (default: same path). Single path only."
    ),
) -> None:
    """Add one or more paths to the allowlist."""
    targets = [p.resolve() for p in paths] if paths else [Path.cwd().resolve()]
    if dest is not None and len(targets) > 1:
        die("error: --dest can only be used with a single path")
    doc = read_doc()
    if "paths" not in doc:
        doc["paths"] = tomlkit.table()
    key = "ro" if ro else "rw"
    if key not in doc["paths"]:
        doc["paths"][key] = tomlkit.array()
    arr = doc["paths"][key]
    existing = {
        expand(str(x["source"])) if isinstance(x, dict) else expand(str(x)) for x in arr
    }
    changed = False
    for p in targets:
        if p in existing:
            typer.echo(f"already present in paths.{key}: {p}")
            continue
        if dest is not None:
            row = tomlkit.inline_table()
            row["source"] = str(p)
            row["dest"] = str(dest.expanduser().resolve())
            arr.append(row)
        else:
            arr.append(str(p))
        existing.add(p)
        changed = True
        typer.echo(f"added paths.{key}: {p}" + (f" -> {dest}" if dest is not None else ""))
    if changed:
        write_doc(doc)


@app.command("rm")
def rm(
    paths: Optional[list[Path]] = typer.Argument(
        None, help="Paths to remove from the allowlist (both paths.rw and paths.ro)."
    ),
    missing: bool = typer.Option(
        False, "--missing", help="Remove every allowlist entry that no longer exists on the host."
    ),
) -> None:
    """Remove paths from the allowlist."""
    if missing and paths:
        die("error: --missing cannot be combined with paths")
    if not missing and not paths:
        die("error: give a path to remove (or use --missing to prune stale entries)")
    doc = read_doc()
    doc_paths = doc.get("paths")
    if doc_paths is None:
        typer.echo("(allowlist is empty)")
        return
    wanted: set[Path] = set()
    for p in paths or []:
        wanted.add(expand(str(p)))

    def _drop(entry) -> bool:
        src = _parse_path_entry(entry, False)[0]
        if missing:
            return not Path(src).exists()
        return src in wanted or Path(src).resolve() in wanted

    not_found = set(wanted)
    changed = False
    for key in ("rw", "ro"):
        arr = doc_paths.get(key)
        if arr is None:
            continue
        kept = []
        for entry in arr:
            if _drop(entry):
                src = _parse_path_entry(entry, False)[0]
                not_found.discard(Path(src))
                not_found.discard(Path(src).resolve())
                typer.echo(f"removed paths.{key}: {src}")
                changed = True
            else:
                kept.append(entry)
        if changed and len(kept) != len(list(arr)):
            new_arr = tomlkit.array()
            for entry in kept:
                new_arr.append(entry)
            doc_paths[key] = new_arr
    if missing and not changed:
        typer.echo("no missing entries")
        return
    for p in sorted(not_found, key=str):
        typer.echo(f"not in allowlist: {p}")
    if changed:
        write_doc(doc)


app.command("remove")(rm)


# Patterns for syscalls where the path is the second argument (after a fd/AT_FDCWD)
_PATH_SECOND_RE = re.compile(
    r'\b(?:openat|newfstatat|faccessat|readlinkat|statx)\([^,]+,\s*"((?:[^"\\]|\\.)*)"'
)
# Patterns for syscalls where the path is the first string argument
_PATH_FIRST_RE = re.compile(
    r'\b(?:stat|lstat|access|open|readlink|execve)\("((?:[^"\\]|\\.)*)"'
)

_SYSTEM_PREFIXES = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev", "/tmp")
_ETC_BOUND = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/ssl", "/etc/ca-certificates",
    "/etc/pki", "/etc/passwd", "/etc/group", "/etc/nsswitch.conf",
    "/etc/localtime", "/etc/alternatives",
)
_PKG_ROOTS = ("/opt", "/srv", "/var/lib", "/var/local")


def _parse_strace(trace_file: Path) -> set[Path]:
    """Extract absolute paths from failed file-system related strace lines."""
    paths: set[Path] = set()
    for line in trace_file.read_text(errors="replace").splitlines():
        for rx in (_PATH_SECOND_RE, _PATH_FIRST_RE):
            m = rx.search(line)
            if not m:
                continue
            raw = m.group(1).encode().decode("unicode_escape", errors="replace")
            if raw.startswith("/"):
                paths.add(Path(raw))
    return paths


def _is_default_covered(p: Path, home: Path, venv: Optional[Path], extras: list[Path]) -> bool:
    """True if p is already mounted by build_bwrap_cmd's standard set."""
    s = str(p)
    for d in _SYSTEM_PREFIXES:
        if s == d or s.startswith(d + "/"):
            return True
    for f in _ETC_BOUND:
        if s == f or s.startswith(f + "/"):
            return True
    covered_home = (
        home / ".gitconfig", home / ".config" / "git",
        home / ".ssh" / "known_hosts",
        home / ".local",
    )
    for h in covered_home:
        if p == h or h in p.parents:
            return True
    if venv and (p == venv or venv in p.parents):
        return True
    for e in extras:
        if p == e or e in p.parents:
            return True
    return False


def _autofix_target(p: Path) -> Path:
    """Pick a sensible directory to allowlist for a discovered path."""
    parts = p.parts
    for root in _PKG_ROOTS:
        rp = Path(root).parts
        if len(parts) > len(rp) and parts[: len(rp)] == rp:
            return Path(*parts[: len(rp) + 1])
    return p if p.is_dir() else p.parent


def _dedupe_targets(targets: list[Path]) -> list[Path]:
    out: list[Path] = []
    for t in sorted(set(targets), key=lambda x: x.parts):
        if any(o == t or o in t.parents for o in out):
            continue
        out.append(t)
    return out


def _prompt_target(p: Path) -> Optional[Path]:
    opts: list[tuple[str, Path]] = [("file", p), ("parent dir", p.parent)]
    pkg = _autofix_target(p)
    if pkg not in (o[1] for o in opts):
        opts.append(("package root", pkg))
    typer.echo(f"\n  {p}")
    for i, (label, t) in enumerate(opts, 1):
        typer.echo(f"    [{i}] {label}: {t}")
    typer.echo("    [s] skip")
    raw = typer.prompt("    choice", default="1").strip().lower()
    if raw == "s":
        return None
    try:
        return opts[int(raw) - 1][1]
    except (ValueError, IndexError):
        typer.echo("    invalid; skipping")
        return None


def _save_allowlist(doc: tomlkit.TOMLDocument, key: str, targets: list[Path]) -> list[Path]:
    if "paths" not in doc:
        doc["paths"] = tomlkit.table()
    if key not in doc["paths"]:
        doc["paths"][key] = tomlkit.array()
    arr = doc["paths"][key]
    existing = {expand(str(x)) for x in arr}
    added: list[Path] = []
    for t in targets:
        if t in existing:
            continue
        arr.append(str(t))
        existing.add(t)
        added.append(t)
    write_doc(doc)
    return added


# Whether to wrap the sandboxed command in strace so we can offer to add
# missing paths to the allowlist. None = use config / default (False).
# Overridden by the top-level --trace / --no-trace flags consumed in main().
_TRACE_OVERRIDE: Optional[bool] = None

# Whether to disable venv for the current invocation.
# Overridden by the top-level --no-venv flag consumed in main().
_VENV_DISABLE_OVERRIDE: Optional[bool] = None


def _trace_enabled() -> bool:
    if _TRACE_OVERRIDE is not None:
        return _TRACE_OVERRIDE
    val = read_doc().get("trace")
    return False if val is None else bool(val)


def _venv_disabled() -> bool:
    if _VENV_DISABLE_OVERRIDE is not None:
        return _VENV_DISABLE_OVERRIDE
    return False


def _consume_trace_flags(args: list[str]) -> tuple[list[str], Optional[bool]]:
    """Strip leading --trace / --no-trace tokens. Returns (rest, override)."""
    override: Optional[bool] = None
    while args:
        if args[0] == "--trace":
            override = True
            args = args[1:]
        elif args[0] == "--no-trace":
            override = False
            args = args[1:]
        else:
            break
    return args, override


def _consume_venv_flags(args: list[str]) -> tuple[list[str], Optional[bool]]:
    """Strip leading --no-venv tokens. Returns (rest, override)."""
    override: Optional[bool] = None
    while args:
        if args[0] == "--no-venv":
            override = True
            args = args[1:]
        else:
            break
    return args, override





def run_under_sandbox(cmd: list[str]) -> int:
    """Run a bwrap command, wrapping in strace if --trace was passed. Always
    offer to add any host paths the command tried to reach but couldn't."""
    if not _trace_enabled():
        return _run_passthrough(cmd)
    strace = shutil.which("strace")
    if strace is None:
        typer.echo("warning: strace not found; running without trace", err=True)
        return _run_passthrough(cmd)
    with tempfile.NamedTemporaryFile(prefix="botbox-trace-", suffix=".log", delete=False) as tf:
        trace_file = Path(tf.name)
    try:
        wrapped = [
            strace, "-f", "-qq",
            "-e", "trace=openat,open,stat,lstat,newfstatat,access,faccessat,readlink,readlinkat,execve",
            "--status=failed",
            "--signal=none",
            "-o", str(trace_file),
            "--", *cmd,
        ]
        rc = _run_passthrough(wrapped)
        _offer_autofix(trace_file)
        return rc
    finally:
        trace_file.unlink(missing_ok=True)


def _run_passthrough(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130


def _offer_autofix(trace_file: Path) -> None:
    paths = _parse_strace(trace_file)
    doc = read_doc()
    home = Path.home()
    venv = load_venv(doc)
    loaded = load_paths(doc)
    extras = [p for p, _, _ in loaded] + [d for _, _, d in loaded if d is not None]
    candidates = sorted(
        (p for p in paths
         if not _is_default_covered(p, home, venv, extras) and p.exists()),
        key=lambda x: x.parts,
    )
    if not candidates:
        return

    typer.echo(
        f"\ntrace: {len(candidates)} host path(s) the command tried to open "
        f"were not accessible inside the sandbox:",
        err=True,
    )
    for p in candidates:
        typer.echo(f"  {p}", err=True)

    raw = typer.prompt("\n[a]dd all / [r]eview each / [n]o", default="n").strip().lower()
    choice = raw[:1] if raw else "n"
    if choice == "n":
        return

    if choice == "r":
        chosen: list[Path] = []
        for p in candidates:
            t = _prompt_target(p)
            if t is not None:
                chosen.append(t)
        targets = _dedupe_targets(chosen)
    else:
        targets = candidates

    if not targets:
        return
    added = _save_allowlist(doc, "ro", targets)
    for t in added:
        typer.echo(f"added paths.ro: {t}")
    if added:
        typer.echo("re-run the command to pick up the new mounts.")


@app.command()
def venv(path: Optional[Path] = typer.Argument(None, help="Venv to use (omit to print current).")) -> None:
    """Set or show the default Python venv (bound read-only)."""
    doc = read_doc()
    if path is None:
        v = load_venv(doc)
        typer.echo(str(v) if v else "(unset)")
        return
    p = path.expanduser().resolve()
    if "python" not in doc:
        doc["python"] = tomlkit.table()
    doc["python"]["venv"] = str(p)
    write_doc(doc)
    typer.echo(f"venv set: {p}")


@app.command("env")
def env_cmd() -> None:
    """List env variables set inside the sandbox (values are redacted)."""
    env = load_env(read_doc())
    if not env:
        typer.echo("(no env set; set one with: botbox env-set KEY value)")
        return
    for k, v in env.items():
        typer.echo(f"{k}={redact_env_value(v)}")


@app.command("env-set")
def env_set(
    key: str = typer.Argument(help="Env variable name."),
    value: str = typer.Argument(help="Value. Starting with $ reads the host env var at launch."),
) -> None:
    """Set (or overwrite) an env variable for the sandbox ([env] table)."""
    doc = read_doc()
    if "env" not in doc:
        doc["env"] = tomlkit.table()
    existed = key in doc["env"]
    doc["env"][key] = value
    write_doc(doc)
    verb = "updated" if existed else "set"
    typer.echo(f"{verb} env.{key}={value}")
    if value.startswith("$"):
        m = _HOST_VAR_RE.match(value)
        name = m.group(1) if m and m.group(1) is not None else (m.group(3) if m else None)
        if name and name not in os.environ:
            typer.echo(f"warning: '{name}' is not currently set on the host; it will be skipped", err=True)


@app.command("env-unset")
def env_unset(key: str = typer.Argument(help="Env variable name.")) -> None:
    """Remove an env variable from the sandbox config."""
    doc = read_doc()
    env = doc.get("env")
    if not env or key not in env:
        typer.echo(f"not set: env.{key}")
        return
    del env[key]
    write_doc(doc)
    typer.echo(f"unset env.{key}")


@app.command("print", context_settings=PASSTHROUGH)
def print_cmd(
    ctx: typer.Context,
    agent: Optional[str] = typer.Argument(None, help="Agent name (default: default_agent)."),
) -> None:
    """Print the bwrap command for an agent instead of executing it."""
    doc = read_doc()
    agents = load_agents(doc)
    name = agent or default_agent(doc)
    if not name:
        die("error: no agent specified and default_agent not set")
    if name not in agents:
        die(f"error: unknown agent '{name}'; known: {', '.join(agents) or '(none)'}")
    cfg = agents[name]
    _, cmd = build_bwrap_cmd(cfg["command"], cfg["args"] + list(ctx.args), disable_venv=_venv_disabled())
    typer.echo(" \\\n  ".join(shlex.quote(a) for a in cmd))


def _register_agents(app: typer.Typer) -> None:
    """Read config and register a subcommand for each [agents.<name>]."""
    try:
        doc = read_doc()
    except Exception as e:
        typer.echo(f"warning: could not parse {CONFIG_FILE}: {e}", err=True)
        return
    for name, cfg in load_agents(doc).items():
        command = cfg["command"]
        default_args = cfg["args"]

        def make_handler(command: str, default_args: list[str]):
            def handler(ctx: typer.Context) -> None:
                _, cmd = build_bwrap_cmd(command, default_args + list(ctx.args), disable_venv=_venv_disabled())
                raise typer.Exit(run_under_sandbox(cmd))
            args_help = " ".join(default_args) if default_args else "(none)"
            handler.__doc__ = f"Run `{command}` in the sandbox. Default args: {args_help}."
            return handler

        app.command(name=name, context_settings=PASSTHROUGH)(make_handler(command, default_args))


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    doc = read_doc()
    name = default_agent(doc)
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    agents = load_agents(doc)
    if name not in agents:
        die(f"error: default_agent '{name}' has no [agents.{name}] table")
    cfg = agents[name]
    _, cmd = build_bwrap_cmd(cfg["command"], cfg["args"], disable_venv=_venv_disabled())
    raise typer.Exit(run_under_sandbox(cmd))


_register_agents(app)


def main() -> None:
    global _TRACE_OVERRIDE, _VENV_DISABLE_OVERRIDE
    args, override = _consume_trace_flags(sys.argv[1:])
    if override is not None:
        _TRACE_OVERRIDE = override
    args, override = _consume_venv_flags(args)
    if override is not None:
        _VENV_DISABLE_OVERRIDE = override
    sys.argv = [sys.argv[0]] + args
    if args and not args[0].startswith("-"):
        click_cmd = typer.main.get_command(app)
        if args[0] not in click_cmd.commands:
            _, cmd = build_bwrap_cmd(args[0], args[1:], disable_venv=_venv_disabled())
            sys.exit(run_under_sandbox(cmd))
    app()


if __name__ == "__main__":
    main()
