# botbox

Run coding agents (Claude Code and friends) inside a [bubblewrap](https://github.com/containers/bubblewrap)
sandbox so they only see the directories you've opted in to.

Running an agent with `--dangerously-skip-permissions` is convenient but the
agent then has read access to your entire home directory. `botbox` wraps the
agent in a sandbox that exposes only:

- the repos you've added to the allowlist,
- the current working directory (always rw, for this invocation only),
- a configured Python venv (read-only),
- your git config,
- env vars from the `[env]` config table,
- system libraries and the bits of `/etc` needed for DNS and TLS.

The bubblewrap approach is adapted from
[Patrick McCanna's writeup](https://patrickmccanna.net/a-detailed-writeup-of-claude-code-constrained-by-bubblewrap/).
Differences: TOML config so multiple repos and agents can be opted in once;
an ephemeral rw bind for the current working directory; per-agent default args.

## Requirements

- Linux with `bubblewrap` installed (`pacman -S bubblewrap`, `apt install bubblewrap`, …)
- Python 3.11+
- The agent binary on `PATH` (e.g. `claude`)

## Install

```sh
pip install botbox
```

## First run

On first invocation, `~/.config/botbox/config.toml` is seeded from the
template shipped with the package. Edit it to taste:

```toml
default_agent = "claude"

[python]
venv = "~/repos/venv"

[env]
# EDITOR = "vim"
# GITHUB_TOKEN = "$GITHUB_TOKEN"   # reads the host's GITHUB_TOKEN at launch

[paths]
rw = ["~/repos/circuitpython"]
ro = []

[agents.claude]
command = "claude"
args = ["--dangerously-skip-permissions"]
```

Each `[agents.<name>]` table becomes a subcommand. `command` is the binary
to exec; `args` is prepended to anything you pass on the CLI.

`botbox add PATH --dest DEST` mounts `PATH` at `DEST` inside the sandbox
instead of at its own location. This is how you give a sandboxed agent its
own state, e.g. per-agent Claude Code login/session state:

```toml
[paths]
rw = [
  { source = "~/.local/share/botbox/claude/.claude", dest = "~/.claude" },
  { source = "~/.local/share/botbox/claude/.claude.json", dest = "~/.claude.json" },
]
```

The `dest` location must not otherwise exist in the sandbox (it's overlaying
the real `~/.claude` here, which is not bound by default). Create the source
dirs and a stub `~/.claude.json` (`echo '{}' > ...`) before the first run;
you'll need to log in once inside the sandbox.

## Usage

```
botbox                  # run the default agent
botbox claude           # run a specific agent
botbox claude --resume  # extra args are forwarded after the configured ones
botbox bash             # any unknown name is forwarded to bwrap as-is
botbox list             # show config
botbox add              # add cwd to paths.rw
botbox add PATH...      # add one or more paths to paths.rw
botbox add --ro PATH... # add paths read-only
botbox add PATH --dest DEST # mount PATH at a different sandbox location
botbox venv ~/repos/v   # set the default Python venv
botbox env              # list env vars set inside the sandbox
botbox env-set K V      # set an env var (a $VAR value reads the host var at launch)
botbox env-unset K      # remove an env var
botbox rm PATH...       # remove paths from the allowlist (both ro and rw)
botbox rm --missing     # remove allowlist entries that no longer exist on the host
botbox print claude     # print the bwrap command instead of executing it
botbox --trace claude   # wrap in strace and prompt to allowlist missing paths
```

The current working directory is always bound rw for the invocation,
regardless of whether it's in the allowlist — list paths there only when
you want them visible from somewhere else (e.g. a sibling library edited
alongside the main repo).

### Allowlisting with --trace

Pass `--trace` *before* the subcommand to wrap the invocation in
`strace -e trace=openat,execve --status=failed`:

```
botbox --trace claude
botbox --trace bash
```

After the command exits, botbox parses the trace for paths the command
tried to open but couldn't reach inside the sandbox, drops anything already
covered, and prompts — regardless of whether the command succeeded:

```
trace: 5 host path(s) the command tried to open were not accessible inside the sandbox:
  /opt/claude-code/bin/claude
  /opt/claude-code/cli.js
  /opt/claude-code/sdk.mjs
  ...
[a]dd all / [r]eview each / [n]o (n):
```

`a` adds each listed path as its own `paths.ro` entry (no rollup). `r` walks
each file and lets you pick file / parent dir / package root (`/opt/<x>`,
`/srv/<x>`, `/var/lib/<x>`) individually if you'd rather widen the bind.

Strace adds ~no measurable overhead to interactive agents (most time is
spent waiting on I/O), so you can set `trace = true` in the config to
enable on every invocation; `--no-trace` then disables for a single run.

## What's inside the sandbox

Read-only:

- `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`
- `/etc/resolv.conf`, `/etc/hosts`, `/etc/ssl`, `/etc/ca-certificates`,
  `/etc/passwd`, `/etc/group`, `/etc/nsswitch.conf`, `/etc/localtime`
- `~/.gitconfig`, `~/.config/git`
- `~/.ssh/known_hosts`
- `~/.local`, `~/.nvm` (if present)
- the configured `[python] venv`
- entries under `[paths] ro`

Read-write:

- `~/.npm` (package cache)
- entries under `[paths] rw`
- the current working directory

Kernel filesystems are namespaced: `/proc` (with a new PID namespace),
`/dev`, and a fresh `tmpfs` at `/tmp`. Networking is shared with the host.

Env variables from `[env]` are applied with `--setenv` after the built-ins
(`HOME`, `USER`, `PATH`, `TERM`, `LANG`, `VIRTUAL_ENV`), so
they can override them. A value starting with `$` (e.g.
`GITHUB_TOKEN = "$GITHUB_TOKEN"`) is read from the host environment when the
sandbox starts and skipped with a warning if unset on the host; a leading `~`
is expanded.

## License

MIT
