"""
Stage 1 — Repository scoring and selection.

Scores candidate repositories across four signals and applies a baseline
health gate before any task harvesting begins. Repos that fail the baseline
(test suite broken at HEAD) are dropped immediately and logged separately.

Usage:
    python scorer.py --candidates candidates.txt --output output/scored_repos.json

    GITHUB_TOKEN environment variable must be set.

candidates.txt is a newline-separated list of GitHub full names, e.g.:
    pallets/flask
    psf/requests
    encode/httpx
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from schema import RepoScores, ScoredRepo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
SELECTION_PERCENTILE = 70  # repos at or above this composite percentile are selected

# Signal weights — must sum to 1.0
WEIGHTS = {
    "import_density": 0.35,
    "multi_file_pr_ratio": 0.30,
    "ci_complexity": 0.15,
    "language_diversity": 0.20,
}

# Target language mix for final 30-50 repo selection (post-filter)
TARGET_LANGUAGE_MIN = {
    "Python": 10,
    "TypeScript": 6,
    "JavaScript": 2,
    "Go": 6,
    "Java": 2,
    "Kotlin": 2,
    "Rust": 2,
    "C++": 2,
}

BASELINE_TIMEOUT_SECONDS = 300  # 5 min per test attempt
BASELINE_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# GitHub API client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin wrapper around the GitHub REST API with rate-limit backoff."""

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
        """GET request with automatic rate-limit retry (exponential backoff)."""
        url = f"{GITHUB_API}{path}"
        for attempt in range(6):
            response = self._session.get(url, params=params)

            if response.status_code == 200:
                return response.json()

            if response.status_code == 403:
                reset_at = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_at - time.time(), 1)
                logger.warning("Rate limited. Waiting %.0fs before retry %d.", wait, attempt + 1)
                time.sleep(wait)
                continue

            if response.status_code == 404:
                logger.debug("404 for %s", url)
                return None

            backoff = 2**attempt
            logger.warning(
                "HTTP %d for %s. Retrying in %ds (attempt %d).",
                response.status_code,
                url,
                backoff,
                attempt + 1,
            )
            time.sleep(backoff)

        response.raise_for_status()

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Collect all pages from a paginated endpoint."""
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
# Baseline health gate
# ---------------------------------------------------------------------------


def _detect_test_command(repo_dir: Path) -> str | None:
    """Heuristically detect the test command for a cloned repository."""
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "pyproject.toml").exists():
        return "pytest --tb=no -q"
    if (repo_dir / "setup.py").exists() or (repo_dir / "tox.ini").exists():
        return "python -m pytest --tb=no -q"
    if (repo_dir / "package.json").exists():
        return "npm test"
    if (repo_dir / "go.mod").exists():
        return "go test ./..."
    if (repo_dir / "Cargo.toml").exists():
        return "cargo test"
    if (repo_dir / "pom.xml").exists():
        return "mvn test -q"
    return None


def check_baseline_health(repo_full_name: str) -> tuple[bool, str]:
    """
    Clone the repo at the latest stable commit and run its test suite twice.
    Returns (passes, reason_if_fails).

    A repo is considered healthy only if both attempts exit with code 0.
    """
    clone_url = f"https://github.com/{repo_full_name}.git"

    for attempt in range(1, BASELINE_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(prefix="gemini-lc-baseline-") as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            logger.info("Baseline attempt %d/%d for %s", attempt, BASELINE_ATTEMPTS, repo_full_name)

            # Shallow clone — only latest commit needed for baseline
            clone_result = subprocess.run(
                ["git", "clone", "--depth=1", "--filter=blob:none", clone_url, str(repo_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if clone_result.returncode != 0:
                reason = f"git clone failed: {clone_result.stderr[:200]}"
                logger.warning("Baseline %s attempt %d: %s", repo_full_name, attempt, reason)
                if attempt == BASELINE_ATTEMPTS:
                    return False, reason
                continue

            test_cmd = _detect_test_command(repo_dir)
            if test_cmd is None:
                return False, "Could not detect test command."

            test_result = subprocess.run(
                test_cmd,
                shell=True,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=BASELINE_TIMEOUT_SECONDS,
            )
            if test_result.returncode != 0:
                reason = f"Test suite failed (exit {test_result.returncode}): {test_result.stdout[-300:]}"
                logger.warning("Baseline %s attempt %d: %s", repo_full_name, attempt, reason)
                if attempt == BASELINE_ATTEMPTS:
                    return False, reason
                continue

            logger.info("Baseline %s attempt %d: PASS", repo_full_name, attempt)

    return True, ""


# ---------------------------------------------------------------------------
# Signal scorers
# ---------------------------------------------------------------------------


def score_import_density(repo_full_name: str, client: GitHubClient) -> float:
    """
    Cross-module import density: ratio of files that import from another
    top-level module to total Python files, sampled from the default branch.

    Uses the GitHub trees API to list .py files, then fetches a sample of
    up to 50 files and parses their imports with the ast module.
    """
    repo_data = client.get(f"/repos/{repo_full_name}")
    if not repo_data:
        return 0.0

    default_branch = repo_data.get("default_branch", "main")
    tree = client.get(
        f"/repos/{repo_full_name}/git/trees/{default_branch}",
        params={"recursive": "1"},
    )
    if not tree or "tree" not in tree:
        return 0.0

    py_files = [
        item["path"]
        for item in tree["tree"]
        if item["type"] == "blob" and item["path"].endswith(".py")
    ]

    if not py_files:
        return 0.0

    # Sample up to 50 files to stay within rate limits
    sample = py_files[:50]
    top_modules = {p.split("/")[0] for p in py_files if "/" in p}

    cross_module_count = 0
    parsed_count = 0

    for file_path in sample:
        content_data = client.get(
            f"/repos/{repo_full_name}/contents/{file_path}",
            params={"ref": default_branch},
        )
        if not content_data or content_data.get("encoding") != "base64":
            continue

        try:
            source = base64.b64decode(content_data["content"]).decode("utf-8", errors="replace")
            tree_ast = ast.parse(source, filename=file_path)
        except SyntaxError:
            continue

        parsed_count += 1
        for node in ast.walk(tree_ast):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in top_modules:
                    cross_module_count += 1
                    break  # count once per file

    if parsed_count == 0:
        return 0.0

    return min(cross_module_count / parsed_count, 1.0)


def score_multi_file_pr_ratio(repo_full_name: str, client: GitHubClient) -> float:
    """
    Ratio of merged PRs touching ≥5 files across ≥2 distinct top-level
    modules to total merged PRs (sample of last 100).
    """
    prs = client.get(
        f"/repos/{repo_full_name}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100},
    )
    if not prs:
        return 0.0

    merged_prs = [pr for pr in prs if pr.get("merged_at")]
    if not merged_prs:
        return 0.0

    qualifying = 0
    for pr in merged_prs[:50]:  # sample 50 merged PRs
        files = client.get(f"/repos/{repo_full_name}/pulls/{pr['number']}/files")
        if not files:
            continue
        if len(files) < 5:
            continue
        modules = {f["filename"].split("/")[0] for f in files}
        if len(modules) >= 2:
            qualifying += 1

    return min(qualifying / len(merged_prs[:50]), 1.0)


def score_ci_complexity(repo_full_name: str, client: GitHubClient) -> float:
    """
    CI configuration complexity: presence of .github/workflows/ files,
    scored by total job count across all workflow files (capped at 1.0).

    A repo with no CI scores 0. A repo with 20+ total jobs scores 1.0.
    """
    contents = client.get(f"/repos/{repo_full_name}/contents/.github/workflows")
    if not contents or not isinstance(contents, list):
        return 0.0

    workflow_files = [f for f in contents if f["name"].endswith((".yml", ".yaml"))]
    if not workflow_files:
        return 0.0

    total_jobs = 0
    for wf in workflow_files[:10]:  # cap to avoid excessive API calls
        wf_content = client.get(f"/repos/{repo_full_name}/contents/{wf['path']}")
        if not wf_content or wf_content.get("encoding") != "base64":
            continue
        raw = base64.b64decode(wf_content["content"]).decode("utf-8", errors="replace")
        # Count "jobs:" sections as a proxy for job count
        total_jobs += len(re.findall(r"^\s{0,2}jobs\s*:", raw, re.MULTILINE))
        # Count individual job definitions (keys under jobs:)
        total_jobs += len(re.findall(r"^\s{2,4}\w[\w-]*\s*:", raw, re.MULTILINE))

    # Normalise: 20 total jobs = score of 1.0
    return min(total_jobs / 20, 1.0)


def score_language_diversity(repo_full_name: str, client: GitHubClient) -> float:
    """
    Non-primary language proportion from the GitHub linguist API.
    A repo that is 100% its primary language scores 0.
    A repo where 50%+ of code is in other languages scores 1.0.
    """
    languages = client.get(f"/repos/{repo_full_name}/languages")
    if not languages or len(languages) < 2:
        return 0.0

    total_bytes = sum(languages.values())
    if total_bytes == 0:
        return 0.0

    primary_bytes = max(languages.values())
    non_primary_ratio = 1.0 - (primary_bytes / total_bytes)

    # Normalise: 50% non-primary = score 1.0
    return min(non_primary_ratio / 0.5, 1.0)


# ---------------------------------------------------------------------------
# Composite scorer
# ---------------------------------------------------------------------------


def score_repo(repo_full_name: str, client: GitHubClient) -> RepoScores:
    """Compute all four signals and the weighted composite."""
    logger.info("Scoring %s...", repo_full_name)

    import_density = score_import_density(repo_full_name, client)
    multi_file_pr_ratio = score_multi_file_pr_ratio(repo_full_name, client)
    ci_complexity = score_ci_complexity(repo_full_name, client)
    language_diversity = score_language_diversity(repo_full_name, client)

    composite = (
        WEIGHTS["import_density"] * import_density
        + WEIGHTS["multi_file_pr_ratio"] * multi_file_pr_ratio
        + WEIGHTS["ci_complexity"] * ci_complexity
        + WEIGHTS["language_diversity"] * language_diversity
    )

    return RepoScores(
        import_density=import_density,
        multi_file_pr_ratio=multi_file_pr_ratio,
        ci_complexity=ci_complexity,
        language_diversity=language_diversity,
        composite=composite,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run(candidates: list[str], output_path: Path, skip_baseline: bool = False) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN environment variable is not set.")

    client = GitHubClient(token)
    scored: list[ScoredRepo] = []
    dropped: list[dict[str, Any]] = []

    for full_name in candidates:
        full_name = full_name.strip()
        if not full_name or full_name.startswith("#"):
            continue

        repo_data = client.get(f"/repos/{full_name}")
        if not repo_data:
            logger.warning("Repo not found: %s — skipping.", full_name)
            continue

        # --- Baseline health gate (runs first, before any scoring) ---
        if skip_baseline:
            baseline_pass, baseline_reason = True, ""
            logger.info("Skipping baseline health check for %s (--skip-baseline set).", full_name)
        else:
            baseline_pass, baseline_reason = check_baseline_health(full_name)

        if not baseline_pass:
            entry = {
                "repo": full_name,
                "reason": baseline_reason,
                "dropped_at": datetime.utcnow().isoformat(),
            }
            dropped.append(entry)
            logger.warning("DROPPED %s: %s", full_name, baseline_reason)
            scored.append(
                ScoredRepo(
                    repo_url=f"https://github.com/{full_name}",
                    full_name=full_name,
                    primary_language=repo_data.get("language") or "Unknown",
                    stars=repo_data.get("stargazers_count", 0),
                    scores=RepoScores(
                        import_density=0.0,
                        multi_file_pr_ratio=0.0,
                        ci_complexity=0.0,
                        language_diversity=0.0,
                        composite=0.0,
                    ),
                    baseline_tests_pass=False,
                    selected=False,
                    drop_reason=baseline_reason,
                )
            )
            continue

        # --- Score signals ---
        try:
            repo_scores = score_repo(full_name, client)
        except Exception as exc:
            logger.error("Failed to score %s: %s — skipping.", full_name, exc)
            continue

        scored.append(
            ScoredRepo(
                repo_url=f"https://github.com/{full_name}",
                full_name=full_name,
                primary_language=repo_data.get("language") or "Unknown",
                stars=repo_data.get("stargazers_count", 0),
                scores=repo_scores,
                baseline_tests_pass=True,
                selected=False,  # set below after percentile calculation
            )
        )

    if not scored:
        logger.error("No repos were successfully scored.")
        return

    # --- Apply 70th-percentile threshold ---
    healthy = [r for r in scored if r.baseline_tests_pass]
    if healthy:
        composites = sorted(r.scores.composite for r in healthy)
        threshold_idx = int(len(composites) * SELECTION_PERCENTILE / 100)
        threshold = composites[min(threshold_idx, len(composites) - 1)]
        logger.info(
            "70th-percentile threshold: %.3f (over %d healthy repos)", threshold, len(healthy)
        )
    else:
        threshold = 0.0

    for repo in scored:
        if repo.baseline_tests_pass and repo.scores.composite >= threshold:
            repo.selected = True

    # --- Write outputs ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(
            [json.loads(r.model_dump_json()) for r in scored],
            f,
            indent=2,
            default=str,
        )
    logger.info("Wrote %d scored repos to %s", len(scored), output_path)

    dropped_path = output_path.parent / "dropped_repos.json"
    with open(dropped_path, "w") as f:
        json.dump(dropped, f, indent=2)
    logger.info("Wrote %d dropped repos to %s", len(dropped), dropped_path)

    selected = [r for r in scored if r.selected]
    logger.info(
        "Selected %d / %d repos (threshold=%.3f)", len(selected), len(healthy), threshold
    )

    # --- Language mix summary ---
    lang_counts: dict[str, int] = {}
    for repo in selected:
        lang_counts[repo.primary_language] = lang_counts.get(repo.primary_language, 0) + 1
    logger.info("Language mix of selected repos: %s", lang_counts)

    # Warn if target minimums are not met
    for lang, minimum in TARGET_LANGUAGE_MIN.items():
        count = lang_counts.get(lang, 0)
        if count < minimum:
            logger.warning(
                "Language target not met: need %d %s repos, have %d.", minimum, lang, count
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Score and select candidate repositories.")
    parser.add_argument(
        "--candidates",
        required=True,
        help="Path to a newline-separated list of GitHub full names (org/repo).",
    )
    parser.add_argument(
        "--output",
        default="output/scored_repos.json",
        help="Path to write the scored repos JSON (default: output/scored_repos.json).",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the baseline health check (useful for dry runs).",
    )
    args = parser.parse_args()

    candidates_path = Path(args.candidates)
    if not candidates_path.exists():
        parser.error(f"Candidates file not found: {candidates_path}")

    candidates = candidates_path.read_text().splitlines()
    run(candidates, Path(args.output), skip_baseline=args.skip_baseline)


if __name__ == "__main__":
    main()
