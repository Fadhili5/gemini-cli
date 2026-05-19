# Long-Context Eval — Curation Pipeline

Python pipeline for building the long-context coding evaluation dataset
([issue #23316](https://github.com/google-gemini/gemini-cli/issues/23316)).

The pipeline runs in three stages:

| Stage                            | Script         | Input                      | Output                        |
| -------------------------------- | -------------- | -------------------------- | ----------------------------- |
| 1 — Score & select repos         | `scorer.py`    | `candidates.txt`           | `output/scored_repos.json`    |
| 2 — Harvest raw tasks            | `harvester.py` | `output/scored_repos.json` | `output/raw_tasks.json`       |
| 3 — Validate (long-context gate) | `validator.py` | `output/raw_tasks.json`    | `output/validated_tasks.json` |

## Setup

```bash
cd evals/datasets/curation
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export GITHUB_TOKEN=your_token_here
```

## Stage 1 — Repository Scoring

Create a `candidates.txt` file with one GitHub `org/repo` per line:

```
pallets/flask
psf/requests
encode/httpx
tiangolo/fastapi
```

Run the scorer:

```bash
python scorer.py --candidates candidates.txt --output output/scored_repos.json
```

Each repo is scored on four signals (weighted composite ≥ 70th percentile →
selected):

| Signal                | Weight | Description                                      |
| --------------------- | ------ | ------------------------------------------------ |
| `import_density`      | 35%    | Cross-module import density via Python AST       |
| `multi_file_pr_ratio` | 30%    | Ratio of PRs touching ≥5 files across ≥2 modules |
| `ci_complexity`       | 15%    | CI workflow presence and job count               |
| `language_diversity`  | 20%    | Non-primary language code proportion             |

**Baseline health gate:** Before scoring, the pipeline clones each repo and runs
its test suite (two attempts). Repos where the baseline fails are dropped
immediately and logged to `output/dropped_repos.json`. This prevents broken
environments from contaminating the dataset.

Flags:

```
--candidates   Path to newline-separated list of org/repo names (required)
--output       Output path (default: output/scored_repos.json)
--skip-baseline  Skip the baseline health check (dry-run mode)
```

## Stage 2 — Task Harvesting

```bash
python harvester.py --scored output/scored_repos.json --output output/raw_tasks.json
```

For each selected repo, mines merged PRs and applies four hardness gates in
sequence. A PR is dropped on the first gate it fails:

1. **File count gate** — ≥5 files changed across ≥2 distinct top-level modules
2. **Layer gate** — changes span ≥2 of: `api`, `core`, `test`, `config`
3. **Issue linkage gate** — PR references a GitHub issue or has a descriptive
   body
4. **Leakage gate** — issue description must not contain exact symbol names from
   the patch

Flags:

```
--scored    Path to scored_repos.json (default: output/scored_repos.json)
--output    Output path (default: output/raw_tasks.json)
--max-prs   Max merged PRs to inspect per repo (default: 200)
```

## Output files

All generated files are gitignored (`output/*.json`). Only `.gitkeep` is
committed to preserve the directory.

| File                          | Description                                                               |
| ----------------------------- | ------------------------------------------------------------------------- |
| `output/scored_repos.json`    | All scored repos with signals, composite score, and `selected` flag       |
| `output/dropped_repos.json`   | Repos dropped by the baseline health gate, with reasons                   |
| `output/raw_tasks.json`       | ~1,000 candidate tasks that passed all four hardness gates                |
| `output/validated_tasks.json` | 200–300 tasks with `solvability_probe` field (produced by `validator.py`) |

## Schema

See [`schema.py`](schema.py) for Pydantic model definitions. The final validated
task schema is documented in
[`../long-context/README.md`](../long-context/README.md).
