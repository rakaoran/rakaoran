#!/usr/bin/env python3
"""Update the profile README metrics block from GitHub GraphQL data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

START = "<!-- profile-metrics:start -->"
END = "<!-- profile-metrics:end -->"
API_URL = "https://api.github.com/graphql"
BAR_WIDTH = 20
LANG_LIMIT = 10

PROFILE_QUERY = """
query ProfileMetrics($login: String!, $from: DateTime!, $to: DateTime!, $repoCursor: String) {
  user(login: $login) {
    createdAt
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
    }
    mergedPullRequests: pullRequests(first: 1, states: [MERGED]) {
      totalCount
    }
    repositories(
      first: 100
      after: $repoCursor
      ownerAffiliations: OWNER
      privacy: PUBLIC
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        isFork
        stargazerCount
        languages(first: 100, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node {
              name
            }
          }
        }
      }
    }
  }
}
"""


COMMIT_QUERY = """
query CommitTotal($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
    }
  }
}
"""


def iso8601(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-readme-metrics",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API request failed: {error.code} {detail}") from error

    if payload.get("errors"):
        raise SystemExit(json.dumps(payload["errors"], indent=2))

    return payload["data"]


def collect_metrics(login: str, token: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    cursor = None
    stars = 0
    language_sizes: dict[str, int] = {}
    created_at = None
    commits_last_year = 0
    merged_pull_requests = 0

    while True:
        data = graphql(
            token,
            PROFILE_QUERY,
            {
                "login": login,
                "from": iso8601(since),
                "to": iso8601(now),
                "repoCursor": cursor,
            },
        )
        user = data.get("user")
        if not user:
            raise SystemExit(f"GitHub user not found: {login}")

        created_at = user["createdAt"]
        contributions = user["contributionsCollection"]
        commits_last_year = contributions["totalCommitContributions"]
        merged_pull_requests = user["mergedPullRequests"]["totalCount"]

        repositories = user["repositories"]
        for repo in repositories["nodes"]:
            if repo["isFork"]:
                continue

            stars += repo["stargazerCount"]
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                language_sizes[name] = language_sizes.get(name, 0) + edge["size"]

        page_info = repositories["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    if not created_at:
        raise SystemExit(f"Could not read account creation date for {login}")

    return {
        "stars": stars,
        "commits": collect_total_commits(login, token, created_at, now),
        "commits_last_year": commits_last_year,
        "merged_pull_requests": merged_pull_requests,
        "languages": language_sizes,
        "from": since,
        "to": now,
    }


def yearly_windows(created_at: str, now: datetime) -> list[tuple[datetime, datetime]]:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    windows = []
    start = created

    while start < now:
        end = min(datetime(start.year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
        windows.append((start, end))
        start = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)

    return windows


def collect_total_commits(
    login: str, token: str, created_at: str, now: datetime
) -> int:
    total = 0
    for start, end in yearly_windows(created_at, now):
        data = graphql(
            token,
            COMMIT_QUERY,
            {"login": login, "from": iso8601(start), "to": iso8601(end)},
        )
        total += data["user"]["contributionsCollection"]["totalCommitContributions"]
    return total


def fmt_int(value: int) -> str:
    return f"{value:,}"


def bar(percent: float) -> str:
    filled = round((percent / 100) * BAR_WIDTH)
    filled = max(0, min(BAR_WIDTH, filled))
    return "[" + ("█" * filled) + ("░" * (BAR_WIDTH - filled)) + "]"


def stat_block(metrics: dict[str, Any]) -> str:
    rows = [
        ("Stars", fmt_int(metrics["stars"])),
        ("Commits", fmt_int(metrics["commits"])),
        ("Commits (last 365d)", fmt_int(metrics["commits_last_year"])),
        ("Merged PRs", fmt_int(metrics["merged_pull_requests"])),
    ]
    label_width = max(len(label) for label, _ in rows)
    value_width = max(len(value) for _, value in rows)
    return "\n".join(
        f"{label:<{label_width}}  {value:>{value_width}}" for label, value in rows
    )


def language_block(language_sizes: dict[str, int]) -> str:
    total = sum(language_sizes.values())
    if total == 0:
        return f"none         {bar(0)}   0.00%"

    ordered = sorted(language_sizes.items(), key=lambda item: item[1], reverse=True)
    top_languages = ordered[:LANG_LIMIT]
    name_width = max(len(name) for name, _ in top_languages)
    rows = []
    for name, size in top_languages:
        percent = (size / total) * 100
        rows.append(f"{name:<{name_width}}  {bar(percent)} {percent:6.2f}%")
    return "\n".join(rows)


def render(metrics: dict[str, Any]) -> str:
    updated = metrics["to"].strftime("%Y-%m-%d")
    stats = escape(stat_block(metrics))
    languages = escape(language_block(metrics["languages"]))

    return f"""<!-- profile-metrics:start -->
<table>
<tr>
<td width="50%" valign="top">
<pre>
{stats}
</pre>
</td>
<td width="50%" valign="top">
<pre>
{languages}
</pre>
</td>
</tr>
</table>

<sub>Last updated: {updated}</sub>
<!-- profile-metrics:end -->"""


def replace_block(readme: Path, block: str) -> None:
    text = readme.read_text(encoding="utf-8")
    start = text.find(START)
    end = text.find(END)
    if start == -1 or end == -1 or start > end:
        raise SystemExit(f"Could not find metrics markers in {readme}")

    end += len(END)
    updated = text[:start] + block + text[end:]
    readme.write_text(updated, encoding="utf-8")


def infer_login() -> str | None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repository:
        return repository.split("/", 1)[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user", default=os.environ.get("GITHUB_USERNAME") or infer_login()
    )
    parser.add_argument("--readme", default="README.md")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not args.user:
        raise SystemExit("Missing GitHub username. Pass --user or set GITHUB_USERNAME.")
    if not token:
        raise SystemExit("Missing GitHub token. Set GITHUB_TOKEN or GH_TOKEN.")

    metrics = collect_metrics(args.user, token)
    replace_block(Path(args.readme), render(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
