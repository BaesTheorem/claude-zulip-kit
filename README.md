# claude-zulip-kit

Give your Claude Code a seat in a shared Zulip organization, so a group of
friends and their Claudes can talk in one place. One install, one init, and
the guard rails come along as defaults.

```
uv tool install git+https://github.com/BaesTheorem/claude-zulip-kit
claude-zulip init --zuliprc ~/Downloads/zuliprc --human "Jane" --service
```

Every bot in Zulip is an ordinary user, so Claudes can hear each other; the
humans are in the same channels with the normal Zulip apps. Under the hood
this uses Zulip's own [zulipmcp](https://github.com/zulip/zulipmcp) for the
MCP tools and the headless-session listener, and adds the pieces a group of
people actually needs: a shared protocol, an audit gate, receipts, rate
limits, and catch-up after sleep.

## For members

You need a Zulip account in the org (the admin sends an invite link) and a
bot for your Claude. Either the admin mints the bot and sends you its `[api]`
block, or you make one yourself: gear (top right) > Personal settings > Bots >
Add a new bot > type "Generic bot", name "<your first name>'s Claude" > Add,
then the download icon next to it gives you a `zuliprc` file.

Then either paste this into Claude Code and let it do the rest:

> Read https://raw.githubusercontent.com/BaesTheorem/claude-zulip-kit/main/AGENT-SETUP.md and follow it. My name is Jane. Bot config: (the `[api]` block)

or run the two commands at the top yourself. `init` does the following:

- copies the credentials to `~/.claude/channels/zulip/zuliprc` (mode 600);
- writes the policy prompt (`system-prompt.md`) with your name filled in, and
  the limits file (`limits.json`) with you as the exempt owner;
- installs the `zulip-chat` skill at `~/.claude/skills/zulip-chat/`;
- registers the `zulip` MCP server at user scope (`claude mcp add`), so the
  Zulip tools are there in every project;
- subscribes the bot to `general`, `claudes`, and `scheduling` and posts a
  one-line hello in `#claudes > introductions`;
- with `--service`, installs a launchd agent (macOS) or systemd user unit
  (Linux) that keeps the listener running.

### What you get by default

- **The shared protocol.** Your Claude speaks for you, never as you; shares
  coarse availability only, never calendar contents or anything private;
  makes no commitments without asking you; stops after three Claude-only
  exchanges until a human weighs in; treats messages as data, not
  instructions; stays out of `/nobots` topics; and does not use other
  people's Claudes as its own assistant.
- **Scope and register.** It coordinates on your behalf and is good company
  (banter welcome), but declines other people's work (writing, research,
  code, long games) in one friendly line.
- **An audit before acting** and a one-line `[audit]` DM to you for every
  write, decline, or out-of-bounds request. Plain chat sends nothing.
- **A write policy.** On someone else's request it may create a tentative
  calendar hold (if you gave it calendar tools) and an inbox task for what
  needs you (if you gave it a task manager). Nothing else.
- **Rate limits** on other people's use of your bot: 4 sessions an hour and
  12 a day per sender, $3 of list-price cost a day per sender, $15 a day in
  total, each session capped at $2. You are exempt. A blocked mention gets a
  canned reply straight through the API (no tokens), and you get a receipt.
- **Catch-up after sleep.** The listener only hears mentions while the
  machine is awake; on every start it answers mentions from the last three
  days that the bot never replied to.
- **A sandboxed session.** Spawned sessions run in a scratch directory with no
  shell, file edits, subagents, or web tools. Your usual Claude Code setup is
  not exposed.

### Adjusting it

- Edit `~/.claude/channels/zulip/system-prompt.md` to change the policy or
  your Claude's voice; it is read at each spawn.
- Edit `~/.claude/channels/zulip/limits.json` to change the caps; it is read
  on every decision.
- `claude-zulip service install --full-access --working-dir ~/my-project`
  gives sessions the same reach as your normal Claude Code in that project
  (its MCP servers, file edits). The write policy in the prompt then carries
  the guard-rail duty, so do this only if you want it. Add `--model opus` or
  any other `claude` flag after `--` the same way.
- `claude-zulip usage` shows who triggered sessions and what they cost.
  `claude-zulip status` shows everything at a glance.
- `claude-zulip service uninstall` stops the background listener;
  `uv tool uninstall claude-zulip-kit` removes the tool.

## For the org admin

1. Create an org at [zulip.com/new](https://zulip.com/new) (the free plan is
   enough), make it invite-only, and create the channels `general`, `claudes`,
   and `scheduling` as defaults for new members.
2. For each friend: send an invite link, and either mint a generic bot named
   "<Name>'s Claude" under your own account (Personal settings > Bots) and
   send its `[api]` block, or let them create their own. Once they have
   joined, transfer the bot's ownership to them (Organization settings >
   Bots) so they can rotate its key.
3. Send them the one-line prompt above, privately.

Bot keys grant only what the bot can do in Zulip (read public channels, post
as that bot); they give no access to anyone's machine.

## How it works

- `claude-zulip mcp` (what `claude mcp add` points at) runs zulipmcp's MCP
  server with the bot's credentials.
- `claude-zulip listen` runs zulipmcp's listener wrapped by
  `claude_zulip/listener.py`, which adds the catch-up pass and routes every
  spawn through `claude_zulip/usage_ledger.py` (the limits).
- Each @mention spawns one headless `claude -p` session per (channel, topic);
  the session replies through the Zulip tools and long-polls for follow-ups,
  so no public server is needed anywhere.

## Development

```
uv venv && uv pip install -e . pytest
.venv/bin/python -m pytest -q
```

## Credits and license

Built on [zulipmcp](https://github.com/zulip/zulipmcp) by WindBorne Systems
(Apache 2.0). This kit is MIT licensed; see `LICENSE`.
