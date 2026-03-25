"""
Stage 2 — Task harvesting from git history.

For each selected repository in scored_repos.json, mines merged PRs and
applies a four-gate hardness filter. Passing candidates are stored as
RawTask objects in output/raw_tasks.json.

Usage:
    python harvester.py --scored output/scored_repos.json --output output/raw_tasks.json

    GITHUB_TOKEN environment variable must be set.

Gates (applied in sequence — task is dropped on first failure):
    1. File count gate:   ≥5 files changed across ≥2 distinct top-level modules
    2. Layer gate:        changes span ≥2 of: api, core, test, config
    3. Issue linkage:     PR references a GitHub issue or has a descriptive body
    4. Leakage gate:      issue description must not contain gold-patch symbol names
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from schema import LayerType, RawTask, ScoredRepo, TaskType

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Keywords that suggest a PR is a meaningful engineering task
TASK_KEYWORDS = re.compile(
    r"\b(fix|resolves?|closes?|refactor|implement|architect|redesign|migrat|replac)\b",
    re.IGNORECASE,
)

# Heuristic path → layer classification
LAYER_PATTERNS: list[tuple[re.Pattern[str], LayerType]] = [
    (re.compile(r"(^|/)test[s]?/|_test\.(py|ts|go|java|rs)$|\.spec\.(ts|js)$", re.I), LayerType.TEST),
    (re.compile(r"(^|/)(api|router|route[s]?|handler[s]?|endpoint[s]?|view[s]?)/", re.I), LayerType.API),
    (re.compile(r"(^|/)(\.(github|circleci)|Dockerfile|docker-compose|Makefile|setup\.py|pyproject\.toml|package\.json|go\.mod|Cargo\.toml)", re.I), LayerType.CONFIG),
]

MODEL_CUTOFF = datetime(2024, 4, 1, tzinfo=timezone.utc)  # conservative cutoff for contamination splits


# ---------------------------------------------------------------------------
# GitHub API client (mirrors scorer.py — intentionally kept self-contained)
# ---------------------------------------------------------------------------


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{GITHUB_API}{path}"
        for attempt in range(6):
            resp = self._session.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 403:
                reset_at = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_at - time.time(), 1)
                logger.warning("Rate limited. Waiting %.0fs.", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            time.sleep(2**attempt)
        return None

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results: list[Any] = []
        page = 1
        while True:
            params["page"] = page
            page_data = self.get(path, params)
            if not page_data:
                break
            results.extend(page_data)
            if len(page_data) < params["per_page"]:
                break
            page += 1
        return results


# ---------------------------------------------------------------------------
# Layer classification
# ---------------------------------------------------------------------------


def classify_layer(file_path: str) -> LayerType:
    """Map a file path to one of the four architectural layer types."""
    for pattern, layer in LAYER_PATTERNS:
        if pattern.search(file_path):
            return layer
    return LayerType.CORE


def get_layers_touched(files: list[dict[str, Any]]) -> list[LayerType]:
    return list({classify_layer(f["filename"]) for f in files})


# ---------------------------------------------------------------------------
# Hardness gates
# ---------------------------------------------------------------------------


def passes_file_count_gate(files: list[dict[str, Any]]) -> bool:
    """Gate 1: ≥5 files changed across ≥2 distinct top-level modules."""
    if len(files) < 5:
        return False
    modules = {f["filename"].split("/")[0] for f in files}
    return len(modules) >= 2


def passes_layer_gate(files: list[dict[str, Any]]) -> bool:
    """Gate 2: changes span ≥2 architectural layers."""
    layers = get_layers_touched(files)
    return len(layers) >= 2


def passes_issue_linkage_gate(pr: dict[str, Any]) -> bool:
    """
    Gate 3: PR references a GitHub issue or has a descriptive body.
    Checks the PR body for closing keywords or issue references.
    """
    body = (pr.get("body") or "").lower()
    if not body or len(body) < 30:
        return False
    if re.search(r"(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+", body, re.I):
        return True
    if re.search(r"#\d{2,}", body):  # bare issue reference
        return True
    if TASK_KEYWORDS.search(body):
        return True
    return False


def passes_leakage_gate(issue_body: str | None, gold_patch: str) -> bool:
    """
    Gate 4: the issue description must not contain exact symbol names
    from the gold patch (prevents trivial look-up tasks).

    Extracts function/class/variable names from the patch's + lines and
    checks if any appear verbatim in the issue body.
    """
    if not issue_body:
        return True  # no issue body to leak from

    # Extract symbols from added lines in the patch
    added_lines = [line[1:] for line in gold_patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    symbol_pattern = re.compile(r"\b(def |class |func |fn |function )\s*(\w+)", re.I)
    symbols = {m.group(2) for line in added_lines for m in symbol_pattern.finditer(line)}

    if not symbols:
        return True  # no named symbols in the patch — can't leak

    issue_lower = issue_body.lower()
    for symbol in symbols:
        if len(symbol) > 4 and symbol.lower() in issue_lower:
            logger.debug("Leakage detected: symbol '%s' found in issue body.", symbol)
            return False

    return True


# ---------------------------------------------------------------------------
# Task type inference
# ---------------------------------------------------------------------------


def infer_task_type(pr_title: str, pr_body: str) -> TaskType:
    """Infer the task type from the PR title and body."""
    text = f"{pr_title} {pr_body}".lower()
    if re.search(r"\b(fix|bug|error|crash|regression|incorrect|wrong)\b", text):
        return TaskType.BUGFIX
    if re.search(r"\b(refactor|clean|reorgani[sz]|restructure|simplif)\b", text):
        return TaskType.REFACTOR
    if re.search(r"\b(architect|redesign|migrat|overhaul|rewrite)\b", text):
        return TaskType.ARCHITECTURAL
    return TaskType.FEATURE


# ---------------------------------------------------------------------------
# Patch retrieval
# ---------------------------------------------------------------------------


def get_pr_patch(pr: dict[str, Any], client: GitHubClient) -> str:
    """Retrieve the full unified diff for a PR via the compare API."""
    files = client.get(
        f"/repos/{pr['base']['repo']['full_name']}/pulls/{pr['number']}/files"
    )
    if not files:
        return ""
    patches = []
    for f in files:
        if "patch" in f:
            patches.append(
                f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f['patch']}"
            )
    return "\n".join(patches)


# ---------------------------------------------------------------------------
# Main harvesting loop
# ---------------------------------------------------------------------------


def harvest_repo(
    repo: ScoredRepo,
    client: GitHubClient,
    max_prs: int = 200,
) -> list[RawTask]:
    """Extract qualifying raw tasks from a single repository."""
    full_name = repo.full_name
    logger.info("Harvesting %s...", full_name)

    prs = client.get(
        f"/repos/{full_name}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": min(max_prs, 100)},
    )
    if not prs:
        logger.warning("No PRs found for %s.", full_name)
        return []

    merged_prs = [pr for pr in prs if pr.get("merged_at")]
    logger.info("Found %d merged PRs for %s.", len(merged_prs), full_name)

    tasks: list[RawTask] = []

    for pr in merged_prs:
        pr_number = pr["number"]

        # Fetch file list
        files = client.get(f"/repos/{full_name}/pulls/{pr_number}/files")
        if not files:
            continue

        # Gate 1
        if not passes_file_count_gate(files):
            logger.debug("PR #%d dropped: file count gate.", pr_number)
            continue

        # Gate 2
        if not passes_layer_gate(files):
            logger.debug("PR #%d dropped: layer gate.", pr_number)
            continue

        # Gate 3
        if not passes_issue_linkage_gate(pr):
            logger.debug("PR #%d dropped: issue linkage gate.", pr_number)
            continue

        # Build gold patch
        gold_patch = get_pr_patch(pr, client)
        if not gold_patch:
            logger.debug("PR #%d dropped: empty patch.", pr_number)
            continue

        # Resolve issue body for leakage check and task description
        issue_number: int | None = None
        issue_body: str | None = None
        body = pr.get("body") or ""
        issue_match = re.search(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", body, re.I)
        if not issue_match:
            issue_match = re.search(r"#(\d+)", body)
        if issue_match:
            issue_number = int(issue_match.group(1))
            issue_data = client.get(f"/repos/{full_name}/issues/{issue_number}")
            if issue_data:
                issue_body = issue_data.get("body") or ""

        # Gate 4
        if not passes_leakage_gate(issue_body, gold_patch):
            logger.debug("PR #%d dropped: leakage gate.", pr_number)
            continue

        # Build task description from issue or PR body
        task_description = (
            issue_body.strip()
            if issue_body and len(issue_body) > 50
            else body.strip()
        )
        if not task_description:
            task_description = pr["title"]

        # Determine base commit (the commit the PR was merged into)
        base_commit = pr["merge_commit_sha"] or pr["base"]["sha"]

        # Temporal metadata
        pr_created = datetime.fromisoformat(
            pr["created_at"].replace("Z", "+00:00")
        )
        temporal = {
            "pr_created": pr["created_at"],
            "post_cutoff": pr_created > MODEL_CUTOFF,
        }

        # Language detection from file extensions
        ext_map = {
            ".py": "python", ".ts": "typescript", ".tsx": "typescript",
            ".js": "javascript", ".go": "go", ".rs": "rust",
            ".java": "java", ".kt": "kotlin", ".cpp": "c++", ".c": "c",
        }
        langs = list({
            ext_map[Path(f["filename"]).suffix]
            for f in files
            if Path(f["filename"]).suffix in ext_map
        })

        changed_files = [f["filename"] for f in files]
        changed_modules = list({f.split("/")[0] for f in changed_files})
        layers = get_layers_touched(files)

        instance_id = f"{full_name.replace('/', '__')}__{pr_number}"

        task = RawTask(
            instance_id=instance_id,
            repo_url=repo.repo_url,
            repo_full_name=full_name,
            base_commit=base_commit,
            pr_number=pr_number,
            pr_title=pr["title"],
            pr_url=pr["html_url"],
            issue_number=issue_number,
            task_description=task_description,
            task_type=infer_task_type(pr["title"], body),
            gold_patch=gold_patch,
            changed_files=changed_files,
            changed_modules=changed_modules,
            layers_touched=layers,
            languages=langs if langs else [repo.primary_language.lower()],
            temporal_metadata=temporal,
            passes_file_count_gate=True,
            passes_layer_gate=True,
            passes_issue_linkage_gate=True,
            passes_leakage_gate=True,
        )
        tasks.append(task)
        logger.debug("PR #%d accepted as task %s.", pr_number, instance_id)

    logger.info("Harvested %d tasks from %s.", len(tasks), full_name)
    return tasks


def run(scored_path: Path, output_path: Path, max_prs_per_repo: int = 200) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN environment variable is not set.")

    client = GitHubClient(token)

    with open(scored_path) as f:
        scored_data = json.load(f)

    repos = [ScoredRepo.model_validate(r) for r in scored_data]
    selected = [r for r in repos if r.selected]
    logger.info("Harvesting from %d selected repos.", len(selected))

    all_tasks: list[RawTask] = []
    for repo in selected:
        try:
            tasks = harvest_repo(repo, client, max_prs=max_prs_per_repo)
            all_tasks.extend(tasks)
        except Exception as exc:
            logger.error("Failed to harvest %s: %s", repo.full_name, exc)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(
            [json.loads(t.model_dump_json()) for t in all_tasks],
            f,
            indent=2,
            default=str,
        )

    logger.info(
        "Wrote %d raw tasks from %d repos to %s.",
        len(all_tasks),
        len(selected),
        output_path,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Harvest raw tasks from selected repositories.")
    parser.add_argument(
        "--scored",
        default="output/scored_repos.json",
        help="Path to scored_repos.json from scorer.py.",
    )
    parser.add_argument(
        "--output",
        default="output/raw_tasks.json",
        help="Path to write raw_tasks.json.",
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=200,
        help="Maximum merged PRs to inspect per repo (default: 200).",
    )
    args = parser.parse_args()
    run(Path(args.scored), Path(args.output), max_prs_per_repo=args.max_prs)


if __name__ == "__main__":
    main()
