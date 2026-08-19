import os
import base64
from datetime import datetime, timezone
from typing import Any, cast

import requests
import yaml

# Limited by what github gives
CLOSE_REASON_MAP = {
    "resolved":  "RESOLVED",
    "outdated":  "OUTDATED",
    "duplicate": "DUPLICATE",
}

# ─── Environment ──────────────────────────────────────────────────────────────
# GITHUB_REPOSITORY, GITHUB_ACTOR, GITHUB_EVENT_NAME are set automatically
# by the runner. Everything else is passed explicitly from action.yml.

TOKEN: str              = os.environ["GITHUB_TOKEN"]
REPO: str               = os.environ["GITHUB_REPOSITORY"]
ACTOR: str              = os.environ["GITHUB_ACTOR"]
EVENT: str              = os.environ["GITHUB_EVENT_NAME"]
DISCUSSION_NODE_ID: str = os.environ["DISCUSSION_NODE_ID"]
DISCUSSION_AUTHOR: str  = os.environ["DISCUSSION_AUTHOR"]
COMMENT_BODY: str       = os.environ.get(key="COMMENT_BODY", default="").strip()

CONFIG_PATH  = os.environ.get(key="CONFIG_PATH",  default=".github/discussion_moderator/config.yml")
BANNED_PATH  = os.environ.get(key="BANNED_PATH",  default=".github/discussion_moderator/banned.yml")

HEADERS: dict[str, str] = {"Authorization": f"Bearer {TOKEN}"}

# ─── Optional Features ────────────────────────────────────────────────────────


# Strikes
STRIKES_PATH: str    = os.environ.get(key="STRIKES_PATH",  default=".github/discussion_moderator/strikes.yml")

# How many strikes before ban - optional -- switch to enable if strikes feature is turned on
_strikes_to_ban: str  = os.environ.get("STRIKES_TO_BAN", "").strip()
STRIKE_TO_BAN: int | None       = int(_strikes_to_ban) if _strikes_to_ban else None

STRIKES_ENABLED: bool = bool(_strikes_to_ban)


# ─── Contents API ─────────────────────────────────────────────────────────────
# All file I/O goes through the API — no checkout required.
# NOTE: concurrent writes will 409 and fail the step. Not handled by design.

def read_file(path: str) -> tuple[dict[Any, Any], str | None]:
    """
    Returns (parsed_dict, sha).
    sha is None when the file doesn't exist yet (first write will create it).
    """
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers=HEADERS,
    )
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    data   = resp.json()
    parsed = cast(dict[str, Any], yaml.safe_load(base64.b64decode(data["content"])) or {})
    return parsed, data["sha"]


def write_file(path: str, content: dict[str, Any], sha: str | None, commit_msg: str) -> str:
    body: dict[str, str] = {
        "message": commit_msg,
        "content": base64.b64encode(
            yaml.dump(content, default_flow_style=False, allow_unicode=True).encode()
        ).decode(),
    }
    if sha:
        body["sha"] = sha

    resp = requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


# ─── GraphQL ──────────────────────────────────────────────────────────────────

def graphql(query: str, variables: dict[str, Any]) -> Any:
    resp = requests.post(
        "https://api.github.com/graphql",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise RuntimeError(f"GraphQL errors: {result['errors']}")
    return result["data"]


def close_discussion(node_id: str, reason: str) -> None:
    """ reason must be a DiscussionCloseReason enum: RESOLVED | OUTDATED | DUPLICATE -- because that's what github makes available """
    graphql(
        """
        mutation($id: ID!, $reason: DiscussionCloseReason!) {
          closeDiscussion(input: {discussionId: $id, reason: $reason}) {
            discussion { id closed }
          }
        }
        """,
        {"id": node_id, "reason": reason},
    )


def post_comment(discussion_id: str, body: str):
    graphql(
        """
        mutation($discussionId: ID!, $body: String!) {
          addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
            comment { id }
          }
        }
        """,
        {"discussionId": discussion_id, "body": body},
    )

# ─── Parsing ──────────────────────────────────────────────────────────────────

def extract_target(body: str) -> str | None:
    """Pull @username (or plain username) from the second token of a command."""
    parts: list[str] = body.strip().split()
    return parts[1].lstrip("@") if len(parts) >= 2 else None


def parse_close(body: str)  -> tuple[None, None] | tuple[str | None, str]:
    """
    /close resolved [optional note]  →  ("RESOLVED", "resolved [optional note]")
    /close                           →  (None, None)          <- missing reason
    /close weeewooo                   →  (None, "weeewooo")   <- unrecognised reason
    """
    parts: list[str] = body.strip().split(None, 1)
    if len(parts) < 2:
        return None, None
    reason_text: str = parts[1].strip()
    first_word: str  = reason_text.split()[0].lower()
    enum: str | None        = CLOSE_REASON_MAP.get(first_word)
    return enum, reason_text    # enum is None when first_word isn't in the map

# ─── Moderation actions ───────────────────────────────────────────────────────

def do_ban(target: str, actor: str, reason: str, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:

    banned_users[target] = {
        "reason":    reason,
        "banned_by": actor,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_file(BANNED_PATH, banned_users, banned_users_sha, f"ban: {target}")

    clear_strikes(target, strike_counts, strike_counts_sha)



def do_unban(target: str, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:
    if target not in banned_users:
        post_comment(DISCUSSION_NODE_ID, f"@{target} is not in the banned list.")
        return
    del banned_users[target]
    write_file(BANNED_PATH, banned_users, banned_users_sha, f"unban: {target}")
    clear_strikes(target, strike_counts, strike_counts_sha) # Redundancy. Ideally this should have been removed after the user was banned




def do_strike(target: str, strike_counts: dict[str, int], strike_counts_sha: str | None, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None) -> None:

    if not STRIKES_ENABLED:
        return

    strike_counts[target] = strike_counts.get(target, 0) + 1
    count: int = strike_counts[target]

    strike_counts_sha = write_file(
        STRIKES_PATH, strike_counts, strike_counts_sha,
        f"strike: {target} ({count}/{STRIKE_TO_BAN})"
    )
    post_comment(
        DISCUSSION_NODE_ID,
        f"⚠️ Strike **{count}/{STRIKE_TO_BAN}** issued to @{target} by @{ACTOR}."
    )

    assert STRIKE_TO_BAN is not None
    if count >= STRIKE_TO_BAN:
        reason = f"Auto-ban: reached {STRIKE_TO_BAN} strikes"
        do_ban(target, "__system__", reason, banned_users, banned_users_sha, strike_counts, strike_counts_sha)
        post_comment(
            DISCUSSION_NODE_ID,
            f"🔨 @{target} has been automatically banned with reason: {reason}."
        )
        close_discussion(DISCUSSION_NODE_ID, "RESOLVED")


def clear_strikes(target: str, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:

    # no-op call if disabled
    if not STRIKES_ENABLED:
        return

    if target not in strike_counts:
        return
    
    del strike_counts[target]
    write_file(STRIKES_PATH, strike_counts, strike_counts_sha, f"clear strikes: {target}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    mod_config: dict[str, list[str]]
    _: Any
    mod_config, _ = cast(tuple[dict[str, list[str]], str | None], read_file(CONFIG_PATH))

    banned_users: dict[str, dict[str, str]]
    banned_users_sha: str | None
    banned_users, banned_users_sha = cast(tuple[dict[str, dict[str, str]], str | None], read_file(BANNED_PATH))

    strike_counts: dict[str, int]
    strike_counts_sha: str | None
    if STRIKES_ENABLED:
        strike_counts, strike_counts_sha = cast(tuple[dict[str, int], str | None], read_file(STRIKES_PATH))
    else:
        strike_counts, strike_counts_sha = {}, None


    closers      : list[str]   = mod_config.get("closers") or []
    strikers : list[str] = mod_config.get("strikers") or []
    moderators   : list[str]   = mod_config.get("moderators") or []

    # Discussion opened
    if EVENT == "discussion":
        if ACTOR in banned_users:
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")
            post_comment(
                DISCUSSION_NODE_ID,
                f"This discussion has been automatically closed.\n"
                f"@{ACTOR} is not permitted to open discussions.\n"
                f"Ban Reason: {banned_users[ACTOR]["reason"]}\n"
            )
        return

    # Comment posted
    if EVENT == "discussion_comment":
        if not COMMENT_BODY:
            return

        parts = COMMENT_BODY.split()
        cmd   = parts[0].lower()

        # /close <reason> [note]
        if cmd == "/close":
            if ACTOR not in closers and ACTOR not in moderators:
                return

            enum_reason, human_reason = parse_close(COMMENT_BODY)

            if enum_reason is None:
                if human_reason is None:
                    post_comment(
                        DISCUSSION_NODE_ID,
                        "Usage: `/close <RESOLVED | OUTDATED | DUPLICATE> [note]`"
                    )
                    return
                else:
                    post_comment(
                        DISCUSSION_NODE_ID,
                        f"Unknown close reason `{human_reason.split()[0]}`"
                    )
                return

            post_comment(
                DISCUSSION_NODE_ID,
                f"Closed by @{ACTOR} with reason: {human_reason}"
            )
            close_discussion(DISCUSSION_NODE_ID, enum_reason)

        # /strike (defaults to discussion author)
        elif cmd == "/strike":
            # strikes not configured
            if not STRIKES_ENABLED:
                return

            if ACTOR not in strikers and ACTOR not in moderators:
                return

            target = DISCUSSION_AUTHOR

            # If user already banned, don't strike. It's rude to hit someone when they're down.
            if target in banned_users:
                post_comment(
                    DISCUSSION_NODE_ID,
                    f"@{target} already banned"
                )

                return

            do_strike(target, strike_counts, strike_counts_sha, banned_users, banned_users_sha)

        # /ban <reason>
        elif cmd == "/ban":
            if ACTOR not in moderators:
                return

            target = target = DISCUSSION_AUTHOR

            if target in banned_users:
                post_comment(
                DISCUSSION_NODE_ID,
                "@{target} already banned"
                )
                return

            reason_parts = COMMENT_BODY.split(None, 1)

            if len(reason_parts) < 2:
                post_comment(DISCUSSION_NODE_ID, "Usage: `/ban <reason>`")
                return
            reason = reason_parts[1].strip()
        
            do_ban(target, ACTOR, reason, banned_users, banned_users_sha, strike_counts, strike_counts_sha)
            
            post_comment(
                DISCUSSION_NODE_ID,
                f"🔨 @{target} banned by @{ACTOR} with reason: {reason}"
            )
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")
        # /unban @user
        elif cmd == "/unban":
            if ACTOR not in moderators:
                return
            target = extract_target(COMMENT_BODY)
            if not target:
                post_comment(DISCUSSION_NODE_ID, "Usage: `/unban @username`")
                return
            do_unban(target, banned_users, banned_users_sha, strike_counts, strike_counts_sha)
            post_comment(DISCUSSION_NODE_ID, f"✅ @{target} has been unbanned by @{ACTOR}.")


if __name__ == "__main__":
    main()