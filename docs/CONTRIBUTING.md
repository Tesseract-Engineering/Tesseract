# Contributing

## Branches

| Branch | What it is |
|---|---|
| `this-is-not-prod` | The development branch. Every PR targets it |
| `main` | The released branch. It changes only through a release PR from `this-is-not-prod` (`docs/RELEASING.md`) |
| `<component>-<topic>` | A feature branch, such as `graph-source-refs`, `forge-broker` or `ci-images` |

**Never name a branch `forge/<anything>`.** A branch called `forge` exists, and git cannot hold both `forge` and `forge/x`. The same applies to any existing branch name followed by a slash.

To stack a PR on another one that is still open, target the other PR's branch. GitHub retargets it to `this-is-not-prod` when the base merges and its branch is deleted.

## Merging

- PRs into `this-is-not-prod` are **squash-merged**. That keeps one commit per PR and drops throwaway commits, such as the ones that prove a CI check can fail. Use a merge commit instead only when the individual commits matter.
- Release PRs into `main` always use a **merge commit**, so `main`'s history shows each release.
- Branches are deleted after merging.

## Reviews

A PR needs one approval from a code owner of every component it touches. `.github/CODEOWNERS` says who owns what, and GitHub requests those reviews automatically. When you edit another component's files, keep the edit minimal and call it out in the PR description.

The PR template asks what the PR changes, why, how it was tested and what it leaves out. Write the description for the reviewers who own the code, not for yourself.

## CI

Every PR runs these checks (`.github/workflows/ci.yml`). They are **required**: a PR cannot merge until all of them pass.

| Check | What it does |
|---|---|
| `lint` | `ruff check .` over the whole repository |
| `test (3.11)`, `test (3.12)` | `pytest` on both supported Pythons, with every extra in `pyproject.toml` installed and an S3 bucket service for the `s3` tests |
| `neo4j` | `pytest -m neo4j` against a Neo4j service: the access-control tests |
| `images` | Validates the compose stack and builds every container image, without pushing |

The check names are an interface: branch protection requires them by name. Renaming a job is a breaking change, so update the ruleset in the same PR.

`stack.yml` (`compose`) runs the whole compose stack. It is not required. Add the `run-stack` label to run it on a PR. `docs/TESTING.md` covers where tests go, the service markers, and how to run everything locally.

The repository is public, so CI never sees real data or credentials. Sandbox runs stay manual.

## Adding a component

A new top-level component, such as `slackbot/`, `docs_addin/` or `forge/`, is built, linted and tested by the existing CI. Do **not** edit `.github/workflows/ci.yml` to add steps for it:

1. **Its dependencies** go in a new extra in `pyproject.toml` (`slackbot = [...]`), with a one-line comment saying what the component is. CI installs every extra it finds there, and so does `pip install -e '.[<extras>]'` locally.
2. **Its package** goes in `[tool.setuptools.packages.find] include` (`"slackbot*"`), so it is importable after an editable install.
3. **Its tests** go in `tests/<component>_tests/`, with an empty `__init__.py` and plain pytest. A script-style suite that already exists is wrapped, the way `tests/graph_tests/test_graph_script.py` wraps the graph's. Tests that need a service use a marker (`docs/TESTING.md`).
4. **Lint** is `ruff check .`, so the new directory is linted with no change.
5. **A container image**, if it has one: add its Dockerfile to the `images` job's list in `ci.yml` and to the matrix in `release.yml`. That list is the only workflow edit a component makes.
6. **Ownership:** add a line to `.github/CODEOWNERS` for the directory and its tests, and say in the PR who owns it.

If an open PR edits `ci.yml` only to install or run its own component, resolve the conflict by taking `this-is-not-prod`'s `ci.yml` and following the steps above.

## Dependencies

- **Tools are pinned exactly, and libraries use ranges.** `pytest` and `ruff` have exact pins in the `dev` extra, because a new release of either can fail every PR without a code change. Libraries keep compatible ranges.
- **One source of truth.** Every image installs its dependencies from an extra in `pyproject.toml`, never from a separate `requirements.txt`.
- **Dependabot** (`.github/dependabot.yml`) opens weekly PRs against `this-is-not-prod`. It covers Python packages (grouped into one PR), GitHub Actions, and the Dockerfiles' base images. Dependabot reads its configuration from the **default branch**, so it starts working once `this-is-not-prod` is the default branch or the file reaches `main`.
- **Compose image tags are checked by hand.** Dependabot does not cover the images named in `swarm/compose.yaml` (`rustfs/rustfs`, `neo4j`, `ubuntu/squid`). Once a month, or when one of them has a security release, compare each tag with the newest one on Docker Hub. To move one, update `swarm/compose.yaml` and the matching CI service (`neo4j` in `ci.yml` uses the same tag as compose), then run the stack with the `run-stack` label.

## Secrets

Never commit `.env`, `data/`, `var/`, tokens, private keys, or anything fetched from GitHub, Jira, Slack or Drive. Test fixtures are hand-made. GitGuardian and GitHub secret scanning both check pushes. If either flags a value that is only a throwaway test password (`ci-only-password`, `neo4j-local-dev`), mark it as a test credential rather than renaming it.
