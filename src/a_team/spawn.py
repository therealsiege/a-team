"""Spawn terminal windows running Claude Code.

Supports two terminal backends, selectable via `a-team config terminal`:

  **Ghostty** (default): opens a new window via the File > New Window menu
  (AppleScript / System Events), then delivers the launch command by
  clipboard paste (Cmd-V). Pasting is atomic — no dropped characters.

  **iTerm**: uses iTerm's native scripting model — `create window with
  default profile` / `create tab with default profile` and `write text` to
  deliver commands directly. No clipboard touching required.

Title persistence: claude emits its own OSC-0 title sequences, which
would clobber a one-time printf. A backgrounded loop re-emits the agent
name every second; a trap kills it when claude exits or the window closes.
"""

import os
import shlex
import subprocess
import time

from . import harness as _harness
from . import hosts as _hosts


_APPLESCRIPT = r'''
tell application "Ghostty" to activate
tell application "System Events"
    tell process "Ghostty"
        set n0 to count of windows
        click menu item "New Window" of menu "File" of menu bar 1
        -- Wait for the new window to actually appear (up to ~4s) rather than a
        -- blind delay. Window-open latency varies with system load and OS/Ghostty
        -- version; a too-short fixed delay pastes into a window that isn't ready
        -- yet, so nothing runs (the failure this fixes).
        repeat 40 times
            if (count of windows) > n0 then exit repeat
            delay 0.1
        end repeat
    end tell
end tell
delay 0.4
tell application "System Events"
    -- Clear anything the restored shell may have buffered at the prompt, then
    -- paste the launch command (atomic — no dropped chars) and submit it.
    keystroke "u" using control down
    delay 0.1
    keystroke "v" using command down
    delay 0.3
    key code 36
end tell
'''


_APPLESCRIPT_TAB = r"""
tell application "Ghostty" to activate
tell application "System Events"
    tell process "Ghostty"
        -- A tab needs a window to live in. With none open, "New Tab" is either
        -- disabled or makes a window anyway depending on version, so be explicit.
        if (count of windows) is 0 then
            click menu item "New Window" of menu "File" of menu bar 1
            repeat 40 times
                if (count of windows) > 0 then exit repeat
                delay 0.1
            end repeat
        else
            set t0 to 0
            try
                set t0 to count of (tabs of window 1)
            end try
            click menu item "New Tab" of menu "File" of menu bar 1
            -- Wait for the tab to exist rather than guessing at a delay. Some
            -- Ghostty builds do not expose `tabs` to System Events at all; fall
            -- back to a bounded wait rather than failing the open.
            repeat 40 times
                try
                    if (count of (tabs of window 1)) > t0 then exit repeat
                on error
                    delay 0.4
                    exit repeat
                end try
                delay 0.1
            end repeat
        end if
    end tell
end tell
delay 0.4
tell application "System Events"
    keystroke "u" using control down
    delay 0.1
    keystroke "v" using command down
    delay 0.3
    key code 36
end tell
"""


# ---------------------------------------------------------------------------
# iTerm AppleScript templates — `write text` delivers the command directly,
# so no clipboard save/restore is needed.
# ---------------------------------------------------------------------------

_ITERM_APPLESCRIPT = '''\
tell application "iTerm" to activate
tell application "iTerm"
    create window with default profile
    tell current session of current window
        write text "<COMMAND>"
    end tell
end tell'''

_ITERM_APPLESCRIPT_TAB = '''\
tell application "iTerm" to activate
tell application "iTerm"
    if (count of windows) is 0 then
        create window with default profile
    else
        tell current window
            create tab with default profile
        end tell
    end if
    tell current session of current window
        write text "<COMMAND>"
    end tell
end tell'''


def _escape_for_applescript(s: str) -> str:
    r"""Escape a string for embedding in an AppleScript double-quoted literal.

    AppleScript string literals use ``"..."`` and recognise only ``\\`` and
    ``\"`` as escape sequences — everything else is literal.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _deliver_ghostty(command: str, *, tab: bool = False) -> None:
    """Deliver *command* via Ghostty's clipboard-paste approach."""
    try:
        prev = subprocess.run(["pbpaste"], capture_output=True).stdout
    except Exception:
        prev = b""
    subprocess.run(["pbcopy"], input=command.encode(), check=True)
    try:
        subprocess.run(["osascript", "-e",
                        _APPLESCRIPT_TAB if tab else _APPLESCRIPT], check=True)
    finally:
        try:
            subprocess.run(["pbcopy"], input=prev, check=False)
        except Exception:
            pass


def _deliver_iterm(command: str, *, tab: bool = False) -> None:
    """Deliver *command* via iTerm's ``write text`` — no clipboard needed."""
    template = _ITERM_APPLESCRIPT_TAB if tab else _ITERM_APPLESCRIPT
    script = template.replace("<COMMAND>", _escape_for_applescript(command))
    subprocess.run(["osascript", "-e", script], check=True)


def _deliver(command: str, *, tab: bool = False) -> None:
    """Open a terminal window/tab and run *command* in it."""
    from . import config as _config
    terminal = _config.get_terminal()
    if terminal == "iterm":
        _deliver_iterm(command, tab=tab)
    else:
        _deliver_ghostty(command, tab=tab)


def _validate(name: str, path: str) -> None:
    """Reject inputs that would break the launch command's quoting."""
    if any(ch in name for ch in ("'", "\n", "\r")):
        raise ValueError(f"agent name cannot contain single quotes or newlines: {name!r}")
    if any(ch in path for ch in ("'", "\n", "\r")):
        raise ValueError(f"agent path cannot contain single quotes or newlines: {path!r}")


def _build_command(
    display_name: str,
    path: str,
    harness: _harness.Harness,
    session_mode: str,
    config_dir: str | None = None,
    host: "_hosts.Host | None" = None,
    session: str | None = None,
) -> str:
    r"""The plain bash command pasted into the new window. Single backslashes
    (``\\e``, ``\\a`` in source -> literal ``\e``, ``\a``) so printf emits real escapes.

    The harness supplies both the launch verb (new/continue/resume) and the
    per-account config export — and it exports only its OWN config variable, so
    a Claude config dir can never leak into Codex."""
    seq = f"\\e]0;{display_name}\\a\\e]1;{display_name}\\a\\e]2;{display_name}\\a"
    h = host if host is not None else _hosts.LOCAL
    # Every agent starts with Remote Control on, named after itself. Two
    # reasons: the session is then reachable from the phone and by SendMessage
    # from other sessions, and it carries a real name instead of the
    # auto-generated `hostname-ancient-wirth` the runtime would pick.
    #
    # Local agents used to be skipped, on the grounds that you are sitting at
    # that machine anyway. That stopped being true once the phone became a real
    # surface: an M4 session cannot be named any other way. The flag is read
    # ONLY at a conversation's first registration and is ignored on every
    # resume, and `/rc` can only auto-generate, so a session that starts without
    # it can never be given a chosen name. See toolkit/TRAPS.md.
    rc_args = harness.remote_control_args(display_name)
    engine_cmd = harness.launch_command(session_mode, rc_args)
    # Auto-init project tooling (idempotent, no-ops if already set up)
    inits = (
        '{ command -v codegraph >/dev/null 2>&1 && [ ! -d .codegraph ] '
        '&& codegraph init; } 2>/dev/null; '
        'npx ruflo@latest init wizard 2>/dev/null; '
    )
    engine_cmd = inits + engine_cmd
    # Account config is a local-machine concept (CLAUDE_CONFIG_DIR). A remote
    # host runs under its OWN login, so we never export it across the SSH hop.
    env = harness.env_prefix(config_dir) if not h.is_remote else ""
    body = h.launch_command(path, engine_cmd, session or "agent")
    return (
        "{ "
        f"{env}"
        f"( while :; do printf '{seq}'; sleep 1; done ) & "
        "TPID=$!; "
        'trap "kill $TPID 2>/dev/null" EXIT INT TERM HUP; '
        f"{body}; "
        "}"
    )


def open_agent(
    name: str,
    path: str,
    *,
    session_mode: str = "continue",
    topic: str | None = None,
    config_dir: str | None = None,
    harness: str = _harness.DEFAULT_HARNESS,
    host: str = _hosts.DEFAULT_HOST,
    session: str | None = None,
) -> None:
    """Open a terminal window for the agent under the chosen harness.

    Opens a window in the configured terminal (Ghostty or iTerm), sets the
    title to `name` (kept set via a re-emit loop), cd's into `path`, and
    runs the harness. `session_mode` selects how it starts:
      - "continue": resume the most-recent session in this dir (fresh fallback)
      - "new":      a fresh session
      - "resume":   the harness's own past-session picker (fresh fallback)
    `topic` is an optional label appended to the window title. `config_dir`
    selects the account config home for harnesses that support it (Claude:
    CLAUDE_CONFIG_DIR); None = default/personal. `harness` is "claude" | "codex".

    Raises RuntimeError with an actionable message if the harness executable is
    not on PATH, so a missing `codex` reports setup instead of a mangled window.
    """
    h = _harness.get(harness)
    target = _hosts.get(host)
    if target.is_remote:
        # The harness runs on the REMOTE box, so a local `which` proves nothing.
        # What matters here is that we can reach the host at all.
        if not target.reachable():
            raise RuntimeError(
                f"{target.label} is not reachable (check `ssh {target.ssh_alias}` "
                f"and that Tailscale is up)."
            )
    elif not h.is_available():
        raise RuntimeError(
            f"{h.label} is not installed or not on PATH (need `{h.executable}`). "
            f"Install it or choose a different harness."
        )
    _validate(name, path)
    if topic:
        _validate(topic, path)
        display_name = f"{name}: {topic}"
    else:
        display_name = name
    command = _build_command(
        display_name, path, h, session_mode, config_dir, host=target, session=session
    )
    _deliver(command)


TMUX_BIN = "/opt/homebrew/bin/tmux"


def tmux_sessions(host: str = _hosts.DEFAULT_HOST) -> list[str] | None:
    """tmux session names on a host, or None if the host could not be reached.

    None is not []: "cannot tell" and "nothing running" lead to different
    actions, and collapsing them makes an unreachable host look idle.
    """
    target = _hosts.get(host)
    cmd = [TMUX_BIN, "ls", "-F", "#S"]
    if target.is_remote:
        # Every argument must survive a second shell on the far side. "#S"
        # unquoted starts a COMMENT there, so `tmux ls -F` ran with no format
        # and the whole call read as an unreachable host.
        cmd = ["ssh", "-o", "ConnectTimeout=8", target.ssh_alias,
               " ".join(shlex.quote(c) for c in cmd)]
    if not target.is_remote and not os.path.exists(TMUX_BIN):
        # No tmux on this machine at all. That is a definite "nothing is
        # running here", not a failure to find out, and returning None made a
        # laptop with no tmux look unreachable.
        return []
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return []
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        # tmux exits non-zero with "no server running" when nothing is up,
        # which IS an answer. An ssh failure is not.
        err = (r.stderr or "").lower()
        if "no server running" in err or "no such file" in err:
            return []
        return None
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def attach_window(session: str, host: str = _hosts.DEFAULT_HOST,
                  tab: bool = False) -> None:
    """Open a terminal window attached to an existing session on `host`.

    `tab` opens it as a tab in the frontmost window instead of its own window,
    which is how a row of Associates is normally arranged.

    Attach-or-create, not attach. `tmux attach -t` exits when the session is not
    there, which leaves a dead window with no error on screen: over mosh it keeps
    showing the last frame it received, so a frozen window reads as an idle one.
    So use the same attach-or-create the launch path uses.
    """
    target = _hosts.get(host)
    tmux_cmd = f"{TMUX_BIN} new-session -A -s {shlex.quote(session)}"
    if target.is_remote:
        command = "MOSH_TITLE_NOPREFIX=1; export MOSH_TITLE_NOPREFIX; " + \
            _hosts.reconnecting_attach(target.ssh_alias, tmux_cmd)
    else:
        command = tmux_cmd
    _deliver(command, tab=tab)


def open_all(agents: list[dict], delay_between: float = 1.0) -> None:
    """Open terminal windows for every agent, respecting each agent's saved
    harness AND resolved account config (both previously dropped here), with a
    small delay so the terminal settles between opens."""
    from . import config  # local import avoids a module-load cycle

    for agent in config._normalized(agents):
        harness_key = agent.get("harness", _harness.DEFAULT_HARNESS)
        open_agent(
            agent["name"],
            agent["path"],
            harness=harness_key,
            host=agent.get("host", _hosts.DEFAULT_HOST),
            session=agent.get("id"),
            config_dir=config.resolve_config_dir(agent, harness=harness_key),
        )
        time.sleep(delay_between)
