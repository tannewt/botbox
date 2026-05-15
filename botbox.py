"""Run agents (claude, etc.) inside a bubblewrap sandbox with an opt-in allowlist.

Config lives at ~/.config/botbox/config.toml. Each agents.<name> table becomes
a subcommand (e.g. `botbox claude`) that runs the configured command with its
default args, plus any extra args passed on the CLI.

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
import shlex
import shutil
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


def load_paths(doc) -> list[tuple[Path, bool]]:
    paths = doc.get("paths") or {}
    out: list[tuple[Path, bool]] = []
    for p in paths.get("rw") or []:
        out.append((expand(str(p)), False))
    for p in paths.get("ro") or []:
        out.append((expand(str(p)), True))
    return out


def load_venv(doc) -> Optional[Path]:
    v = (doc.get("python") or {}).get("venv")
    return expand(str(v)) if v else None


def load_agents(doc) -> dict[str, dict]:
    agents = doc.get("agents") or {}
    return {
        str(name): {
            "command": str(cfg.get("command", name)),
            "args": [str(a) for a in (cfg.get("args") or [])],
        }
        for name, cfg in agents.items()
    }


def default_agent(doc) -> Optional[str]:
    v = doc.get("default_agent")
    return str(v) if v else None


def die(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(1)


def build_bwrap_cmd(agent_command: str, agent_args: list[str]) -> tuple[str, list[str]]:
    home = Path.home()
    user = os.environ.get("USER", "")
    ssh_sock = os.environ.get("SSH_AUTH_SOCK", "")
    pwd = Path.cwd().resolve()

    doc = read_doc()
    entries = load_paths(doc)
    venv = load_venv(doc)

    # cwd is always bound rw for the current invocation. If it's not already
    # covered by a configured rw path, add an ephemeral entry (not persisted).
    rw_paths = [p for p, ro in entries if not ro]
    if not any(pwd == p or p in pwd.parents for p in rw_paths):
        entries.append((pwd, False))

    bwrap = shutil.which("bwrap") or die("error: bwrap not found; install bubblewrap")
    agent_bin = shutil.which(agent_command) or die(f"error: '{agent_command}' not found on PATH")

    cmd = [bwrap]

    for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(d).exists():
            cmd += ["--ro-bind", d, d]

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
    if ssh_sock:
        sock_dir = str(Path(ssh_sock).parent)
        cmd += ["--bind", sock_dir, sock_dir]

    # if (home / ".nvm").is_dir():
    #     cmd += ["--ro-bind", str(home / ".nvm"), str(home / ".nvm")]

    if (home / ".local").is_dir():
        cmd += ["--ro-bind", str(home / ".local"), str(home / ".local")]

    # if (home / ".npm").is_dir():
    #     cmd += ["--bind", str(home / ".npm"), str(home / ".npm")]

    for p in (home / ".claude", home / ".claude.json"):
        if p.exists():
            cmd += ["--bind", str(p), str(p)]

    if venv:
        if not venv.exists():
            typer.echo(f"warning: venv {venv} missing", err=True)
        elif venv not in [e[0] for e in entries]:
            cmd += ["--ro-bind", str(venv), str(venv)]

    for p, ro in entries:
        if not p.exists():
            typer.echo(f"warning: {p} missing; skipping", err=True)
            continue
        cmd += ["--ro-bind" if ro else "--bind", str(p), str(p)]

    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    if venv and venv.exists():
        path_env = f"{venv}/bin:{path_env}"

    cmd += [
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--setenv", "HOME", str(home),
        "--setenv", "USER", user,
        "--setenv", "PATH", path_env,
        "--setenv", "TERM", os.environ.get("TERM", "xterm-256color"),
        "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"),
    ]
    if venv and venv.exists():
        cmd += ["--setenv", "VIRTUAL_ENV", str(venv)]
    if ssh_sock:
        cmd += ["--setenv", "SSH_AUTH_SOCK", ssh_sock]

    cmd += [
        "--share-net",
        "--unshare-pid",
        "--die-with-parent",
        "--chdir", str(pwd),
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

    if venv:
        missing = "  [MISSING]" if not venv.exists() else ""
        typer.echo(f"venv  {venv}{missing}")
    for p, ro in entries:
        marker = "ro " if ro else "rw "
        missing = "  [MISSING]" if not p.exists() else ""
        typer.echo(f"{marker}  {p}{missing}")
    if agents:
        typer.echo("agents:")
        for name, cfg in agents.items():
            mark = " (default)" if name == default else ""
            argstr = " " + " ".join(shlex.quote(a) for a in cfg["args"]) if cfg["args"] else ""
            typer.echo(f"  {name}{mark}: {cfg['command']}{argstr}")
    if not venv and not entries and not agents:
        typer.echo(f"(empty; edit {CONFIG_FILE} or run: botbox add)")


@app.command()
def add(
    path: Optional[Path] = typer.Argument(None, help="Path to add (default: cwd)."),
    ro: bool = typer.Option(False, "--ro", help="Bind read-only."),
) -> None:
    """Add a path to the allowlist."""
    p = (path or Path.cwd()).resolve()
    doc = read_doc()
    if "paths" not in doc:
        doc["paths"] = tomlkit.table()
    key = "ro" if ro else "rw"
    if key not in doc["paths"]:
        doc["paths"][key] = tomlkit.array()
    arr = doc["paths"][key]
    if p in [expand(str(x)) for x in arr]:
        typer.echo(f"already present in paths.{key}: {p}")
        return
    arr.append(str(p))
    write_doc(doc)
    typer.echo(f"added paths.{key}: {p}")


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
    _, cmd = build_bwrap_cmd(cfg["command"], cfg["args"] + list(ctx.args))
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
                bwrap, cmd = build_bwrap_cmd(command, default_args + list(ctx.args))
                os.execvp(bwrap, cmd)
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
    bwrap, cmd = build_bwrap_cmd(cfg["command"], cfg["args"])
    os.execvp(bwrap, cmd)


_register_agents(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
