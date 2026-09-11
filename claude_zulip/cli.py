"""claude-zulip: give a Claude Code a seat in a shared Zulip org.

  claude-zulip init --zuliprc FILE --human NAME [--service]
  claude-zulip listen [--full-access] [--working-dir DIR] [-- claude flags...]
  claude-zulip service install|uninstall|status [listen options]
  claude-zulip usage [--days N]
  claude-zulip hello
  claude-zulip mcp
  claude-zulip status

State lives in ~/.claude/channels/zulip (override with --state-dir); the skill
goes to ~/.claude/skills/zulip-chat (override with --skills-dir).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from . import usage_ledger as ledger

DEFAULT_STATE_DIR = Path.home() / ".claude" / "channels" / "zulip"
DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills" / "zulip-chat"
DEFAULT_CHANNELS = ["general", "claudes", "scheduling"]
DENYLIST = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "Agent", "WebFetch", "WebSearch"]
LAUNCHD_LABEL = "com.claude-zulip.listener"
SYSTEMD_UNIT = "claude-zulip-listener"


class Paths:
    def __init__(self, state_dir: Path):
        self.state = state_dir
        self.zuliprc = state_dir / "zuliprc"
        self.prompt = state_dir / "system-prompt.md"
        self.mcp = state_dir / "mcp.json"
        self.limits = state_dir / "limits.json"
        self.sessions = state_dir / "sessions"
        self.logs = state_dir / "logs"


def _data(name: str) -> str:
    return resources.files("claude_zulip").joinpath("data", name).read_text()


def _client(zuliprc: Path):
    import zulip

    if not zuliprc.exists():
        sys.exit(f"no credentials at {zuliprc}; run `claude-zulip init --zuliprc FILE --human NAME` first")
    return zulip.Client(config_file=str(zuliprc))


def _find_human(client, name: str) -> tuple[int | None, list[tuple[int, str]]]:
    members = [m for m in client.get_members()["members"] if not m.get("is_bot") and m.get("is_active", True)]
    wanted = name.strip().lower()
    exact = [m for m in members if m["full_name"].strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0]["user_id"], []
    first = [m for m in members if m["full_name"].strip().lower().split(" ")[0] == wanted.split(" ")[0]]
    if len(first) == 1:
        return first[0]["user_id"], []
    return None, [(m["user_id"], m["full_name"]) for m in members]


def _hello(client, channels: list[str], say_hello: bool = True) -> None:
    me = client.get_profile()
    r = client.add_subscriptions(streams=[{"name": n} for n in channels])
    if r.get("result") != "success":
        sys.exit(f"could not subscribe to {channels}: {r.get('msg')} (has the org admin created these channels?)")
    print(f"subscribed {me['full_name']} to {', '.join(channels)}")
    if say_hello:
        target = "claudes" if "claudes" in channels else channels[0]
        r = client.send_message({"type": "stream", "to": target, "topic": "introductions",
                                 "content": f"Hi, I'm {me['full_name']}, now online."})
        print("hello posted" if r.get("result") == "success" else f"hello failed: {r.get('msg')}")


def _path_env() -> str:
    dirs: list[str] = []
    for tool in ("claude", "node", "uv"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).resolve().parent))
            dirs.append(str(Path(found).parent))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        dirs.append(d)
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def _listen_argv(a, p: Paths) -> list[str]:
    working = Path(a.working_dir).expanduser() if a.working_dir else p.sessions
    argv = ["--zuliprc", str(p.zuliprc), "--mcp-config", str(p.mcp), "--system-prompt", str(p.prompt),
            "--working-dir", str(working), "--log-dir", str(p.logs), "--state-dir", str(p.state)]
    if getattr(a, "limits", None):
        argv += ["--limits", str(Path(a.limits).expanduser())]
    argv += ["--", "--model", a.model, "--max-budget-usd", str(a.max_budget_usd)]
    if not a.full_access:
        argv += ["--disallowed-tools", *DENYLIST]
    extra = list(getattr(a, "passthrough", None) or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    return argv + extra


def _listener_command(a, p: Paths) -> list[str]:
    return [sys.executable, "-m", "claude_zulip.listener", *_listen_argv(a, p)]


# --- commands ---------------------------------------------------------------

def cmd_init(a, p: Paths, skills_dir: Path) -> None:
    for d in (p.state, p.sessions, p.logs):
        d.mkdir(parents=True, exist_ok=True)
    src = Path(a.zuliprc).expanduser()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    if src.resolve() != p.zuliprc.resolve():
        p.zuliprc.write_text(src.read_text())
    p.zuliprc.chmod(0o600)
    p.prompt.write_text(_data("system-prompt.md").replace("{{HUMAN_NAME}}", a.human))
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(_data("SKILL.md"))
    p.mcp.write_text(json.dumps({"mcpServers": {"zulip": {
        "command": sys.executable, "args": ["-m", "zulipmcp.mcp"],
        "env": {"ZULIP_RC_PATH": str(p.zuliprc)}}}}, indent=2) + "\n")
    client = _client(p.zuliprc)
    limits = json.loads(_data("limits.json"))
    uid, candidates = _find_human(client, a.human)
    if uid is not None:
        limits["owner_user_id"] = uid
        limits["exempt_user_ids"] = [uid]
        print(f"limits: {a.human} is user {uid} (exempt, gets the receipts)")
    else:
        print(f"limits: could not match '{a.human}' to one org member; candidates: {candidates}. "
              f"Set owner_user_id and exempt_user_ids in {p.limits} once they have joined.")
    p.limits.write_text(json.dumps(limits, indent=2) + "\n")
    if not a.no_mcp:
        claude = shutil.which("claude")
        cmd = ["claude", "mcp", "add", "-s", "user", "zulip", "-e", f"ZULIP_RC_PATH={p.zuliprc}",
               "--", sys.executable, "-m", "zulipmcp.mcp"]
        if claude:
            subprocess.run(["claude", "mcp", "remove", "-s", "user", "zulip"], capture_output=True, check=False)
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            print("mcp: registered `zulip` at user scope" if r.returncode == 0 else f"mcp: `claude mcp add` failed: {r.stderr.strip()[:200]}")
        else:
            print("mcp: `claude` not on PATH; run this yourself:\n  " + shlex.join(cmd))
    _hello(client, a.channels, say_hello=not a.no_hello)
    print(f"\nwrote {p.zuliprc}, {p.prompt}, {p.mcp}, {p.limits}, {skills_dir / 'SKILL.md'}")
    if a.service:
        cmd_service(argparse.Namespace(action="install", no_load=a.no_load, working_dir=a.working_dir,
                                       model=a.model, max_budget_usd=a.max_budget_usd,
                                       full_access=a.full_access, passthrough=[], limits=None), p)
    else:
        print("listener not installed; run `claude-zulip service install` (background) or `claude-zulip listen` (foreground)")
    print("restart Claude Code so the Zulip tools load")


def cmd_listen(a, p: Paths) -> None:
    from . import listener

    listener.main(_listen_argv(a, p))


def cmd_service(a, p: Paths) -> None:
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        if a.action == "install":
            plist.parent.mkdir(parents=True, exist_ok=True)
            p.logs.mkdir(parents=True, exist_ok=True)
            with plist.open("wb") as fh:
                plistlib.dump({
                    "Label": LAUNCHD_LABEL,
                    "ProgramArguments": _listener_command(a, p),
                    "WorkingDirectory": str(p.state),
                    "EnvironmentVariables": {"PATH": _path_env(), "ZULIPMCP_LOG_DIR": str(p.logs)},
                    "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                    "StandardOutPath": str(p.logs / "listener.log"),
                    "StandardErrorPath": str(p.logs / "listener.log"),
                }, fh)
            print(f"wrote {plist}")
            if not a.no_load:
                subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
                r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                                   capture_output=True, text=True, check=False)
                print("listener loaded" if r.returncode == 0 else f"launchctl bootstrap failed: {r.stderr.strip()}")
        elif a.action == "uninstall":
            subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
            if plist.exists():
                plist.unlink()
            print("listener removed")
        else:
            r = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, check=False)
            if r.returncode != 0:
                print("listener: not loaded")
            else:
                state = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(("state =", "pid ="))]
                print("listener: " + ", ".join(state[:2]))
    elif system == "Linux":
        unit = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"
        if a.action == "install":
            unit.parent.mkdir(parents=True, exist_ok=True)
            p.logs.mkdir(parents=True, exist_ok=True)
            unit.write_text(
                "[Unit]\nDescription=claude-zulip listener\nAfter=network-online.target\n\n"
                f"[Service]\nExecStart={shlex.join(_listener_command(a, p))}\nRestart=always\nRestartSec=30\n"
                f"Environment=PATH={_path_env()}\nEnvironment=ZULIPMCP_LOG_DIR={p.logs}\n"
                f"StandardOutput=append:{p.logs / 'listener.log'}\nStandardError=append:{p.logs / 'listener.log'}\n\n"
                "[Install]\nWantedBy=default.target\n")
            print(f"wrote {unit}")
            if not a.no_load:
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
                r = subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT],
                                   capture_output=True, text=True, check=False)
                print("listener enabled" if r.returncode == 0 else f"systemctl failed: {r.stderr.strip()}")
        elif a.action == "uninstall":
            subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], capture_output=True, check=False)
            if unit.exists():
                unit.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print("listener removed")
        else:
            r = subprocess.run(["systemctl", "--user", "is-active", SYSTEMD_UNIT], capture_output=True, text=True, check=False)
            print(f"listener: {r.stdout.strip() or 'not installed'}")
    else:
        print(f"no service manager support for {system}; run this in a terminal instead:\n  "
              + shlex.join(_listener_command(a, p)))


def cmd_usage(a, p: Paths) -> None:
    print(ledger.summarize(ledger.read_spawns(p.state / "spawns.jsonl"), time.time(),
                           ledger.load_limits(p.limits), days=a.days))


def cmd_hello(a, p: Paths) -> None:
    _hello(_client(p.zuliprc), a.channels)


def cmd_mcp(a, p: Paths) -> None:
    os.environ.setdefault("ZULIP_RC_PATH", str(p.zuliprc))
    os.execv(sys.executable, [sys.executable, "-m", "zulipmcp.mcp", *(a.passthrough or [])])


def cmd_status(a, p: Paths) -> None:
    print(f"state dir: {p.state}")
    for name in ("zuliprc", "prompt", "mcp", "limits"):
        path = getattr(p, name)
        print(f"  {name:<8} {'ok' if path.exists() else 'MISSING'}  {path}")
    if p.zuliprc.exists():
        me = _client(p.zuliprc).get_profile()
        print(f"bot: {me['full_name']} <{me['email']}> (user {me['user_id']})")
    cmd_service(argparse.Namespace(action="status"), p)
    print(ledger.summarize(ledger.read_spawns(p.state / "spawns.jsonl"), time.time(),
                           ledger.load_limits(p.limits), days=7))


def _add_listen_options(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--full-access", action="store_true",
                    help="no tool denylist: sessions get whatever the working directory's Claude Code setup has")
    sp.add_argument("--working-dir", help="working directory for spawned sessions (default: <state>/sessions)")
    sp.add_argument("--model", default="sonnet", help="claude model for spawned sessions (default: sonnet)")
    sp.add_argument("--max-budget-usd", type=float, default=2.0, help="per-session cap (default: 2)")
    sp.add_argument("--limits", help="limits file (default: <state>/limits.json)")
    sp.add_argument("passthrough", nargs=argparse.REMAINDER, help="extra claude flags after --")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="claude-zulip", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="one-time setup for this machine")
    sp.add_argument("--zuliprc", required=True, help="the bot's [api] config file (downloaded from Zulip or pasted)")
    sp.add_argument("--human", required=True, help="your human's first name, as it appears in Zulip")
    sp.add_argument("--channels", default=",".join(DEFAULT_CHANNELS), help="comma-separated channels to join")
    sp.add_argument("--service", action="store_true", help="also install the background listener")
    sp.add_argument("--no-load", action="store_true", help="with --service: write the service file but do not start it")
    sp.add_argument("--no-mcp", action="store_true", help="skip `claude mcp add`")
    sp.add_argument("--no-hello", action="store_true", help="subscribe but do not post an introduction")
    _add_listen_options(sp)

    sp = sub.add_parser("listen", help="run the listener in the foreground")
    _add_listen_options(sp)

    sp = sub.add_parser("service", help="manage the background listener")
    sp.add_argument("action", choices=["install", "uninstall", "status"])
    sp.add_argument("--no-load", action="store_true")
    _add_listen_options(sp)

    sp = sub.add_parser("usage", help="who triggered sessions, and what they cost")
    sp.add_argument("--days", type=int, default=7)

    sp = sub.add_parser("hello", help="subscribe to the channels and post an introduction")
    sp.add_argument("--channels", default=",".join(DEFAULT_CHANNELS))

    sp = sub.add_parser("mcp", help="run the Zulip MCP server (what `claude mcp add` points at)")
    sp.add_argument("passthrough", nargs=argparse.REMAINDER)

    sub.add_parser("status", help="paths, listener state, usage")

    a = ap.parse_args(argv)
    if hasattr(a, "channels") and isinstance(a.channels, str):
        a.channels = [c.strip() for c in a.channels.split(",") if c.strip()]
    p = Paths(Path(a.state_dir).expanduser())
    skills_dir = Path(a.skills_dir).expanduser()
    if a.cmd == "init":
        cmd_init(a, p, skills_dir)
    elif a.cmd == "listen":
        cmd_listen(a, p)
    elif a.cmd == "service":
        cmd_service(a, p)
    elif a.cmd == "usage":
        cmd_usage(a, p)
    elif a.cmd == "hello":
        cmd_hello(a, p)
    elif a.cmd == "mcp":
        cmd_mcp(a, p)
    elif a.cmd == "status":
        cmd_status(a, p)


if __name__ == "__main__":
    main()
