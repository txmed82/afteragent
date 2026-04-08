from __future__ import annotations

import json
import subprocess
from pathlib import Path


PR_VIEW_FIELDS = ",".join(
    [
        "number",
        "title",
        "url",
        "createdAt",
        "updatedAt",
        "reviewDecision",
        "headRefOid",
        "headRefName",
        "baseRefName",
        "changedFiles",
        "commits",
        "files",
        "comments",
        "reviews",
    ]
)

CHECK_FIELDS = ",".join(
    [
        "bucket",
        "completedAt",
        "description",
        "event",
        "link",
        "name",
        "startedAt",
        "state",
        "workflow",
    ]
)

RUN_LIST_FIELDS = ",".join(
    [
        "attempt",
        "conclusion",
        "createdAt",
        "databaseId",
        "displayTitle",
        "event",
        "headBranch",
        "headSha",
        "name",
        "number",
        "startedAt",
        "status",
        "updatedAt",
        "url",
        "workflowDatabaseId",
        "workflowName",
    ]
)

RUN_VIEW_FIELDS = ",".join(
    [
        "attempt",
        "conclusion",
        "createdAt",
        "databaseId",
        "displayTitle",
        "event",
        "headBranch",
        "headSha",
        "jobs",
        "name",
        "number",
        "startedAt",
        "status",
        "updatedAt",
        "url",
        "workflowName",
    ]
)

REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 20) {
            nodes {
              id
              body
              createdAt
              url
              author {
                login
              }
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
""".strip()


def capture_github_context(
    cwd: Path,
    artifact_dir: Path,
    repo: str | None = None,
    pr_number: int | None = None,
) -> dict | None:
    repo_info = repo_identity(cwd, repo)
    if not repo_info:
        return None

    pr = pr_snapshot(cwd, pr_number, repo)
    if not pr or not pr.get("number"):
        return None

    owner, repo = repo_info["nameWithOwner"].split("/", 1)
    review_threads = fetch_review_threads(cwd, owner, repo, int(pr["number"]), repo_info["nameWithOwner"])
    checks = pr_checks(cwd, int(pr["number"]), repo_info["nameWithOwner"])
    ci_runs = fetch_workflow_runs(
        cwd, str(pr.get("headRefOid") or ""), artifact_dir, repo_info["nameWithOwner"]
    )

    snapshot = {
        "repo": repo_info["nameWithOwner"],
        "pr_number": pr["number"],
        "pr_title": pr.get("title"),
        "pr_url": pr.get("url"),
        "review_decision": pr.get("reviewDecision"),
        "created_at": pr.get("createdAt"),
        "updated_at": pr.get("updatedAt"),
        "head_sha": pr.get("headRefOid"),
        "head_ref": pr.get("headRefName"),
        "base_ref": pr.get("baseRefName"),
        "pr_changed_files": normalize_pr_files(pr.get("files", [])),
        "commit_history": normalize_commits(pr.get("commits", [])),
        "review_summary": {
            "issue_comment_count": len(pr.get("comments", []) or []),
            "review_count": len(pr.get("reviews", []) or []),
            "thread_count": len(review_threads),
            "unresolved_thread_count": len(
                [thread for thread in review_threads if not thread["is_resolved"]]
            ),
        },
        "review_threads": review_threads,
        "checks": normalize_checks(checks),
        "ci_runs": ci_runs,
    }
    (artifact_dir / "github_context.json").write_text(json.dumps(snapshot, indent=2))
    return snapshot


def repo_identity(cwd: Path, repo: str | None = None) -> dict | None:
    if repo:
        return {"nameWithOwner": repo}
    command = ["gh", "repo", "view", "--json", "nameWithOwner"]
    payload = gh_json(cwd, command)
    if isinstance(payload, dict) and payload.get("nameWithOwner"):
        return payload
    return None


def pr_snapshot(cwd: Path, pr_number: int | None = None, repo: str | None = None) -> dict | None:
    command = ["gh", "pr", "view"]
    if pr_number is not None:
        command.append(str(pr_number))
    command.extend(["--json", PR_VIEW_FIELDS])
    if repo:
        command.extend(["-R", repo])
    payload = gh_json(cwd, command)
    if isinstance(payload, dict) and payload.get("number"):
        return payload
    return None


def pr_checks(cwd: Path, pr_number: int, repo: str | None = None) -> list[dict]:
    command = ["gh", "pr", "checks", str(pr_number), "--json", CHECK_FIELDS]
    if repo:
        command.extend(["-R", repo])
    payload = gh_json(cwd, command)
    return payload if isinstance(payload, list) else []


def fetch_review_threads(
    cwd: Path,
    owner: str,
    repo: str,
    number: int,
    repo_name_with_owner: str | None = None,
) -> list[dict]:
    threads: list[dict] = []
    cursor: str | None = None
    while True:
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={number}",
        ]
        if repo_name_with_owner:
            command.extend(["-R", repo_name_with_owner])
        if cursor:
            command.extend(["-F", f"after={cursor}"])
        payload = gh_json(cwd, command)
        if not isinstance(payload, dict):
            break
        thread_data = (
            payload.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        nodes = thread_data.get("nodes", [])
        page_info = thread_data.get("pageInfo", {})
        for thread in nodes:
            threads.append(normalize_review_thread(thread))
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return threads


def fetch_workflow_runs(
    cwd: Path, head_sha: str, artifact_dir: Path, repo: str | None = None
) -> list[dict]:
    if not head_sha:
        return []

    command = ["gh", "run", "list", "--commit", head_sha, "--json", RUN_LIST_FIELDS, "-L", "20"]
    if repo:
        command.extend(["-R", repo])
    payload = gh_json(cwd, command)
    if not isinstance(payload, list):
        return []

    logs_dir = artifact_dir / "ci_logs"
    if payload:
        logs_dir.mkdir(parents=True, exist_ok=True)

    runs: list[dict] = []
    for run in payload:
        run_id = run.get("databaseId")
        detailed = None
        if run_id:
            detail_command = ["gh", "run", "view", str(run_id), "--json", RUN_VIEW_FIELDS]
            if repo:
                detail_command.extend(["-R", repo])
            detailed = gh_json(cwd, detail_command)
        run_payload = detailed if isinstance(detailed, dict) else run
        failed_log = ""
        if run_id:
            log_command = ["gh", "run", "view", str(run_id), "--log-failed"]
            if repo:
                log_command.extend(["-R", repo])
            failed_log = gh_text(cwd, log_command)
        log_artifact = None
        if failed_log:
            log_path = logs_dir / f"run_{run_id}.log"
            log_path.write_text(failed_log)
            log_artifact = str(Path("artifacts") / log_path.relative_to(artifact_dir.parent))
        runs.append(
            {
                "database_id": run_payload.get("databaseId") or run_id,
                "workflow_name": run_payload.get("workflowName") or run_payload.get("name"),
                "display_title": run_payload.get("displayTitle"),
                "status": run_payload.get("status"),
                "conclusion": run_payload.get("conclusion"),
                "event": run_payload.get("event"),
                "started_at": run_payload.get("startedAt"),
                "updated_at": run_payload.get("updatedAt"),
                "url": run_payload.get("url"),
                "jobs": normalize_jobs(run_payload.get("jobs", [])),
                "failed_log_artifact": log_artifact,
                "failed_log_excerpt": extract_log_excerpt(failed_log),
            }
        )
    return runs


def normalize_pr_files(files: list[dict]) -> list[str]:
    paths: list[str] = []
    for item in files or []:
        path = item.get("path") or item.get("file") or item.get("name")
        if path:
            paths.append(path)
    return sorted(set(paths))


def normalize_commits(commits: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in commits or []:
        commit = item.get("commit") if isinstance(item, dict) else None
        source = commit if isinstance(commit, dict) else item
        authors = source.get("authors") or []
        normalized.append(
            {
                "oid": source.get("oid"),
                "message_headline": source.get("messageHeadline") or source.get("message"),
                "committed_date": source.get("committedDate"),
                "authors": [
                    (author.get("user") or {}).get("login") or author.get("name")
                    for author in authors
                ],
            }
        )
    return normalized


def normalize_review_thread(thread: dict) -> dict:
    comments = thread.get("comments", {}).get("nodes", [])
    normalized_comments = [
        {
            "id": comment.get("id"),
            "author": (comment.get("author") or {}).get("login"),
            "created_at": comment.get("createdAt"),
            "url": comment.get("url"),
            "body": comment.get("body", ""),
        }
        for comment in comments
    ]
    latest_comment_at = max(
        [comment["created_at"] for comment in normalized_comments if comment.get("created_at")],
        default=None,
    )
    return {
        "id": thread.get("id"),
        "is_resolved": bool(thread.get("isResolved")),
        "is_outdated": bool(thread.get("isOutdated")),
        "path": thread.get("path"),
        "line": thread.get("line") or thread.get("originalLine"),
        "latest_comment_at": latest_comment_at,
        "comments": normalized_comments,
    }


def normalize_checks(checks: list[dict]) -> list[dict]:
    return [
        {
            "bucket": check.get("bucket"),
            "name": check.get("name"),
            "state": check.get("state"),
            "workflow": check.get("workflow"),
            "started_at": check.get("startedAt"),
            "completed_at": check.get("completedAt"),
            "link": check.get("link"),
            "description": check.get("description"),
        }
        for check in checks or []
    ]


def normalize_jobs(jobs: list[dict]) -> list[dict]:
    normalized = []
    for job in jobs or []:
        normalized.append(
            {
                "name": job.get("name"),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "started_at": job.get("startedAt"),
                "completed_at": job.get("completedAt"),
                "steps": [
                    {
                        "name": step.get("name"),
                        "status": step.get("status"),
                        "conclusion": step.get("conclusion"),
                        "number": step.get("number"),
                    }
                    for step in job.get("steps", []) or []
                ],
            }
        )
    return normalized


def extract_log_excerpt(log_text: str, limit: int = 12) -> list[str]:
    if not log_text:
        return []
    lines: list[str] = []
    for line in log_text.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ["error", "failed", "traceback", "assert"]):
            lines.append(line.strip())
        if len(lines) >= limit:
            break
    return lines


def gh_json(cwd: Path, command: list[str]) -> list | dict | None:
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def gh_text(cwd: Path, command: list[str]) -> str:
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout
