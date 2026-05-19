"""
Shared Pydantic models for the long-context evaluation dataset curation pipeline.

These models are used by scorer.py, harvester.py, and validator.py and are
serialized to JSON in evals/datasets/long-context/tasks.json.

The schema is backward-compatible with SWE-bench so existing tooling can be
reused with minimal adaptation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskType(str, Enum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    ARCHITECTURAL = "architectural"


# ---------------------------------------------------------------------------
# Stage 1 — Repo scoring
# ---------------------------------------------------------------------------


class RepoScores(BaseModel):
    """Individual signal scores for a candidate repository."""

    import_density: float = Field(
        ge=0.0,
        le=1.0,
        description="Cross-module import density (0–1, higher = more interconnected).",
    )
    multi_file_pr_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="Ratio of PRs touching ≥5 files across ≥2 modules.",
    )
    ci_complexity: float = Field(
        ge=0.0,
        le=1.0,
        description="CI configuration presence and job-count complexity (0–1).",
    )
    language_diversity: float = Field(
        ge=0.0,
        le=1.0,
        description="Non-primary language code proportion (0–1).",
    )
    composite: float = Field(
        ge=0.0,
        le=1.0,
        description="Weighted composite of the four signals.",
    )


class ScoredRepo(BaseModel):
    """A candidate repository after Stage 1 scoring."""

    repo_url: str = Field(description="HTTPS URL, e.g. https://github.com/org/repo.")
    full_name: str = Field(description="GitHub full name, e.g. org/repo.")
    primary_language: str = Field(description="Most-used language per GitHub linguist.")
    stars: int = Field(ge=0)
    scores: RepoScores
    baseline_tests_pass: bool = Field(
        description=(
            "True if the repo's test suite passes at the latest stable commit "
            "with no patch applied (two attempts). Repos where this is False are "
            "dropped before any task harvesting."
        )
    )
    selected: bool = Field(
        description="True if composite score is at or above the 70th-percentile threshold."
    )
    drop_reason: str | None = Field(
        default=None,
        description="Populated when selected=False or baseline_tests_pass=False.",
    )
    scored_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Stage 2 — Task harvesting
# ---------------------------------------------------------------------------


class LayerType(str, Enum):
    API = "api"
    CORE = "core"
    TEST = "test"
    CONFIG = "config"


class RawTask(BaseModel):
    """A candidate task extracted from a merged PR, before validation."""

    instance_id: str = Field(
        description="Unique identifier: {repo_full_name}__{issue_number}."
    )
    repo_url: str
    repo_full_name: str
    base_commit: str = Field(description="The commit SHA the PR was merged into.")
    pr_number: int
    pr_title: str
    pr_url: str
    issue_number: int | None = None
    task_description: str = Field(
        description="Derived from the linked issue body or PR description."
    )
    task_type: TaskType
    gold_patch: str = Field(description="Unified diff of the merged PR.")
    changed_files: list[str]
    changed_modules: list[str] = Field(
        description="Top-level module directories touched by the PR."
    )
    layers_touched: list[LayerType] = Field(
        description="Which architectural layers the changes span."
    )
    languages: list[str]
    temporal_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "{'pr_created': ISO date string, 'post_cutoff': bool}. "
            "Enables contamination-aware evaluation splits."
        ),
    )
    harvested_at: datetime = Field(default_factory=datetime.utcnow)

    # Hardness filter results — populated by harvester.py
    passes_file_count_gate: bool = False
    passes_layer_gate: bool = False
    passes_issue_linkage_gate: bool = False
    passes_leakage_gate: bool = False

    @property
    def passes_all_gates(self) -> bool:
        return (
            self.passes_file_count_gate
            and self.passes_layer_gate
            and self.passes_issue_linkage_gate
            and self.passes_leakage_gate
        )


# ---------------------------------------------------------------------------
# Stage 3 — Validation (populated by validator.py)
# ---------------------------------------------------------------------------


class DependencyGraph(BaseModel):
    nodes: list[str] = Field(description="File paths that are graph nodes.")
    edges: list[tuple[str, str]] = Field(
        description="Directed edges (importer, importee)."
    )


class SolvabilityProbe(BaseModel):
    partial_context_pass: bool = Field(
        description="True if the agent solved the task using only the directly-edited files."
    )
    full_context_pass: bool = Field(
        description="True if the agent solved the task with the full required_files context."
    )
    probe_model: str = Field(description="Model used for the probe, e.g. gemini-flash-2.5.")


class ValidatedTask(BaseModel):
    """
    A fully validated task instance ready for inclusion in tasks.json.

    Fields are a superset of RawTask, extended with long-context metadata
    produced by validator.py.
    """

    # Core identity (SWE-bench compatible)
    instance_id: str
    repo_url: str
    base_commit: str
    task_description: str
    task_type: TaskType
    gold_patch: str
    test_patch: str = Field(
        description="Shell command(s) to run the relevant tests, e.g. 'pytest tests/core/'."
    )

    # Long-context metadata
    required_files: list[str] = Field(
        description="Files the agent must read to solve the task (transitive dep traversal)."
    )
    dependency_graph: DependencyGraph
    min_context_tokens: int = Field(
        ge=0,
        description="Approximate token count of required_files contents.",
    )
    dependency_depth: int = Field(
        ge=0,
        description="Longest dependency chain from edited files to required_files.",
    )
    difficulty_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Composite of dependency_depth, min_context_tokens, and layer count.",
    )

    # Provenance
    languages: list[str]
    baseline_tests_pass: bool
    solvability_probe: SolvabilityProbe
    docker_image: str = Field(
        description="Pinned Docker image URI for the test environment."
    )
    temporal_metadata: dict[str, Any] = Field(default_factory=dict)
