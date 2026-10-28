"""
prompts.py - the recommendation agent's three model steps, versioned like swarm/stages/prompts.py.

  READ    one item -> is it routine work (an ask, or a recurring task filed to be done), what, in words
          that match the same work from anyone else ("grant read access to a system"), and which system
  MERGE   the groups plain code made -> which of them are the same request worded differently
  REVIEW  one group's items, and its runbook when the items link one -> a name, whether the handling
          follows the same steps, and those steps

The model only proposes. judge.py checks every answer in code: ids it cites must be ids it was given,
merges must name groups that exist, and nothing it says changes a count. A version goes into every
recording key (through the system text), so a prompt change re-asks rather than reusing old answers.
Each prompt carries its own version, so changing one re-asks only that step.
"""

from __future__ import annotations

from typing import Any

VERSION = "rec-v3"           # READ and REVIEW
MERGE_VERSION = "rec-v5"     # v4: samples show how each was handled; v5: groups lists only what joins


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": sorted(properties), "additionalProperties": False}


READ = f"""\
[{VERSION} read] You read one ticket or chat thread from a company's tools and say whether it is a piece of \
routine work: somebody asking for something to be done for them (access, an account, a reset, a change, a \
report, a data export), or a recurring task filed to be done (rotate keys, renew certificates, onboard a new \
hire or a model).

Not routine work: a question that only wants an answer ("where is the runbook?"), a status update, a \
discussion or proposal, an announcement, a decision, an incident being investigated.

When it is routine work, write it as a short imperative phrase that reads the same whoever asked, whenever, \
for whichever customer and about whichever system. Name the object generically: "a system" for any repo, \
service, dashboard, database or tool; "a customer" for any customer. Leave out qualifiers that vary between \
cases (read-only, temporary, for a new hire, by workspace, for a quarter). "Grant read access to a system", \
not "Get Dana prod DB access for AUTH-12"; "Export usage data for a customer", not "Send Acme their Q3 CSV". \
Use the plainest common words, so the same work phrased differently gets the same phrase. Otherwise leave \
the phrase empty.

system: the system, service, repo, tool or dataset the work is about, exactly as the text names it \
("staging Grafana", "prod read replica", "infra repo"), or empty when it names none. The text may name the \
system only in a reply; look there too."""

READ_SCHEMA = _obj({"is_request": {"type": "boolean"},
                    "request": {"type": "string", "description": "Imperative phrase, empty when not a request"},
                    "system": {"type": "string", "description": "As the text names it, or empty"}})

MERGE = f"""\
[{MERGE_VERSION} merge] Plain code grouped a company's repeated work by wording. Many groups are the same work \
worded differently: "Grant read access to a repo" and "Grant access to a dashboard" are one kind of work \
(access requests), "Export usage data for a customer" and "Provide a usage report" are another. Say which \
groups to join.

Each sample shows how it was handled, when the item says. Judge "same steps" from that handling: two asks \
handled by the same script, runbook or procedure are the same work even when one asks for a different \
dataset or a redo. Join groups that are the same kind of work done by the same steps, whatever the system, \
customer, person, time period or file format: which system an ask is about decides who approves it, not what kind of work it \
is, and that is handled elsewhere. Do not join different kinds of work that share a topic: "reset a VPN \
token" and "grant VPN access" stay apart, and so do "rotate keys" and "investigate a key rotation incident". \
Give each merge a one-sentence reason a reviewer can check. A merge's groups are exactly the groups being \
joined: never list a group the reason leaves out, and when unsure about one group, leave it out."""

MERGE_SCHEMA = _obj({"merges": {"type": "array", "items": _obj({
    "groups": {"type": "array", "items": {"type": "integer"}, "description": "Group numbers to join, two or more"},
    "reason": {"type": "string"}})}})

REVIEW = f"""\
[{VERSION} review] These items are one repeated request at a company. Someone is deciding whether to \
automate it. Read them and say:

- name: what the request is, in a few plain words ("Production database read access").
- same_steps: 0 to 1, how far handling it follows the same steps every time, judged from what the items \
say was done. 0.9 and above when the items show the same steps; around 0.5 when they suggest it; below \
0.3 when each case is handled differently or the items say nothing about handling.
- steps: the steps, in order. When a runbook is given, take them from the runbook and list where the items show handling that departs from it in same_steps, not in the steps. Otherwise take them from the items. Leave it empty rather than guess.
- evidence: the ids of the runbook and items that show the steps, copied exactly as given."""

REVIEW_SCHEMA = _obj({"name": {"type": "string"}, "same_steps": {"type": "number"},
                      "steps": {"type": "array", "items": {"type": "string"}},
                      "evidence": {"type": "array", "items": {"type": "string"}}})
