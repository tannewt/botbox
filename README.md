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
```

Each `[agents.<name>]` table becomes a subcommand. `command` is the binary
to exec; `args` is prepended to anything you pass on the CLI.

## Usage

```
botbox                  # run the default agent
botbox claude           # run a specific agent
botbox claude --resume  # extra args are forwarded after the configured ones
botbox list             # show config
botbox add              # add cwd to paths.rw
botbox add --ro PATH    # add PATH read-only
botbox venv ~/repos/v   # set the default Python venv
botbox print claude     # print the bwrap command instead of executing it
```

The current working directory is always bound rw for the invocation,
regardless of whether it's in the allowlist — list paths there only when
you want them visible from somewhere else (e.g. a sibling library edited
alongside the main repo).

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
