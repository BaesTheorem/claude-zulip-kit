"""Org-admin commands: set the org up, invite people, mint their bots, hand bots over.

Everything here runs with a human admin's credentials (a zuliprc holding the
admin's own API key), never a bot's: bots cannot create bots or invite users.

INVARIANTS:
- setup is idempotent: run it twice and the org ends up the same.
- mint never reuses a short name that is taken; it appends a counter.
- reconcile-owners only hands over active bots the admin owns, named
  "<First>'s Claude", to exactly one active human with that first name.
"""

from __future__ import annotations

import re
import sys
from urllib.parse import urlparse

import zulip

DEFAULT_CHANNELS = {
    "general": "Humans and Claudes, anything goes. Put /nobots in a topic name to keep bots out.",
    "claudes": "Where the Claudes talk to each other (humans welcome). One topic per conversation.",
    "scheduling": "Availability and plans. Claudes give coarse availability only; humans confirm.",
}
DEFAULT_KIT_URL = "https://github.com/BaesTheorem/claude-zulip-kit"
BOT_NAME = re.compile(r"^(?P<first>.+?)'s Claude$", re.IGNORECASE)


def _ok(result: dict, what: str) -> dict:
    if result.get("result") != "success":
        sys.exit(f"{what} failed: {result.get('msg') or result}")
    return result


def _site(client: zulip.Client) -> str:
    return client.base_url.rstrip("/").removesuffix("/api")


def first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0] if parts else ""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "friend"


def bot_first(bot: dict) -> str | None:
    m = BOT_NAME.match(bot.get("full_name", "").strip())
    return m.group("first").strip().lower() if m else None


# --- pure logic ---------------------------------------------------------------

def plan_transfers(members: list[dict], admin_id: int) -> tuple[list[tuple[dict, dict]], list[dict], list[tuple[dict, list[dict]]]]:
    """Return (transfers, pending, ambiguous) for admin-owned "<First>'s Claude" bots."""
    bots = [m for m in members if m.get("is_bot") and m.get("bot_owner_id") == admin_id
            and m.get("is_active", True) and bot_first(m)]
    humans = [m for m in members if not m.get("is_bot") and m.get("is_active", True)
              and m["user_id"] != admin_id]
    humans_by_first: dict[str, list[dict]] = {}
    for h in humans:
        humans_by_first.setdefault(first_name(h["full_name"]).lower(), []).append(h)
    bots_by_first: dict[str, list[dict]] = {}
    for b in bots:
        bots_by_first.setdefault(bot_first(b) or "", []).append(b)
    transfers, pending, ambiguous = [], [], []
    for b in bots:
        key = bot_first(b) or ""
        matches = humans_by_first.get(key, [])
        if not matches:
            pending.append(b)
        elif len(matches) == 1 and len(bots_by_first[key]) == 1:
            transfers.append((b, matches[0]))
        else:
            ambiguous.append((b, matches))
    return transfers, pending, ambiguous


def zuliprc_block(email: str, key: str, site: str) -> str:
    return f"[api]\nemail={email}\nkey={key}\nsite={site}\n"


def dm_text(org_name: str, invite_link: str, kit_url: str, zuliprc: str | None) -> str:
    text = (
        f"Our Claudes can now talk to each other (and to us) in one chat: {org_name}, on Zulip. Two steps:\n\n"
        f"1. Join: {invite_link} (Google sign-in works)\n"
        f"2. Give this link to your Claude Code and ask it to set you up: {kit_url}\n\n"
        "The page walks you and your Claude through the rest.\n"
    )
    if zuliprc:
        text += ("\nI already made your Claude's bot. When your Claude asks for the bot config, give it this "
                 "(you can regenerate the key any time in Settings > Bots):\n" + zuliprc)
    else:
        text += ("It needs one thing from you: a bot login for your Claude, which takes four clicks "
                 "in Zulip and is explained there.\n")
    return text


# --- API operations -------------------------------------------------------------

def find_user(client: zulip.Client, email: str) -> dict:
    for u in _ok(client.get_members(), "list members")["members"]:
        if email.lower() in {u.get("delivery_email", "").lower(), u.get("email", "").lower()}:
            return u
    sys.exit(f"no member with email {email}")


def stream_ids(client: zulip.Client, names: list[str]) -> list[int]:
    streams = _ok(client.get_streams(include_all_active=True), "list channels")["streams"]
    by_name = {s["name"].lower(): s["stream_id"] for s in streams}
    missing = [n for n in names if n.lower() not in by_name]
    if missing:
        sys.exit(f"channels not found: {missing} (run `claude-zulip admin setup` first)")
    return [by_name[n.lower()] for n in names]


def setup(client: zulip.Client, channels: dict[str, str], description: str | None) -> None:
    groups = _ok(client.get_user_groups(), "list groups")["user_groups"]
    admins = next(g["id"] for g in groups if g["name"] == "role:administrators")
    realm = {"invite_required": "true", "can_invite_users_group": f'{{"new": {admins}}}'}
    if description:
        realm["description"] = description
    _ok(client.call_endpoint("realm", method="PATCH", request=realm), "update org settings")
    print("org: invite-only, only admins can invite")
    existing = {s["name"].lower() for s in _ok(client.get_streams(include_all_active=True), "list channels")["streams"]}
    to_create = [{"name": n, "description": d} for n, d in channels.items() if n.lower() not in existing]
    if to_create:
        _ok(client.add_subscriptions(streams=to_create, announce=False, history_public_to_subscribers=True), "create channels")
        print("channels created:", [s["name"] for s in to_create])
    for n, d in channels.items():
        if n.lower() in existing:
            sid = stream_ids(client, [n])[0]
            client.call_endpoint(f"streams/{sid}", method="PATCH", request={"description": d})
    defaults = {s["stream_id"] for s in _ok(client.get_streams(include_default=True), "list channels")["streams"] if s.get("is_default")}
    for sid in stream_ids(client, list(channels)):
        if sid not in defaults:
            _ok(client.call_endpoint("default_streams", method="POST", request={"stream_id": sid}), "set default channel")
    print("default channels for new members:", list(channels))


def invite_link(client: zulip.Client, days: int, channels: list[str]) -> str:
    r = _ok(client.call_endpoint("invites/multiuse", method="POST", request={
        "invite_expires_in_minutes": days * 24 * 60, "invite_as": 400,
        "stream_ids": stream_ids(client, channels), "include_realm_default_subscriptions": "true",
    }), "create invite link")
    return r["invite_link"]


def invite_emails(client: zulip.Client, emails: list[str], days: int, channels: list[str]) -> None:
    _ok(client.call_endpoint("invites", method="POST", request={
        "invitee_emails": ", ".join(emails), "stream_ids": stream_ids(client, channels),
        "invite_expires_in_minutes": days * 24 * 60, "invite_as": 400,
        "include_realm_default_subscriptions": "true",
    }), "send invitations")


def mint(client: zulip.Client, name: str, channels: list[str], owner_email: str | None = None) -> dict:
    """Create "<name>'s Claude", subscribe it, optionally hand it to owner_email; return its config."""
    full = f"{name}'s Claude"
    base = f"{slug(name)}-claude"
    short = base
    result: dict = {}
    for attempt in range(1, 6):
        short = base if attempt == 1 else f"{base}-{attempt}"
        result = client.call_endpoint("bots", method="POST",
                                      request={"full_name": full, "short_name": short, "bot_type": 1})
        if result.get("result") == "success":
            break
        if "already" not in result.get("msg", ""):
            sys.exit(f"create bot failed: {result.get('msg')}")
    _ok(result, "create bot")
    email = result.get("email") or f"{short}-bot@{urlparse(_site(client)).netloc}"
    _ok(client.add_subscriptions(streams=[{"name": c} for c in channels], principals=[email]), "subscribe bot")
    owner = None
    if owner_email:
        owner = find_user(client, owner_email)
        _ok(client.call_endpoint(f"bots/{result['user_id']}", method="PATCH",
                                 request={"bot_owner_id": owner["user_id"]}), "set owner")
    return {"name": full, "bot_id": result["user_id"], "email": email, "api_key": result["api_key"],
            "site": _site(client), "owner": owner}


def transfer(client: zulip.Client, bot_email: str, owner_email: str) -> tuple[dict, dict]:
    bot, owner = find_user(client, bot_email), find_user(client, owner_email)
    _ok(client.call_endpoint(f"bots/{bot['user_id']}", method="PATCH",
                             request={"bot_owner_id": owner["user_id"]}), "transfer bot")
    return bot, owner


def reconcile_owners(client: zulip.Client, dry_run: bool = False) -> list[str]:
    """Hand minted bots to the members with matching first names. Returns log lines."""
    me = _ok(client.get_profile(), "whoami")["user_id"]
    members = _ok(client.get_members(), "list members")["members"]
    transfers, pending, ambiguous = plan_transfers(members, me)
    lines = []
    for bot, human in transfers:
        label = f"{bot['full_name']} -> {human['full_name']} ({human['user_id']})"
        if not dry_run:
            _ok(client.call_endpoint(f"bots/{bot['user_id']}", method="PATCH",
                                     request={"bot_owner_id": human["user_id"]}), "transfer bot")
        lines.append(("would transfer " if dry_run else "transferred ") + label)
    lines += [f"pending: {b['full_name']} (nobody with that first name has joined yet)" for b in pending]
    for bot, humans in ambiguous:
        names = ", ".join(f"{h['full_name']} ({h['user_id']})" for h in humans)
        lines.append(f"ambiguous: {bot['full_name']} could be {names}; use `admin transfer` by hand")
    return lines or ["nothing to do"]


def list_users(client: zulip.Client, bots_only: bool) -> list[str]:
    rows = []
    for u in _ok(client.get_members(), "list members")["members"]:
        if bool(u.get("is_bot")) != bots_only:
            continue
        flag = "bot" if u.get("is_bot") else ("admin" if u.get("is_admin") else "member")
        state = "" if u.get("is_active", True) else "  [deactivated]"
        owner = f"  owner={u.get('bot_owner_id')}" if u.get("is_bot") else ""
        rows.append(f"{u['user_id']:>8}  {flag:<6} {u['full_name']:<24} {u.get('delivery_email') or u['email']}{owner}{state}")
    return rows


def deactivate(client: zulip.Client, email: str) -> dict:
    u = find_user(client, email)
    path = f"bots/{u['user_id']}" if u.get("is_bot") else f"users/{u['user_id']}"
    _ok(client.call_endpoint(path, method="DELETE"), "deactivate")
    return u


def org_name(client: zulip.Client) -> str:
    try:
        return client.get_server_settings().get("realm_name") or "our Zulip"
    except Exception:  # noqa: BLE001 - cosmetic; the DM still reads fine without it
        return "our Zulip"
