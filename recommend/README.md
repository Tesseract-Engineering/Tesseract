# recommend

Finds manual work that keeps coming back and ranks it as automation opportunities. It reads the swarm's runs through `graph/swarm_records.py` (the same current state the graph is built from) and acts on nothing. A person reads the report and picks what to build.

For each pattern the report gives:
- how many times it was asked, as the viewer can see it
- the approver, from who owns the system each ask is about
- the steps, from the runbook the asks link to
- why it ranks where it does
- links to every instance

Plain code counts and scores. A model, when one is configured, does the judgment around it:

```
requests ──READ──▶ a plain phrase + system per item ──group asks──▶ groups ──MERGE──▶ fewer groups
         ──score──▶ top groups ──REVIEW──▶ name, steps, same_steps ──score──▶ report
```

- **READ** says whether each ticket or thread asks for something, writes the ask as a plain phrase with the system left out, and names the system separately. So "read access to Grafana" and "Need prod Postgres creds" group as one request, and each is routed to its own system's owner. Items it says ask for nothing are dropped.
- **MERGE** joins groups worded too differently for word overlap. Each join keeps its reason and the items it joined.
- **REVIEW** names the top patterns and judges whether handling follows the same steps, using the linked runbook when there is one. Steps must cite the runbook or the group's own items, or they are dropped.

The model never changes a count, and every answer is checked in code first (`judge.py`). It is the swarm's model client (`swarm/model.py`): the same `SWARM_MODEL_*` settings, `replay` recordings, and `off`. If it goes down mid-run, the rest of the run is plain code and the report says so.

## What counts, and who approves

- **Asks, not items.** These are one ask, never two:
  - a ticket that links the Slack thread it was filed from
  - a thread that names the one ticket filed from it
  - a Jira Duplicate link

  Ordinary references (epic children, a status thread naming five tickets) never join asks, and an ask never holds two Slack threads (`linking.py`).
- **Approver from ownership, never from who approved last time** (`owners.py`). Owners are read, in order of trust, from:
  1. any table with an Owner column (the handbook's "Service owners")
  2. a repo's CODEOWNERS catch-all line
  3. `owns` relations the swarm extracted

  An ask that names no system goes to its runbook's owner.
- **Runbook** (`runbooks.py`): a document linked from at least 3 asks, or from 2 that are at least 20% of them, by ref or by a Drive or GitHub file URL in the text. Its first ordered list is the steps.
- **As one viewer** (`access.py`, `--as`): only items that person may read are counted, linked, or used for owners and steps, using `graph/swarm_queries.py`'s rules.
- **Noise:** bot-only posts (PagerDuty, CI) are never asks.

| File | What it does |
|---|---|
| `patterns.py` | Groups the window's asks by normalised title overlap. Ticket keys, names, emails and known system names are stripped first. |
| `score.py` | Scores each group on how often it is asked and how well-defined its steps are (runbook, written steps, one handler, alike wording, one place), and writes the evidence and "why" lines from the same numbers. |
| `linking.py`, `owners.py`, `runbooks.py`, `access.py`, `directory.py` | Same-ask links, ownership, runbooks, the viewer filter, one key and name per person across sources. |
| `judge.py`, `prompts.py` | The three model steps and the checks on their answers. |
| `rec_weights.py` | Every threshold and weight. |
| `recommend.py` | The CLI and the markdown report. |

```
python recommend/recommend.py --root samples/run-0 [--as someone@company.com] [--json out.json] [--no-llm]
python recommend/test_recommend.py
python recommend/test_judge.py           # the model steps against a scripted model; no API calls
python recommend/test_sandbox_shape.py   # owners, asks, runbooks, viewers, decoys on a sandbox-shaped company
```
