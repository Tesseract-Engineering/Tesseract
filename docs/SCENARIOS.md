# Scenarios

Tesseract is built around three moments where the knowledge exists somewhere in the company, but the person who needs it doesn't know where or whom to ask. Each ends with a drafted action, not just an answer.

## 1. "Someone already built this": the precedent finder

**Who:** an engineer about to design something new: a continuous-training pipeline, a payments retry system, scoped API tokens, a bulk export.

**Ask:** *"I want to build a continuous retraining pipeline for our ranking model. Can you help me architect it?"*

**What happens:**
1. Tesseract recognises a build request and first asks the question a senior engineer would: *has anyone here already built this?*
2. If another team has, the answer leads with it ("Before you design this from scratch: …"), quoting their design doc, RFC, ADR or launch thread.
3. A **system design diagram** of the existing pipeline is drawn from the cited records. It shows the inputs, the stages in order (with the gates that can stop a run), the outputs, the feedback or rollback loop, and the one box marked *you add this*, which is where the new use case plugs in.
4. **Hands**, all waiting for approval:
   - **Launch a coding agent.** When the precedent lives in a repo, a brief for an agent to work on a branch there: read the cited docs first, build on the existing system, run the tests, and open a draft PR for the owners to review. It never merges, deploys or touches production.
   - **Email and Slack drafts** to the people who built it (the document's owners and the engineers who closed the tickets), asking for 30 minutes. People who are only *planning* the same thing are not offered as builders, and anyone who has left is named and skipped.

**Why it matters:** the costliest code is the second implementation of something that already exists. The evidence that it exists is usually scattered across a doc, a ticket and a Slack thread, in another team's space.

## 2. On-call: "what's going on, and who owns it?"

**Who:** whoever was paged, often not the owner of the failing system.

**Ask:** *"What caused the dashboards to go stale this morning?"*

**What happens:**
1. The answer quotes the incident record, the relevant runbook, and the alerts or threads that say what changed. Each claim is cited, and anything that doesn't verify is left out.
2. A stale runbook doesn't win just because it ranks well; the gate needs the evidence to actually cover what was asked.
3. **Hands:** a Slack heads-up drafted to the current owners of what the evidence points at, summarising the finding and its sources, ready for the on-call engineer to send.

## 3. Onboarding and access: "how do I get in?"

**Who:** a new hire, or anyone who hits a wall.

**Ask:** *"How do I get production database access?"* or *"Where is the break-glass procedure?"*

**What happens:**
1. If readable docs answer it, the process is quoted, and the approvers it names get a drafted request.
2. If the answer lives in a document the asker **can't** read, Tesseract says so: *"1 matching document is restricted (Security) and was not read; ask Security for access."* It never shows the title or a quote.
3. **Hands:** an email and a Slack message to that document's owners asking for access or the right approver. The draft refers to the restricted location (for example "the Security SharePoint site") and never to the sealed document itself.
4. Asked by someone who holds the access, the same question returns the quoted procedure.

**Why it matters:** access questions are where a company brain most easily leaks. Here, the boundary is part of the answer.

## The same discipline across all three

- **Cited or nothing.** `INFO` claims each quote a record; otherwise `NOT_FOUND` with what was searched.
- **Per-viewer.** Two people asking the same question can get different, both correct, answers.
- **People from the graph.** Owners come from document headers, ticket assignees and thread participants, never from a hard-coded list.
- **Nothing sent.** Every action is a draft behind an exact-payload approval. Executing it writes to the outbox.
