import os
import sys
import base64
from datetime import datetime, timezone

import requests
import yaml

# ─── Constants ────────────────────────────────────────────────────────────────

STRIKE_BAN = 3

CLOSE_REASON_MAP = {
    "resolved":  "RESOLVED",
    "outdated":  "OUTDATED",
    "duplicate": "DUPLICATE",
}

# ─── Environment ──────────────────────────────────────────────────────────────
# GITHUB_REPOSITORY, GITHUB_ACTOR, GITHUB_EVENT_NAME are set automatically
# by the runner. Everything else is passed explicitly from action.yml.

TOKEN              = os.environ["GITHUB_TOKEN"]
REPO               = os.environ["GITHUB_REPOSITORY"]
ACTOR              = os.environ["GITHUB_ACTOR"]
EVENT              = os.environ["GITHUB_EVENT_NAME"]
DISCUSSION_NODE_ID = os.environ["DISCUSSION_NODE_ID"]
DISCUSSION_AUTHOR  = os.environ["DISCUSSION_AUTHOR"]
COMMENT_BODY       = os.environ.get("COMMENT_BODY", "").strip()

CONFIG_PATH  = os.environ.get("CONFIG_PATH",  ".github/moderation/config.yml")
BANNED_PATH  = os.environ.get("BANNED_PATH",  ".github/moderation/banned.yml")
STRIKES_PATH = os.environ.get("STRIKES_PATH", ".github/moderation/strikes.yml")

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# ─── Contents API ─────────────────────────────────────────────────────────────
# All file I/O goes through the API — no checkout required.
# NOTE: concurrent writes will 409 and fail the step. Not handled by design.

def read_file(path):
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
    parsed = yaml.safe_load(base64.b64decode(data["content"])) or {}
    return parsed, data["sha"]


def write_file(path, content, sha, commit_msg):
    body = {
        "message": commit_msg,
        "content": base64.b64encode(
            yaml.dump(content, default_flow_style=False, allow_unicode=True).encode()
        ).decode(),
    }
    if sha:           # omit sha entirely on first-ever write (file creation)
        body["sha"] = sha

    resp = requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()

# ─── GraphQL ──────────────────────────────────────────────────────────────────

def graphql(query, variables):
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


def close_discussion(node_id, reason):
    """reason must be a DiscussionCloseReason enum: RESOLVED | OUTDATED | DUPLICATE"""
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


def post_comment(discussion_id, body):
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

def extract_target(body):
    """Pull @username (or plain username) from the second token of a command."""
    parts = body.strip().split()
    return parts[1].lstrip("@") if len(parts) >= 2 else None


def parse_close(body):
    """
    /close resolved [optional note]  →  ("RESOLVED", "resolved [optional note]")
    /close                           →  (None, None)          ← missing reason
    /close badword                   →  (None, "badword")     ← unrecognised reason
    """
    parts = body.strip().split(None, 1)
    if len(parts) < 2:
        return None, None
    reason_text = parts[1].strip()
    first_word  = reason_text.split()[0].lower()
    enum        = CLOSE_REASON_MAP.get(first_word)
    return enum, reason_text    # enum is None when first_word isn't in the map

# ─── Moderation actions ───────────────────────────────────────────────────────

def do_ban(target, actor, reason, banned, banned_sha):
    banned[target] = {
        "reason":    reason,
        "banned_by": actor,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_file(BANNED_PATH, banned, banned_sha, f"ban: {target}")
    # TODO: remove the name from strikes list if present. if banned, strikes are reset


def do_strike(target, strikes, strikes_sha, banned, banned_sha):
    strikes[target] = strikes.get(target, 0) + 1
    count = strikes[target]

    write_file(
        STRIKES_PATH, strikes, strikes_sha,
        f"strike: {target} ({count}/{STRIKE_BAN})"
    )
    post_comment(
        DISCUSSION_NODE_ID,
        f"⚠️ Strike **{count}/{STRIKE_BAN}** issued to @{target} by @{ACTOR}."
    )

    if count >= STRIKE_BAN:
        reason = f"Auto-ban: reached {STRIKE_BAN} strikes"
        do_ban(target, "__system__", reason, banned, banned_sha)
        post_comment(
            DISCUSSION_NODE_ID,
            f"🔨 @{target} has been automatically banned — {reason}."
        )
        close_discussion(DISCUSSION_NODE_ID, "RESOLVED")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    config,  _            = read_file(CONFIG_PATH)
    banned,  banned_sha   = read_file(BANNED_PATH)
    strikes, strikes_sha  = read_file(STRIKES_PATH)

    closers    = config.get("closers",    [])
    strikers   = config.get("strikers",   [])
    moderators = config.get("moderators", [])

    # ── Discussion opened ──────────────────────────────────────────────────────
    if EVENT == "discussion":
        if ACTOR in banned:
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")
            post_comment(
                DISCUSSION_NODE_ID,
                f"This discussion has been automatically closed. "
                f"@{ACTOR} is not permitted to open discussions."
            )
        return

    # ── Comment posted ─────────────────────────────────────────────────────────
    if EVENT == "discussion_comment":
        if not COMMENT_BODY:
            return

        parts = COMMENT_BODY.split()
        cmd   = parts[0].lower()

        # /close <reason> [note]
        if cmd == "/close":
            if ACTOR not in closers:
                post_comment(
                    DISCUSSION_NODE_ID,
                    f"@{ACTOR} is not authorised to use `/close`."
                )
                return

            enum_reason, human_reason = parse_close(COMMENT_BODY)

            if enum_reason is None:
                if human_reason is None:
                    post_comment(
                        DISCUSSION_NODE_ID,
                        "Usage: `/close resolved|outdated|duplicate [optional note]`"
                    )
                else:
                    post_comment(
                        DISCUSSION_NODE_ID,
                        f"Unknown close reason `{human_reason.split()[0]}`.\n"
                        "Valid reasons: `resolved`, `outdated`, `duplicate`."
                    )
                return

            post_comment(
                DISCUSSION_NODE_ID,
                f"Closed by @{ACTOR} — {human_reason}"
            )
            close_discussion(DISCUSSION_NODE_ID, enum_reason)

        # /strike [@user]  (defaults to discussion author)
        elif cmd == "/strike":
            if ACTOR not in strikers:
                post_comment(
                    DISCUSSION_NODE_ID,
                    f"@{ACTOR} is not authorised to use `/strike`."
                )
                return
            target = extract_target(COMMENT_BODY) or DISCUSSION_AUTHOR
            do_strike(target, strikes, strikes_sha, banned, banned_sha)

        # /ban @user [reason]
        elif cmd == "/ban":
            if ACTOR not in moderators:
                post_comment(
                    DISCUSSION_NODE_ID,
                    f"@{ACTOR} is not authorised to use `/ban`."
                )
                return

            target = extract_target(COMMENT_BODY)
            if not target:
                post_comment(
                    DISCUSSION_NODE_ID,
                    "Usage: `/ban @username [reason]`"
                )
                return

            reason_parts = COMMENT_BODY.split(None, 2)
            reason       = reason_parts[2].strip() if len(reason_parts) > 2 else "No reason provided."

            do_ban(target, ACTOR, reason, banned, banned_sha)
            post_comment(
                DISCUSSION_NODE_ID,
                f"🔨 @{target} banned by @{ACTOR} — {reason}"
            )
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")


if __name__ == "__main__":
    main()