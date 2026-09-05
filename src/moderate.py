import os
import base64
from datetime import datetime, timezone
import re
from typing import Any, Literal, cast
from urllib import request
from xml.etree.ElementTree import tostring # cast is just so pyright shuts up

import requests
import yaml


# Possible User Commands

USER_COMMANDS = [
    "close",
    "open",
    "strike",
    "strike-target",
    "ban",
    "unban",
]


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

# --- Strikes ---------------------------------------------------------------

STRIKES_PATH: str    = os.environ.get(key="STRIKES_PATH",  default=".github/discussion_moderator/strikes.yml")

# How many strikes before ban - optional -- switch to enable if strikes feature is turned on
_strikes_to_ban: str  = os.environ.get("STRIKES_TO_BAN", "").strip()
STRIKE_TO_BAN: int | None       = int(_strikes_to_ban) if _strikes_to_ban else None

STRIKES_ENABLED: bool = bool(_strikes_to_ban)



# --- Format-Enforcement -------------------------------------------------------

FORMAT_REGEX_PATH: str = os.environ.get("AUTO_CLOSE_REGEX", "").strip()

FORMAT_ENFORCEMENT_ENABLED: bool = bool(FORMAT_REGEX_PATH)

FORMAT_ENFORCEMENT_STRIKES_ENABLED:bool =  True if os.environ.get("STRIKE_PER_NONFORMAT", "").strip().lower() == "true" else False


# ─── Contents API ────────────────────────────────────────────────────────
# All file I/O goes through the API - no checkout required.
# NOTE: concurrent writes will 409 and fail the step. Not handled by design.

def read_yaml_file(path: str, firstWriteWillCreate: bool = False) -> tuple[dict[Any, Any], str | None]:
    """
    Returns (parsed_dict, sha).
    firstWriteWillCreate = True -> sha is None when the file doesn't exist yet (first write will create it).
    """
    resp: requests.Response = requests.get(
        url=f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers=HEADERS,
    )
    if firstWriteWillCreate and resp.status_code == 404:
        return {}, None
    # Any other error - we have an actual problem
    resp.raise_for_status()
    data   = resp.json()
    parsed = cast(dict[str, Any], yaml.safe_load(base64.b64decode(data["content"])) or {}) # comment only yaml parses to NULL so we normalise so dict access doesn't crash later
    return parsed, data["sha"]


def read_txt_file(path: str, firstWriteWillCreate: bool = False) -> tuple[Literal[''], None] | tuple[str, str | None]:
    """
    Returns (regexString: str, sha).
    sha is None when the file doesn't exist yet (first write will create it).
    """

    resp: requests.Response = requests.get(
        url=f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers=HEADERS
    )

    if firstWriteWillCreate and resp.status_code == 404:
        return "", None
    # Any other error - we have an actual problem
    resp.raise_for_status()

    data   = resp.json()

    content: str = base64.b64decode(s=data["content"]).decode(encoding="utf-8")
    return content, data["sha"]


def write_yaml_file(path: str, content: dict[str, Any], sha: str | None, commit_msg: str) -> str:
    """
    Returns str: sha.
    """


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


# ─── GraphQL - for enacting operations on discussion itself ──────────────────────────────────────────────────────────────────

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
    return

def reopen_discussion(node_id: str) -> None:
    #TODO: do this -- same as above and all, but also (re)opens the discussion


    return





def post_comment(discussion_id: str, body: str) -> None:
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
    return

# ─── Parsing ──────────────────────────────────────────────────────────────────

def extract_username(body: str) -> str | None:
    """Pull @username (or plain username) from the second token of a command of form `command <username>`"""
    parts: list[str] = body.strip().split(sep=' ', maxsplit=3)
    return parts[1].lstrip("@") if len(parts) >= 2 else None


def parse_close(body: str)  -> tuple[str | None, str | None]:
    """
    /close RESOLVED/resolved optional note  →  ("RESOLVED", "optional note")
    /close RESOLVED/resolved                →  ("RESOLVED", "")
    /close weeewooo                         →  ("weeewooo", None)   <- unrecognised reason
    /close                                  →  (None, None)         <- missing reason
    """
    parts: list[str] = body.strip().split(sep=" ", maxsplit=2) # 0 is close, 1 is github reason, 2 is the rest

    if len(parts) < 2 or parts[0].strip().lower() != "close":
        return None, None

    closeReason: str | None  = CLOSE_REASON_MAP.get(parts[1].strip().lower())
    reason_text: str | None = parts[1] if closeReason is None else (parts[2] if len(parts) > 2 else None)

    return closeReason, reason_text





def get_whole_line_after_command_from_comment_body(id_s:int) -> str:

    # empty string impossible since cmd empty would return by now
    m: re.Match[str] | None = re.match(pattern=r".*[^\n]", string=COMMENT_BODY[id_s:])

    assert m # this should only be called after command is recognised as valid - and thus not m case should be impossible
    return m.group()



# ─── Moderation actions ───────────────────────────────────────────────────────

def do_ban(target: str, actor: str, reason: str, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:

    banned_users[target] = {
        "reason":    reason,
        "banned_by": actor,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_yaml_file(BANNED_PATH, banned_users, banned_users_sha, f"ban: {target}")

    clear_strikes(target, strike_counts, strike_counts_sha)

    return



def do_unban(target: str, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:
    if target not in banned_users:
        post_comment(DISCUSSION_NODE_ID, f"@{target} is not in the banned list.")
        return
    del banned_users[target]
    write_yaml_file(BANNED_PATH, banned_users, banned_users_sha, f"unban: {target}")
    clear_strikes(target, strike_counts, strike_counts_sha) # Redundancy. Ideally this should have been removed after the user was banned

    return




def do_strike(target: str, strike_counts: dict[str, int], strike_counts_sha: str | None, banned_users: dict[str, dict[str, str]], banned_users_sha: str | None, automatedStrike:bool = False) -> None:
    """
    Does strike to target user. Also bans if the user exceeded the STRIKE_TO_BAN threshold
    """

    if not STRIKES_ENABLED:
        return

    strike_counts[target] = strike_counts.get(target, 0) + 1
    count: int = strike_counts[target]

    strike_counts_sha = write_yaml_file(
        STRIKES_PATH, strike_counts, strike_counts_sha,
        f"strike: {target} ({count}/{STRIKE_TO_BAN})"
    )

    post_comment(
        DISCUSSION_NODE_ID,
        body=f"⚠️ Strike **{count}/{STRIKE_TO_BAN}** issued to @{target}"
        + (f" by @{ACTOR}." if not automatedStrike else ".")
    )

    assert STRIKE_TO_BAN is not None
    if count >= STRIKE_TO_BAN:
        reason: str = f"Auto-ban: reached {STRIKE_TO_BAN} strikes"
        do_ban(target, "__system__", reason, banned_users, banned_users_sha, strike_counts, strike_counts_sha)
        post_comment(
            DISCUSSION_NODE_ID,
            f"🔨 @{target} has been automatically banned with reason: {reason}."
        )
        close_discussion(DISCUSSION_NODE_ID, "RESOLVED")


    return


def clear_strikes(target: str, strike_counts: dict[str, int], strike_counts_sha: str | None) -> None:

    # no-op call if disabled
    if not STRIKES_ENABLED:
        return

    if target not in strike_counts:
        return
    
    del strike_counts[target]
    write_yaml_file(STRIKES_PATH, strike_counts, strike_counts_sha, f"clear strikes: {target}")

    return



def complies_format(body:str) -> bool:

    if not FORMAT_ENFORCEMENT_ENABLED:
        return False

    regex:str
    _:Any

    regex, _  = read_txt_file(FORMAT_REGEX_PATH)

    return bool(re.fullmatch(regex, body))













# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    user_permission_config: dict[str, list[str]]
    _: Any
    user_permission_config, _ = cast(tuple[dict[str, list[str]], str | None], read_yaml_file(CONFIG_PATH))

    banned_user_list: dict[str, dict[str, str]]
    banned_user_list_sha: str | None
    banned_user_list, banned_user_list_sha = cast(tuple[dict[str, dict[str, str]], str | None], read_yaml_file(BANNED_PATH))

    strike_counts_list: dict[str, int]
    strike_counts_list_sha: str | None
    if STRIKES_ENABLED:
        strike_counts_list, strike_counts_list_sha = cast(tuple[dict[str, int], str | None], read_yaml_file(STRIKES_PATH))
    else:
        strike_counts_list, strike_counts_list_sha = {}, None


    closers      : list[str]   = user_permission_config.get("closers") or []
    strikers     : list[str] = user_permission_config.get("strikers") or []
    moderators   : list[str]   = user_permission_config.get("moderators") or []

    # Discussion opened
    if EVENT == "discussion":
        if ACTOR in banned_user_list:
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")
            post_comment(
                DISCUSSION_NODE_ID,
                f"This discussion has been automatically closed.\n"
                f"@{ACTOR} is not permitted to open discussions.\n"
                f"Ban Reason: {banned_user_list[ACTOR]["reason"]}\n"
            )
            return
        
        elif FORMAT_ENFORCEMENT_ENABLED:
            # allow for empty bodies to also go through the same chain -- same end point in any case
            if not complies_format(COMMENT_BODY):
                close_discussion(DISCUSSION_NODE_ID, "RESOLVED")

                post_comment(
                    DISCUSSION_NODE_ID,
                    f"This discussion has been automatically closed.\n"
                    f"Discussion format is NOT optional!\n"
                )
                if FORMAT_ENFORCEMENT_STRIKES_ENABLED:
                    do_strike(ACTOR, strike_counts_list, strike_counts_list_sha, banned_user_list, banned_user_list_sha, True)
            return


    # Comment posted
    if EVENT == "discussion_comment":


        # Get command
        cmd:str = ""
        id_s:int = -1
        while True:
            id_s = COMMENT_BODY.find('/', id_s+1)
            if id_s == -1:
                break

            # start from the index right after '/'
            id_s += 1


            m: re.Match[str] | None = re.match(pattern=r"\w+(\-\w+)?", string=COMMENT_BODY[id_s:])
            if not m:
                continue          # nothing valid after '/' -> skip
            id_e: int = id_s + m.end()

            for e in USER_COMMANDS:    
                if COMMENT_BODY[id_s:id_e].strip().lower() == e:
                    cmd = e
                    break
            if cmd != "":
                break
        
        if cmd == "":
            return
        


        # /close <reason> [note]
        if cmd == "close":
            if ACTOR not in closers and ACTOR not in moderators:
                return

            close_command_body: str = get_whole_line_after_command_from_comment_body(id_s)

            enum_reason: str | None
            human_reason: str | None
            enum_reason, human_reason = parse_close(close_command_body)

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
                        f"Unknown close reason `{enum_reason}`"
                    )
                return

            post_comment(
                discussion_id=DISCUSSION_NODE_ID,
                body=f"Closed by @{ACTOR}" + ( f" with reason: {human_reason}" if human_reason != None else ".")
            )
            close_discussion(DISCUSSION_NODE_ID, enum_reason)
            return


        # /open
        elif cmd == "open":
            if ACTOR not in closers and ACTOR not in moderators:
                return
            reopen_discussion(DISCUSSION_NODE_ID)
            return



        # /strike
        elif cmd == "strike":
            # strikes not configured
            if not STRIKES_ENABLED:
                return

            if ACTOR not in strikers and ACTOR not in closers and ACTOR not in moderators:
                return

            target = DISCUSSION_AUTHOR

            # If user already banned, don't strike. It's rude to hit someone when they're down.
            if target in banned_user_list:
                post_comment(
                    discussion_id=DISCUSSION_NODE_ID,
                    body=f"@{target} already banned"
                )
                return

            do_strike(target, strike_counts_list, strike_counts_list_sha, banned_user_list, banned_user_list_sha)
            return



        # /strike-target <username>
        elif cmd == "strike-target":
            # strikes not configured
            if not STRIKES_ENABLED:
                return

            if ACTOR not in strikers and ACTOR not in closers and ACTOR not in moderators:
                return


            close_command_body: str = get_whole_line_after_command_from_comment_body(id_s)

            target: str | None = extract_username(close_command_body)
            if target is None:
                post_comment(DISCUSSION_NODE_ID, "Usage: `/strike-target <username>`")
                return

            # If user already banned, don't strike. It's rude to hit someone when they're down.
            if target in banned_user_list:
                post_comment(
                    discussion_id=DISCUSSION_NODE_ID,
                    body=f"@{target} already banned"
                )
                return

            do_strike(target, strike_counts_list, strike_counts_list_sha, banned_user_list, banned_user_list_sha)
            return

    
        # /ban <reason>
        elif cmd == "ban":
            if ACTOR not in moderators:
                return

            target = DISCUSSION_AUTHOR

            if target in banned_user_list:
                post_comment(
                discussion_id=DISCUSSION_NODE_ID,
                body=f"@{target} already banned"
                )
                return


            reason_parts: list[str] = get_whole_line_after_command_from_comment_body(id_s).split(sep=' ', maxsplit=1)

            reason: str | None = reason_parts[1] if len(reason_parts) > 1 else None
            if reason is None:
                post_comment(DISCUSSION_NODE_ID, "Usage: `/ban <reason>`")
                return

            do_ban(target, ACTOR, reason, banned_user_list, banned_user_list_sha, strike_counts_list, strike_counts_list_sha)
            
            post_comment(
                DISCUSSION_NODE_ID,
                f"🔨 @{target} banned by @{ACTOR} with reason: {reason}"
            )
            close_discussion(DISCUSSION_NODE_ID, "RESOLVED")

        # /unban <username>
        elif cmd == "unban":
            if ACTOR not in moderators:
                return


            close_command_body: str = get_whole_line_after_command_from_comment_body(id_s)

            target: str | None = extract_username(close_command_body)
            if target is None:
                post_comment(DISCUSSION_NODE_ID, "Usage: `/unban @username`")
                return
            do_unban(target, banned_user_list, banned_user_list_sha, strike_counts_list, strike_counts_list_sha)
            post_comment(DISCUSSION_NODE_ID, f"✅ @{target} has been unbanned by @{ACTOR}.")


if __name__ == "__main__":
    main()