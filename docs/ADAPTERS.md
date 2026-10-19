# Connecting a company's data

Tesseract reads **exports**: files that someone authorised to produce them handed over. It does not need API tokens or OAuth scopes into live systems. An **adapter** knows one folder layout and turns it into a `Bundle` (records, people, teams, ACLs); the builder does the rest without knowing which company it is.

```bash
python -m tesseract.build --root /path/to/exports --dry-run   # read and check, write nothing
python -m tesseract.build --root /path/to/exports             # build namespace TESSERACT_NS
```

This holds with the Integration Forge too ([FORGE.md](FORGE.md)). Sources that are only reachable through an API (GitHub, Google Drive) get a `snapshot` command that pulls them into the export layout, with each item's native id in the file. The build still reads only files, so it stays offline and reproducible, and the snapshots double as replay fixtures. `snapshot` is the only ingest-time code that calls provider APIs, and it goes through the Forge's credential broker.

The dry run prints **coverage** (files read vs. present, and the folders nobody read), **refs** (how many records per source a live provider could fetch again, links that disagree with their ref, and which sources are index-only; see [FORGE.md](FORGE.md#source-refs)), identity conflicts, adapter problems and proposal counts. Run it first on any new data.

## The workspace layout

The `workspace` adapter reads this layout. Every part is optional, and the adapter is auto-detected when at least two parts are present.

```
company.json            name, email domain, viewers, sign-in users, suggestions
people/directory.json   the roster
slack/                  Slack workspace export
jira/*.csv              Jira CSV exports, one file per project
sharepoint/<site>/<library>/...   SharePoint documents (.md .docx .pdf)
outlook/mailboxes/<mailbox>/*.json   Outlook conversations
teams/<team>/<channel>/*.json        Teams channel threads
teams/chats/*.json                   Teams 1:1 and group chats
docs/                   any other prose: .md .rst .txt .docx .pdf
```

### `company.json`

See the [README](../README.md#configuration) and `tesseract/config.py`. A viewer's `acl_groups` use the same group names the connectors attach (below), for example `sharepoint:security` or `slack:incident-response`.

### `people/directory.json`

```json
{
  "employees": [
    {"id": "e1", "name": "Jane Doe", "email": "jane@example.com", "slack": "jane",
     "title": "Staff Engineer", "team": "Payments"},
    {"id": "e9", "name": "Old Timer", "status": "alumni", "departed_at": "2025-06-30"}
  ],
  "private_slack_channels": ["incident-response"]
}
```

The roster gives each person their accounts (so Jane's Slack messages, email and Jira tickets resolve to one `Person`), their team, and whether they have left. People who have left are never proposed as contacts. Drafts name them as having left instead.

### What each source becomes

| Source | Record | ACL attached at ingest |
|---|---|---|
| Slack export (`users.json` + one folder per channel of daily `*.json`) | one document per **thread**, replies folded in, as `Name: text` lines | channels in `private_slack_channels` → `private`, group `slack:<channel>` |
| Jira CSV (`Issue key, Summary, Description, Created, Updated, Reporter, Assignee, Status, Labels…`) | one issue per row, id `ticket:<KEY>`, assignee declared, ref `forge://jira/issue/<KEY>`, link `<jira_base_url>/browse/<KEY>` when `company.json` sets `jira_base_url` | internal |
| SharePoint file, with optional sidecar `<file>.meta.json` `{author, modified, site, library, sensitivity, url}` | one document | sensitivity Confidential / Highly Confidential / Restricted / Secret → `restricted`, group `sharepoint:<site>` |
| Outlook `{subject, conversation_id, messages: [{from, to, cc, date, body}]}` | one document per conversation, however many mailboxes hold a copy | `private`, one group `outlook:user:<email>` per participant |
| Teams channel thread `{team, channel, thread_id, is_private, messages: [{user, name, timestamp, text}]}` | one document per thread | `is_private` → group `teams:<team>/<channel>` |
| Teams chat `{participants, messages}` | one document per chat | `private`, one group `teams:user:<handle>` per participant |
| `docs/` file | one document, titled by its first Markdown heading | internal |

A document header such as `**Owner:** Jane Doe` or `_Author: Sam Lee_` becomes a `CREDITS` edge with its role. This is the strongest signal for who to contact about a document, so it is worth adding headers to runbooks, ADRs and design docs.

Put company-wide reference pages (about, org chart, tech stack, who-to-ask) under `docs/company-profile/`. Whole-company questions ("what do we sell?", "who do I ask about security?") always consider them. The folder can be changed with `profile_prefix` in `company.json`.

## Writing an adapter

When a company's exports are laid out differently, write a module in `tesseract/adapters/` and register it in `adapters/__init__.py`:

```python
NAME = "acme"

def looks_like(root: Path) -> bool:        # cheap check for --adapter auto
    return (root / "acme-export.json").is_file()

def load(root: Path) -> Bundle:
    bundle = Bundle(name=NAME, root=root, speaker_sources={"slack"})
    bundle.records.extend(connectors.slack_export(root / "chat"))
    ...
    return bundle
```

`workspace.py` is the reference. The contract, in `bundle.py`:

- **Read and rename; decide nothing.** Merging people, resolving conflicts and inferring mentions happen downstream, where they are logged and reviewable. An adapter that "cleans" data hides the evidence.
- **Attach the ACL at ingest.** Set `visibility` and `acl_groups` on every record from what the source says. Nothing downstream can reconstruct which channel was private once the text is loose.
- **Declare, don't guess.** Fill a field (`assigneeName`, `declaredService`, `customer`) only when the source states it. Leave absent what the source does not have.
- **Account for every file.** Records with `meta["path"]` count toward coverage automatically; add structural files (rosters, catalogs) to `bundle.files_read`.

`bundle.problems()` reports duplicate ids and ownership claims about unknown teams or services before a build. The `generic` adapter (every readable document, no people) is the fallback, and its coverage report lists exactly what a real adapter would need to read.
