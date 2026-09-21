"""Tests for `gate.state.lease_comment`.

`parse_lease_comment` parses comments posted by anyone on the internet
under spec D4's open-contribution model, so the failure-mode coverage
here (prose, malformed blocks, unknown actions) is load-bearing in the
same way `tests/gate/state/test_intake.py` treats intake failure modes
as load-bearing: each case asserts the specific behavior, not just "it
didn't crash."
"""

from __future__ import annotations

from gate.state.lease_comment import (
    ACTION_CLAIM,
    ACTION_HEARTBEAT,
    ACTION_RELEASE,
    LEASE_BLOCK_TAG,
    LeaseClaim,
    parse_lease_comment,
    render_lease_comment,
)


def test_round_trip() -> None:
    claim = LeaseClaim(login="octocat", action=ACTION_CLAIM, protocol=6)
    rendered = render_lease_comment(claim)
    assert LEASE_BLOCK_TAG in rendered
    assert parse_lease_comment(rendered) == claim


def test_round_trip_release() -> None:
    claim = LeaseClaim(login="octocat", action=ACTION_RELEASE, protocol=6)
    assert parse_lease_comment(render_lease_comment(claim)) == claim


def test_round_trip_heartbeat() -> None:
    claim = LeaseClaim(login="octocat", action=ACTION_HEARTBEAT, protocol=6)
    assert parse_lease_comment(render_lease_comment(claim)) == claim


def test_rendered_block_survives_surrounding_prose() -> None:
    """A caller may post the block alongside human-readable text."""
    claim = LeaseClaim(login="octocat", action=ACTION_CLAIM, protocol=6)
    body = "Claiming this task.\n\n" + render_lease_comment(claim) + "\nThanks!"
    assert parse_lease_comment(body) == claim


def test_a_prose_comment_is_not_a_lease() -> None:
    """Most comments are prose. Returning None must be the cheap path."""
    assert parse_lease_comment("Looks good to me, thanks for the fix!") is None


def test_an_empty_comment_is_not_a_lease() -> None:
    assert parse_lease_comment("") is None


def test_a_comment_with_an_unrelated_fenced_block_is_not_a_lease() -> None:
    assert parse_lease_comment("```python\nprint('hi')\n```\n") is None


def test_a_malformed_block_is_none_not_an_exception() -> None:
    """The gate parses untrusted input from anyone on the internet.

    Every one of these returns None rather than raising. Note what the
    reasons are *not*: this parser is a hand-written `key: value` reader,
    not `yaml.safe_load`, so "invalid YAML" is no longer a category. What
    replaces it is shape validation — a login must look like a GitHub
    login, a protocol must be a plain non-negative integer, a key must be
    an identifier, and a repeated key is a contradiction rather than a
    last-one-wins. A numeric login like `42` is *accepted*, because numeric
    GitHub usernames are legal; the old YAML reader rejected it for being
    an int, which was wrong.
    """
    hostile_bodies = [
        "```choir-lease\n",  # opened, never closed
        "```choir-lease\nlogin: [1, 2\n```\n",  # not a plausible login
        "```choir-lease\n- just\n- a\n- list\n```\n",  # not `key: value` lines
        "```choir-lease\naction: claim\nprotocol: 6\n```\n",  # missing login
        "```choir-lease\nlogin:\naction: claim\nprotocol: 6\n```\n",  # empty login
        "```choir-lease\nlogin: ../../etc\naction: claim\nprotocol: 6\n```\n",  # path, not login
        "```choir-lease\nlogin: -bob\naction: claim\nprotocol: 6\n```\n",  # leading hyphen
        "```choir-lease\nlogin: bob\naction: claim\nprotocol: six\n```\n",  # protocol not int
        "```choir-lease\nlogin: bob\naction: claim\nprotocol: true\n```\n",  # bool, not int
        "```choir-lease\nlogin: bob\naction: claim\nprotocol: -1\n```\n",  # negative
        # duplicate key — a contradiction, not last-one-wins
        "```choir-lease\nlogin: bob\nlogin: eve\naction: claim\nprotocol: 6\n```\n",
        "```choir-lease\n[1,2,3]: value\n```\n",  # key is not an identifier
        "```choir-lease\nkey: !!python/object/apply:os.system ['echo hi']\n```\n",
    ]
    for body in hostile_bodies:
        assert parse_lease_comment(body) is None, body


def test_an_unknown_action_is_none() -> None:
    body = "```choir-lease\nlogin: bob\naction: requeue\nprotocol: 6\n```\n"
    assert parse_lease_comment(body) is None


def test_a_higher_protocol_parses() -> None:
    """A newer client's lease must remain readable, or an upgraded worker
    silently cannot claim on a not-yet-upgraded repo.
    """
    body = "```choir-lease\nlogin: bob\naction: claim\nprotocol: 999\n```\n"
    claim = parse_lease_comment(body)
    assert claim is not None
    assert claim.login == "bob"
    assert claim.protocol == 999


def test_an_unknown_extra_field_does_not_fail_the_parse() -> None:
    """A newer client's lease must remain readable by an older reader."""
    body = (
        "```choir-lease\n"
        "login: bob\n"
        "action: claim\n"
        "protocol: 6\n"
        "future_field: something-a-protocol-7-worker-added\n"
        "```\n"
    )
    claim = parse_lease_comment(body)
    assert claim == LeaseClaim(login="bob", action=ACTION_CLAIM, protocol=6)


def test_render_uses_the_lease_block_tag() -> None:
    claim = LeaseClaim(login="bob", action=ACTION_CLAIM, protocol=6)
    assert f"```{LEASE_BLOCK_TAG}\n" in render_lease_comment(claim)
