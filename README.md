# botbox

Run coding agents (Claude Code and friends) inside a [bubblewrap](https://github.com/containers/bubblewrap)
sandbox so they only see the directories you've opted in to.

Running an agent with `--dangerously-skip-permissions` is convenient but the
agent then has read access to your entire home directory. `botbox` wraps the
agent in a sandbox that exposes only:

- the repos you've added to the allowlist,
- the current working directory (always rw, for this invocation only),
- your `.claude` login/session state,
- a configured Python venv (read-only),
- your git config and the SSH agent socket (no private keys),
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

[paths]
rw = ["~/repos/circuitpython"]
ro = []

[agents.claude]
command = "claude"
args = ["--dangerously-skip-permissions"]
# state_dir = "host"   # uncomment to share the host's real ~/.claude
```

Each `[agents.<name>]` table becomes a subcommand. `command` is the binary
to exec; `args` is prepended to anything you pass on the CLI.

`state_dir` controls where the agent's `~/.claude` and `~/.claude.json` come
from inside the sandbox. By default (unset) it points at
`~/.local/share/botbox/<agent>` on the host, so sandboxed runs get their
own session/login state and don't read or modify your host Claude. Set it
to `"host"` to bind the host's real `~/.claude` instead, or to any path
for a custom location. The directory and a stub `.claude.json` are created
on first run; you'll need to log in once inside the sandbox.

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
botbox venv ~/repos/v   # set the default Python venv
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

- `~/.claude`, `~/.claude.json` (login + sessions)
- `~/.npm` (package cache)
- the SSH agent socket directory (signing only; no private key access)
- entries under `[paths] rw`
- the current working directory

Kernel filesystems are namespaced: `/proc` (with a new PID namespace),
`/dev`, and a fresh `tmpfs` at `/tmp`. Networking is shared with the host.

## License

MIT
