"""Tests for claude_zulip/admin.py: bot hand-off matching and the invite message."""

from claude_zulip import admin

ADMIN = 1


def _m(uid, name, bot=False, owner=None, active=True):
    d = {"user_id": uid, "full_name": name, "is_bot": bot, "is_active": active}
    if bot:
        d["bot_owner_id"] = owner
    return d


def test_transfer_when_exactly_one_human_matches():
    members = [_m(1, "Alex"), _m(2, "Heidi Bickner"), _m(10, "Heidi's Claude", bot=True, owner=1),
               _m(11, "MIST", bot=True, owner=1)]
    transfers, pending, ambiguous = admin.plan_transfers(members, ADMIN)
    assert [(b["user_id"], h["user_id"]) for b, h in transfers] == [(10, 2)]
    assert pending == [] and ambiguous == []


def test_pending_until_the_person_joins():
    members = [_m(1, "Alex"), _m(10, "Heidi's Claude", bot=True, owner=1)]
    transfers, pending, ambiguous = admin.plan_transfers(members, ADMIN)
    assert transfers == [] and [b["user_id"] for b in pending] == [10]


def test_same_first_name_twice_is_ambiguous():
    members = [_m(1, "Alex"), _m(2, "Sam A"), _m(3, "Sam B"), _m(10, "Sam's Claude", bot=True, owner=1)]
    transfers, pending, ambiguous = admin.plan_transfers(members, ADMIN)
    assert transfers == [] and ambiguous[0][0]["user_id"] == 10


def test_other_owners_deactivated_bots_and_the_admin_are_ignored():
    members = [_m(1, "Alex"), _m(2, "heidi"), _m(10, "Heidi's Claude", bot=True, owner=2),
               _m(12, "Test's Claude", bot=True, owner=1, active=False), _m(3, "Test"),
               _m(13, "Alex's Claude", bot=True, owner=1)]
    transfers, pending, ambiguous = admin.plan_transfers(members, ADMIN)
    assert transfers == [] and [b["user_id"] for b in pending] == [13] and ambiguous == []


def test_invite_message_variants():
    block = admin.zuliprc_block("jane-claude-bot@x.zulipchat.com", "k", "https://x.zulipchat.com")
    with_bot = admin.dm_text("The Claudes", "https://x/join/abc", "https://kit", block)
    assert "1. Join: https://x/join/abc" in with_bot and "key=k" in with_bot and "four clicks" not in with_bot
    without = admin.dm_text("The Claudes", "https://x/join/abc", "https://kit", None)
    assert "four clicks" in without and "[api]" not in without
    assert admin.slug("Zoë O'Brien") == "zo-o-brien"
