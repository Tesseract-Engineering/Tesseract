# Testing

One command runs every component's tests: `pytest`. CI runs exactly that, so a component that follows the layout below is tested in CI without anyone editing the workflow.

## Where tests go

| What | Where |
|---|---|
| A component's tests | `tests/<component>_tests/` (`tests/swarm_tests/`, `tests/graph_tests/`, `tests/forge_tests/`...), with an empty `__init__.py` |
| Hand-made fixtures | `tests/fixtures/<component>/` |
| Fixtures shared across components | `tests/conftest.py` |

New tests are plain pytest. A component that already has a script-style suite is **wrapped**, not rewritten. `tests/graph_tests/test_graph_script.py` runs `python -m graph.test_build_swarm` and fails when the script exits non-zero, with the script's output in the failure. Converting a wrapped suite to native pytest is up to its owner.

## Tests that need a service

Tests that need a running service carry a marker, and **skip themselves** when the service is not configured:

| Marker | Needs | Locally |
|---|---|---|
| `s3` | An S3-compatible bucket at `S3_ENDPOINT` | `docker compose -f swarm/compose.yaml up -d storage`, then `S3_ENDPOINT=http://localhost:9000` |
| `neo4j` | Neo4j at `NEO4J_URI`, with `NEO4J_PASSWORD` | `docker compose -f swarm/compose.yaml up -d neo4j`, then `NEO4J_URI=neo4j://localhost:7687 NEO4J_PASSWORD=neo4j-local-dev` |

pytest runs with `--strict-markers`, so a misspelt marker is an error rather than a test that silently never runs. Register a new marker in `pyproject.toml` under `[tool.pytest.ini_options]`.

## Running tests

```bash
pip install -e '.[dev,swarm,graph]'     # add forge once it exists: '.[dev,swarm,graph,forge]'
ruff check .                            # lint, the same as CI's lint job
pytest                                  # everything; tests whose service is not set up skip
pytest tests/graph_tests                # one component
pytest -m neo4j                         # only the tests that need Neo4j
```

`-ra` is on by default, so the summary lists every skipped test and its reason. A test you expected to run and that shows as skipped usually means a missing environment variable.

## Running the stack in CI

`.github/workflows/stack.yml` (the `compose` job) runs the swarm's compose stack end to end, the way a laptop does:
- it starts storage, the proxy and Neo4j;
- creates the bucket and replays `samples/run-0` into it through the `sample` pipeline;
- runs the graph job (a dry run, then a write);
- checks that `kg:swarm` holds nodes;
- checks that a pipeline container cannot reach `example.com`, because the proxy's allowlist refuses it.

It runs on every push to `this-is-not-prod`, nightly at 06:17 UTC, and by hand (Actions → Stack → Run workflow). On a PR, add the **`run-stack`** label to run it; it reruns on later pushes while the label is on. It is not a required check, because it takes minutes and pulls large images. A failed nightly run opens an issue labelled `ci`, and every run keeps the stack's logs as the `stack-log` artifact for 7 days.

## CI never sees real data or credentials

The repository is **public**: anything CI reads, and anything it prints, is public too. So CI uses only:

- hand-made fixtures;
- the sample run in `samples/run-0`;
- throwaway passwords that guard nothing, such as `ci-only-password`.

Runs against the sandbox (real GitHub, Jira, Slack and Drive, and model calls) stay manual, on laptops: `eval/check_sandbox.py`, `eval/check_extraction.py` and each component's live checks. Never commit `.env`, `data/`, tokens, or anything fetched from a source.
