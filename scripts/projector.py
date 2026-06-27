#!/usr/bin/env python3
"""Projector: reduce RunRecord JSONL -> board telemetry fields.

Three entry points:

  add_to_board(board_token, board_id, target_repo, issue_number)
      Called by agent-orchestrator.yml when a type:feature issue is opened.
      Adds the issue to the Projects v2 board.  Best-effort; never raises.

  project_provisional(board_token, board_id, issue_number, item_id, bindings_path)
      Called at run-end.  Reads all ops-metrics JSONL lines for the issue,
      reduces them, writes Outcome=Provisional plus telemetry numbers to the
      board.  Best-effort; never raises into the caller.

  settle(board_token, board_id, target_repo, pr_number, merged, bindings_path)
      Called by the settlement workflow on PR close.  Resolves the linked
      issue from the PR body, finds the board item, writes the final Outcome.

The pure core -- reduce_runs() -- is I/O-free and fully unit-testable.
The thin adapters (write_telemetry, settle) do GraphQL via GitHub Projects v2.

Environment variables:
  BOARD_ID      GitHub Projects v2 node ID (used when board_id is not passed
                explicitly and field-bindings.json does not contain it).
  TARGET_REPO   owner/repo of the target (product) repository.
  OPS_REPO      owner/repo of the ops repository (for JSONL corpus location).
"""

from __future__ import annotations

import json
import logging
import os
import re
import argparse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

OPS_ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = OPS_ROOT / "ops-metrics"

API = "https://api.github.com"

# ── Pure data type ─────────────────────────────────────────────────────────────

@dataclass
class TelemetryValues:
    """Reduced telemetry for one issue, ready to write to the board."""
    cost_to_date: float = 0.0
    attempts: int = 0
    turns: int = 0
    clean_exit: str = ""
    outcome: str = "Provisional"


# ── Pure reducer ──────────────────────────────────────────────────────────────

def reduce_runs(records: list[dict[str, Any]]) -> TelemetryValues:
    if not records:
        return TelemetryValues()

    cost_total = 0.0
    max_attempt = 0

    for rec in records:
        try:
            c = (rec.get("cost") or {}).get("total_cost_usd")
            if c is not None:
                cost_total += float(c)
        except (TypeError, ValueError):
            pass

        try:
            a = (rec.get("identity") or {}).get("attempt")
            if a is not None:
                max_attempt = max(max_attempt, int(a))
        except (TypeError, ValueError):
            pass

    def _ended_at(rec: dict) -> str:
        return (rec.get("lifecycle") or {}).get("ended_at") or ""

    latest = max(records, key=_ended_at)

    turns = 0
    try:
        turns = int((latest.get("execution") or {}).get("turns") or 0)
    except (TypeError, ValueError):
        pass

    clean_exit_raw = (latest.get("clean_exit") or {}).get("status") or ""
    _EXIT_MAP = {
        "clean":         "Clean",
        "crashed":       "Crashed",
        "max_turns":     "Max turns",
        "infra_failure": "Infra failure",
    }
    clean_exit = _EXIT_MAP.get(clean_exit_raw.lower(), clean_exit_raw.title() if clean_exit_raw else "")

    return TelemetryValues(
        cost_to_date=round(cost_total, 6),
        attempts=max_attempt,
        turns=turns,
        clean_exit=clean_exit,
        outcome="Provisional",
    )


# ── JSONL reader ──────────────────────────────────────────────────────────────

def _load_records_for_issue(issue_number: int, target_repo: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not METRICS_DIR.exists():
        return records

    for jsonl_file in sorted(METRICS_DIR.glob("runs-*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                identity = rec.get("identity") or {}
                if (
                    identity.get("number") == issue_number
                    and identity.get("repo") == target_repo
                ):
                    records.append(rec)
        except OSError:
            pass

    return records


# ── GraphQL helpers ───────────────────────────────────────────────────────────

ADD_ITEM_MUTATION = """
mutation AddItem($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {
    projectId: $projectId
    contentId: $contentId
  }) {
    item { id }
  }
}
"""

UPDATE_NUMBER_MUTATION = """
mutation UpdateNumber(
  $projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: { number: $value }
  }) { projectV2Item { id } }
}
"""

UPDATE_SELECT_MUTATION = """
mutation UpdateSelect(
  $projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!
) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) { projectV2Item { id } }
}
"""


def _graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ops-projector",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if body.get("errors"):
                LOGGER.warning("GraphQL errors: %s", body["errors"])
            return body
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        LOGGER.warning("GraphQL HTTP %s: %s", exc.code, err[:200])
    except Exception as exc:
        LOGGER.warning("GraphQL error: %s", exc)
    return {}


def _set_number(token: str, board_id: str, item_id: str, field_id: str, value: float) -> None:
    _graphql(token, UPDATE_NUMBER_MUTATION, {
        "projectId": board_id, "itemId": item_id, "fieldId": field_id, "value": value,
    })


def _set_single_select(token: str, board_id: str, item_id: str, field_id: str, option_id: str) -> None:
    if not option_id:
        return
    _graphql(token, UPDATE_SELECT_MUTATION, {
        "projectId": board_id, "itemId": item_id, "fieldId": field_id, "optionId": option_id,
    })


# ── Bindings loader ───────────────────────────────────────────────────────────

def _load_bindings(bindings_path: Path | None = None) -> dict:
    if bindings_path is None:
        bindings_path = OPS_ROOT / "field-bindings.json"
    if not bindings_path.exists():
        LOGGER.debug("field-bindings.json not found at %s; projector is a no-op", bindings_path)
        return {}
    try:
        return json.loads(bindings_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not load field-bindings.json: %s", exc)
        return {}


def _resolve_board_id(bindings: dict) -> str:
    from_bindings = (bindings.get("board_id") or "").strip()
    if from_bindings:
        return from_bindings
    return os.environ.get("BOARD_ID", "").strip()


# ── Board item lookup ─────────────────────────────────────────────────────────

def _find_item_id_for_issue(token: str, board_id: str, issue_number: int) -> str:
    query = """
    query FindItem($boardId: ID!, $cursor: String) {
      node(id: $boardId) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content { ... on Issue { number } }
            }
          }
        }
      }
    }
    """
    cursor = None
    while True:
        variables: dict[str, Any] = {"boardId": board_id}
        if cursor:
            variables["cursor"] = cursor
        resp = _graphql(token, query, variables)
        nodes = (
            (resp.get("data") or {}).get("node", {}).get("items", {}).get("nodes", [])
        ) or []
        for node in nodes:
            content = node.get("content") or {}
            if content.get("number") == issue_number:
                return node.get("id") or ""
        page_info = (
            (resp.get("data") or {}).get("node", {}).get("items", {}).get("pageInfo", {})
        ) or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return ""


# ── add_to_board ──────────────────────────────────────────────────────────────

def add_to_board(
    board_token: str,
    target_repo: str,
    issue_number: int,
    board_id: str | None = None,
    bindings_path: Path | None = None,
) -> str:
    """Add an issue to the Projects v2 board.

    Returns the new board item node ID, or empty string on failure.
    Best-effort: never raises into the caller.
    """
    try:
        if not board_id:
            bindings = _load_bindings(bindings_path)
            board_id = _resolve_board_id(bindings)
        if not board_id:
            LOGGER.warning("add_to_board: board_id not configured; skipping")
            return ""

        # Resolve issue node ID
        owner, repo_name = target_repo.split("/", 1)
        url = f"{API}/repos/{owner}/{repo_name}/issues/{issue_number}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {board_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ops-projector",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            issue_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        issue_node_id = issue_data.get("node_id") or ""
        if not issue_node_id:
            LOGGER.warning("add_to_board: could not resolve node_id for issue #%s", issue_number)
            return ""

        # Add to board
        result = _graphql(board_token, ADD_ITEM_MUTATION, {
            "projectId": board_id,
            "contentId": issue_node_id,
        })
        item_id = (
            (result.get("data") or {})
            .get("addProjectV2ItemById", {})
            .get("item", {})
            .get("id") or ""
        )
        if item_id:
            LOGGER.info("add_to_board: issue #%s added to board as item %s", issue_number, item_id)
        else:
            LOGGER.warning("add_to_board: addProjectV2ItemById returned no item ID")
        return item_id

    except Exception as exc:
        LOGGER.warning("add_to_board failed for issue #%s: %s", issue_number, exc)
        return ""


# ── Telemetry writer ──────────────────────────────────────────────────────────

def write_telemetry(
    board_token: str,
    board_id: str,
    item_id: str,
    values: TelemetryValues,
    bindings: dict,
) -> None:
    fields = bindings.get("fields", {})

    def _field_id(name: str) -> str:
        return (fields.get(name) or {}).get("id") or ""

    def _option_id(field_name: str, option_name: str) -> str:
        opts = (fields.get(field_name) or {}).get("options") or {}
        if option_name in opts:
            return opts[option_name]
        for k, v in opts.items():
            if k.lower() == option_name.lower():
                return v
        return ""

    for field_name, value in [
        ("Cost to date", values.cost_to_date),
        ("Turns", float(values.turns)),
        ("Attempts", float(values.attempts)),
    ]:
        fid = _field_id(field_name)
        if not fid:
            LOGGER.debug("No binding for field %r; skipping", field_name)
            continue
        try:
            _set_number(board_token, board_id, item_id, fid, value)
            LOGGER.info("Wrote %s=%s to item %s", field_name, value, item_id)
        except Exception as exc:
            LOGGER.warning("Failed to write %s: %s", field_name, exc)

    for field_name, option_name in [
        ("Outcome", values.outcome),
        ("Clean exit", values.clean_exit),
    ]:
        if not option_name:
            continue
        fid = _field_id(field_name)
        if not fid:
            LOGGER.debug("No binding for field %r; skipping", field_name)
            continue
        oid = _option_id(field_name, option_name)
        if not oid:
            LOGGER.warning("No option id for %r / %r; skipping", field_name, option_name)
            continue
        try:
            _set_single_select(board_token, board_id, item_id, fid, oid)
            LOGGER.info("Wrote %s=%r to item %s", field_name, option_name, item_id)
        except Exception as exc:
            LOGGER.warning("Failed to write %s: %s", field_name, exc)


# ── project_provisional ───────────────────────────────────────────────────────

def project_provisional(
    board_token: str,
    issue_number: int,
    item_id: str,
    bindings_path: Path | None = None,
    target_repo: str | None = None,
) -> None:
    try:
        bindings = _load_bindings(bindings_path)
        board_id = _resolve_board_id(bindings)
        if not board_id or not item_id or not bindings:
            return
        repo = target_repo or os.environ.get("TARGET_REPO", "")
        records = _load_records_for_issue(issue_number, repo)
        values = reduce_runs(records)
        write_telemetry(board_token, board_id, item_id, values, bindings)
        LOGGER.info(
            "Projected provisional telemetry for issue #%s: cost=%.4f attempts=%d turns=%d",
            issue_number, values.cost_to_date, values.attempts, values.turns,
        )
    except Exception as exc:
        LOGGER.warning("project_provisional failed for issue #%s: %s", issue_number, exc)


# ── settle ────────────────────────────────────────────────────────────────────

def _parse_linked_issue(pr_body: str) -> int | None:
    if not pr_body:
        return None
    m = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", pr_body, re.IGNORECASE)
    return int(m.group(1)) if m else None


def settle(
    board_token: str,
    pr_number: int,
    merged: bool,
    bindings_path: Path | None = None,
    target_repo: str | None = None,
) -> None:
    try:
        bindings = _load_bindings(bindings_path)
        board_id = _resolve_board_id(bindings)
        if not board_id or not bindings:
            return

        repo = target_repo or os.environ.get("TARGET_REPO", "")
        if not repo:
            LOGGER.warning("settle: TARGET_REPO not set")
            return

        req = urllib.request.Request(
            f"{API}/repos/{repo}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {board_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ops-projector",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                pr_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            LOGGER.warning("settle: could not fetch PR #%s: %s", pr_number, exc)
            return

        issue_number = _parse_linked_issue(pr_data.get("body") or "")
        if not issue_number:
            LOGGER.debug("settle: no linked issue in PR #%s body", pr_number)
            return

        item_id = _find_item_id_for_issue(board_token, board_id, issue_number)
        if not item_id:
            LOGGER.debug("settle: issue #%s not on board", issue_number)
            return

        outcome_name = "Merged" if merged else "Closed unmerged"
        fields = bindings.get("fields", {})
        outcome_field = fields.get("Outcome") or {}
        field_id = outcome_field.get("id") or ""
        option_id = (outcome_field.get("options") or {}).get(outcome_name) or ""
        if not field_id or not option_id:
            LOGGER.warning("settle: missing binding for Outcome/%r", outcome_name)
            return

        _set_single_select(board_token, board_id, item_id, field_id, option_id)
        LOGGER.info("settle: Outcome=%r on item %s (issue #%s, PR #%s)", outcome_name, item_id, issue_number, pr_number)
    except Exception as exc:
        LOGGER.warning("settle failed for PR #%s: %s", pr_number, exc)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _str_to_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}")


def _pr_payload(event_path: str | None) -> tuple[int | None, bool]:
    path = event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return None, False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None, False
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    merged = bool(pr.get("merged", False))
    return (int(number) if number is not None else None), merged


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Subcommands (via flags):
      --add-to-board --issue N   Add issue N to the Projects v2 board
      --settle --pr N            Write final Outcome when PR N closes
    """
    parser = argparse.ArgumentParser(description="GitHub Projects v2 board manager for AgentOS.")
    parser.add_argument("--add-to-board", action="store_true", help="Add an issue to the project board")
    parser.add_argument("--issue", type=int, default=None, help="Issue number (required for --add-to-board)")
    parser.add_argument("--settle", action="store_true", help="Run settlement for a closed PR")
    parser.add_argument("--pr", dest="pr_number", type=int, default=None, help="PR number (for --settle)")
    parser.add_argument("--merged", type=_str_to_bool, default=None, help="Whether PR was merged (for --settle)")
    parser.add_argument("--token", default=None, help="GitHub token (defaults to GITHUB_TOKEN env)")
    parser.add_argument("--event-path", default=None, help="Path to GHA event payload JSON")
    parser.add_argument("--repo", default=None, help="owner/repo (defaults to TARGET_REPO env)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    target_repo = args.repo or os.environ.get("TARGET_REPO", "")

    if args.add_to_board:
        if not args.issue:
            LOGGER.error("--issue is required with --add-to-board")
            return 1
        if not token:
            LOGGER.error("GITHUB_TOKEN is not set")
            return 1
        if not target_repo:
            LOGGER.error("TARGET_REPO is not set")
            return 1
        board_id = os.environ.get("BOARD_ID", "")
        item_id = add_to_board(token, target_repo, args.issue, board_id=board_id or None)
        return 0 if item_id else 1

    if args.settle:
        bindings = _load_bindings()
        board_id = _resolve_board_id(bindings)
        if not board_id:
            LOGGER.info("board_id not configured; skipping settlement")
            return 0

        pr_number, merged = _pr_payload(args.event_path)
        if args.pr_number is not None:
            pr_number = args.pr_number
        if args.merged is not None:
            merged = args.merged
        if not pr_number:
            LOGGER.warning("No PR number available; skipping settlement")
            return 0
        if not token:
            LOGGER.warning("No GitHub token available; skipping settlement")
            return 0

        settle(token, pr_number, merged, target_repo=target_repo)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
