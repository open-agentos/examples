#!/usr/bin/env python3
"""
github_token.py — Mint a GitHub App installation token for a given agent role.

Usage:
    python3 scripts/github_token.py <role>

Role → env var mapping:
    builder  → BUILDER_APP_ID  + BUILDER_PRIVATE_KEY
    reviewer → REVIEWER_APP_ID + REVIEWER_PRIVATE_KEY
    watcher  → WATCHER_APP_ID  + WATCHER_PRIVATE_KEY
    board    → BOARD_APP_ID    + BOARD_PRIVATE_KEY
    docs     → DOCS_APP_ID     + DOCS_PRIVATE_KEY
    planner  → PLANNER_APP_ID  + PLANNER_PRIVATE_KEY

Prints the installation token to stdout (no trailing newline).
Requires: PyJWT>=2.8, cryptography>=41.0  (both in requirements.txt)
"""

import sys
import os
import time
import json
import urllib.request
import urllib.error

try:
    import jwt
except ImportError:
    print("ERROR: PyJWT not installed. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

ROLE_MAP = {
    "builder":  ("BUILDER_APP_ID",  "BUILDER_PRIVATE_KEY"),
    "reviewer": ("REVIEWER_APP_ID", "REVIEWER_PRIVATE_KEY"),
    "watcher":  ("WATCHER_APP_ID",  "WATCHER_PRIVATE_KEY"),
    "board":    ("BOARD_APP_ID",    "BOARD_PRIVATE_KEY"),
    "docs":     ("DOCS_APP_ID",     "DOCS_PRIVATE_KEY"),
    "planner":  ("PLANNER_APP_ID",  "PLANNER_PRIVATE_KEY"),
}


def make_jwt(app_id: str, private_key_pem: str) -> str:
    """Create a signed JWT for GitHub App authentication (valid ~9 min)."""
    now = int(time.time())
    payload = {
        "iat": now - 60,   # allow 60 s clock skew
        "exp": now + 540,  # 9 minutes (GitHub max is 10)
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_id(app_jwt: str, repo: str) -> str:
    """Resolve the installation ID for this GitHub App on the given repo."""
    owner, repo_name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/installation"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentOS-github-token",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return str(json.loads(resp.read())["id"])
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: Could not get installation ID ({e.code}): {body}", file=sys.stderr)
        sys.exit(1)


def get_installation_token(app_jwt: str, installation_id: str) -> str:
    """Exchange an installation ID for a short-lived installation access token."""
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "agentOS-github-token",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: Could not get installation token ({e.code}): {body}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/github_token.py <role>", file=sys.stderr)
        print(f"Valid roles: {', '.join(ROLE_MAP)}", file=sys.stderr)
        sys.exit(1)

    role = sys.argv[1].lower()
    if role not in ROLE_MAP:
        print(f"ERROR: Unknown role '{role}'. Valid roles: {', '.join(ROLE_MAP)}", file=sys.stderr)
        sys.exit(1)

    app_id_var, key_var = ROLE_MAP[role]
    app_id = os.environ.get(app_id_var)
    private_key = os.environ.get(key_var)

    if not app_id:
        print(f"ERROR: {app_id_var} is not set.", file=sys.stderr)
        sys.exit(1)
    if not private_key:
        print(f"ERROR: {key_var} is not set.", file=sys.stderr)
        sys.exit(1)

    # GHA secrets store literal \n — normalise to real newlines
    private_key = private_key.replace("\\n", "\n")

    target_repo = os.environ.get("TARGET_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    if not target_repo:
        print("ERROR: TARGET_REPO (or GITHUB_REPOSITORY) env var must be set.", file=sys.stderr)
        sys.exit(1)

    app_jwt = make_jwt(app_id, private_key)
    installation_id = get_installation_id(app_jwt, target_repo)
    token = get_installation_token(app_jwt, installation_id)

    # Print with no trailing newline — caller captures: TOKEN=$(python3 ...)
    print(token, end="")


if __name__ == "__main__":
    main()
