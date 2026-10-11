// Maps a raw /ask response into the view model the components render.
// Nothing here is specific to one question: lines, citations, groups, timeline,
// people and actions are all derived from the fields the API returns.

import type { AskResponse, Claim, Owner, ProposedAction, Verdict } from "./api";
import { type SystemInfo, systemOf } from "./systems";

export interface Pill {
  label: string;
  bg: string;
  fg: string;
}

export const VERDICT: Record<Verdict, Pill> = {
  INFO: { label: "Here's what I found", bg: "#E3E2E0", fg: "#32302C" },
  NOT_FOUND: { label: "I couldn't verify this", bg: "#FDECC8", fg: "#402C1B" },
};

export interface LineView {
  text: string;
  bold: boolean;
  quote?: boolean;
  cites: number[];
}

export interface SourceView {
  n: number;
  nodeId?: string;
  system: SystemInfo;
  title: string;
  quote: string;
  quoteRaw: string;
  date: string;
  author: string;
  askCount: number;
  restricted: boolean;
  owner: string;
  accessLabel: string;
  visibility: string;
  note: string;
  muted: boolean;
}

export interface GroupView {
  title: string;
  color: string;
  rows: SourceView[];
}

export interface TimelinePoint {
  iso: string;
  date: string;
  label: string;
  color: string;
}

export interface PersonView {
  name: string;
  role: string;
  init: string;
  bg: string;
}

export interface ActionView {
  id: string;
  name: string;
  reason: string;
  cites: number[];
  payload: string;
}

export interface LookedView {
  system: SystemInfo;
  count: number;
}

export interface NotFoundView {
  looked: LookedView[];
  reason: string;
  terms: string[];
  callout: string;
  owner: string;
  hasRestricted: boolean;
}

export interface AnswerView {
  verdict: Verdict;
  pill: Pill;
  lines: LineView[];
  sources: SourceView[];
  groups: GroupView[];
  railHeading: string;
  timeline: TimelinePoint[];
  people: PersonView[];
  actions: ActionView[];
  rawActions: ProposedAction[];
  notFound: NotFoundView | null;
  footer: string;
  design: { question: string; ids: string[] } | null;
}

// --------------------------------------------------------------------------- //
// small text helpers

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ISO = /\b(20\d{2})-(\d{2})-(\d{2})\b/g;
const MONTH_DAY = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b/g;
const TICKET = /\b[A-Z][A-Z0-9]+-\d+\b/g;

export function prettyDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return iso || "";
  return `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[1]}`;
}

function isoOf(year: string | number, monthName: string, day: string | number): string {
  const mi = MONTHS.indexOf(monthName.slice(0, 3)) + 1;
  return `${year}-${String(mi).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** Split prose into sentences without breaking on "e.g." or decimals. */
export function sentences(text: string): string[] {
  const out: string[] = [];
  const re = /[^.!?]+(?:[.!?](?![\w\d])|$)+/g;
  let buf = "";
  for (const m of (text || "").match(re) ?? []) {
    buf += m;
    const t = buf.trim();
    if (/\b(e\.g|i\.e|vs|etc|No)\.$/.test(t)) continue;
    if (t) out.push(t);
    buf = "";
  }
  if (buf.trim()) out.push(buf.trim());
  return out.length ? out : [text];
}

/** Headline sentences; a long "X, but Y" sentence reads better as two lines. */
function answerSentences(headline: string): string[] {
  return sentences(headline).flatMap((s) => {
    const i = s.indexOf(", but ");
    if (s.length < 120 || i < 30) return [s];
    const rest = s.slice(i + 6);
    return [`${s.slice(0, i)}.`, rest.charAt(0).toUpperCase() + rest.slice(1)];
  });
}

function stripEllipsis(s: string): string {
  return (s || "").replace(/^[…\s]+|[…\s]+$/g, "").trim();
}

/** "Marcus Rodriguez: Ship it." -> "Marcus Rodriguez" */
function speaker(quote: string): string {
  const m = /^[…\s]*([A-Z][a-z]+(?: [A-Z][a-z'-]+){1,2}):\s/.exec(quote || "");
  return m ? m[1] : "";
}

function initials(name: string): string {
  const words = name.split(/[\s\-_]+/).filter(Boolean);
  return (
    words
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? "")
      .join("") || "?"
  );
}

const AVATAR_BG = ["#DBEDDB", "#E7F3F8", "#FDECC8", "#F4EEEE", "#E8DEEE", "#FBF3DB", "#FAEBDD", "#EEE0DA"];

function avatarBg(name: string): string {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_BG[h % AVATAR_BG.length];
}

/** Owner named in a stub note: "you do not have access; owner Legal" -> "Legal". */
export function ownerFromNote(note: string | undefined): string {
  const m = /owner\s+(.+)$/i.exec(note || "");
  return m ? m[1].trim() : "the document owner";
}

/** Mirror of access.owner_of on the server: ACL group -> who to ask. */
function ownerOfGroup(g: string): string {
  if (g.startsWith("sharepoint:"))
    return g
      .split(":")[1]
      .replace(/-/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  if (g.includes(":user:")) return g.split(":user:")[1];
  if (g.startsWith("slack:")) return `the #${g.slice(6).replace(/^#/, "")} channel owners`;
  return g;
}

// --------------------------------------------------------------------------- //
// citations: which answer line does each claim support?

const STOP = new Set(
  "the and for with that this from into over under about after before their there which while would could should have has had been were was are not you your our its it's they them than then when what where who why how also only still just".split(
    " ",
  ),
);

interface Needle {
  text: string;
  weight: number;
}

/** Words a sentence uses when it talks about a source system's records. */
const SOURCE_WORDS: Record<string, string[]> = {
  jira: ["ticket", "jira"],
  github: ["pull request", "github"],
  slack: ["slack", "approved"],
  outlook: ["notice", "email"],
  teams: ["teams"],
  code: ["code", "todo"],
  sharepoint: ["contract", "policy"],
  catalog: ["service", "catalog", "owned"],
};

function claimText(c: Claim): string {
  return [c.title, c.quote, c.claim, c.note].filter(Boolean).join(" ");
}

/** Lowercased content words, possessives and simple suffixes stripped. */
function tokens(text: string): Set<string> {
  const out = new Set<string>();
  for (let w of text
    .toLowerCase()
    .replace(/['’]s\b/g, "")
    .split(/[^a-z0-9#/]+/)) {
    if (w.length < 4 || STOP.has(w)) continue;
    if (w.length > 5) w = w.replace(/(ing|ed|es|s)$/, "");
    out.add(w);
  }
  return out;
}

/** Strong signals: ticket keys, dates, the system's own name, people. */
function needles(c: Claim, people: string[]): Needle[] {
  const text = claimText(c);
  const out: Needle[] = [];
  for (const k of text.match(TICKET) ?? []) out.push({ text: k, weight: 3 });
  const dates = new Set<string>();
  if (c.date) dates.add(c.date.slice(0, 10));
  for (const m of text.matchAll(ISO)) dates.add(m[0]);
  for (const d of dates) {
    out.push({ text: d, weight: 3 });
    out.push({ text: prettyDate(d).replace(/, \d{4}$/, ""), weight: 2 });
  }
  const sys = systemOf(c.source).name;
  if (sys.length >= 4) out.push({ text: sys, weight: 3 });
  for (const p of people) if (p && text.includes(p)) out.push({ text: p, weight: 2 });
  return out;
}

interface Matcher {
  needles: Needle[];
  words: Set<string>;
}

function matcher(c: Claim, people: string[]): Matcher {
  const words = tokens(claimText(c));
  for (const w of SOURCE_WORDS[c.source] ?? []) for (const t of tokens(w)) words.add(t);
  return { needles: needles(c, people), words };
}

/** Strong-signal weights plus content-word overlap, each word worth 1/(lines that use it). */
function score(sentence: string, m: Matcher, df?: Map<string, number>): number {
  const s = sentence.toLowerCase();
  let total = 0;
  const seen = new Set<string>();
  for (const n of m.needles) {
    const t = n.text.toLowerCase();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    if (s.includes(t)) total += n.weight;
  }
  for (const w of tokens(sentence)) if (m.words.has(w)) total += 1 / (df?.get(w) ?? 1);
  return total;
}

const MIN_SCORE = 1.5;

function bestLine(lines: LineView[], m: Matcher, df: Map<string, number>): number {
  let best = -1;
  let bestScore = MIN_SCORE - 1e-9;
  lines.forEach((ln, i) => {
    if (ln.quote) return;
    const sc = score(ln.text, m, df);
    if (sc > bestScore) {
      best = i;
      bestScore = sc;
    }
  });
  return best;
}

function lineDf(lines: LineView[]): Map<string, number> {
  const df = new Map<string, number>();
  for (const ln of lines) for (const w of tokens(ln.text)) df.set(w, (df.get(w) ?? 0) + 1);
  return df;
}

// --------------------------------------------------------------------------- //
// rail grouping

const GROUP_STYLE: Record<string, string> = {
  Sources: "#787774",
  "Nearest, not an answer": "#9B9A97",
};

function accessLabel(c: Claim): string {
  const vis = c.visibility || "internal";
  if (vis === "internal" || vis === "public") return "";
  const via = /has\s+(\S+)/.exec(c.access || "");
  const cap = vis.charAt(0).toUpperCase() + vis.slice(1);
  if (c.restricted) return cap;
  if (!via) return `${cap} · you have access`;
  return via[1].includes(":user:")
    ? `${cap} · in your mailbox`
    : `${cap} · you have access via ${ownerOfGroup(via[1])}`;
}

function sourceView(c: Claim, n: number): SourceView {
  const restricted = Boolean(c.restricted) || (Boolean(c.note) && !c.quote);
  return {
    n,
    nodeId: c.node_id,
    system: systemOf(c.source),
    title: c.title || c.node_id || "Untitled",
    quote: stripEllipsis(c.quote || "").replace(/\*\*|`/g, ""),
    quoteRaw: c.quote || "",
    date: c.date ? prettyDate(c.date) : "",
    author: speaker(c.quote || ""),
    askCount: c.askCount ?? 0,
    restricted,
    owner: ownerFromNote(c.note),
    accessLabel: accessLabel(c),
    visibility: c.visibility || "internal",
    note: "",
    muted: false,
  };
}

// --------------------------------------------------------------------------- //
// timeline

interface DateHit {
  iso: string;
  label: string;
  kind: "plan" | "deadline" | "event";
  priority: number;
}

const EVENT_PRIORITY: Record<string, number> = { outlook: 0, slack: 1, teams: 2, jira: 3, github: 4 };

function shortPhrase(c: Claim): string {
  const q = stripEllipsis(c.quote || "").replace(/^[A-Z][a-z]+(?: [A-Z][a-z'-]+){1,2}:\s/, "");
  const short = sentences(q)
    .map((s) => s.replace(/[.!?]+$/, "").trim())
    .filter((s) => {
      const w = s.split(/\s+/).length;
      return w >= 2 && w <= 3 && !/\d{2}:/.test(s);
    })
    .sort((a, b) => a.length - b.length)[0];
  if (short) return short.charAt(0).toLowerCase() + short.slice(1);
  let head = (c.title || "").replace(TICKET, "").replace(/^[:\s-]+/, "");
  head = head.split(/[:(]/)[0].trim();
  const words = head.split(/\s+/);
  return words.length > 4 ? `${words.slice(0, 4).join(" ")}…` : head;
}

function contextDates(text: string, year: number): { iso: string; context: string }[] {
  const out: { iso: string; context: string }[] = [];
  for (const m of text.matchAll(ISO)) {
    out.push({ iso: m[0], context: text.slice(Math.max(0, (m.index ?? 0) - 60), m.index) });
  }
  for (const m of text.matchAll(MONTH_DAY)) {
    out.push({
      iso: isoOf(m[3] ?? year, m[1], m[2]),
      context: text.slice(Math.max(0, (m.index ?? 0) - 60), m.index),
    });
  }
  return out;
}

const PLAN_CTX = /(sprint|planned|target|scheduled|removal|ship)[^.]*$/i;
const DEADLINE_CTX = /(earliest|no shutdown before|before|until|notice period|deadline|ends?)[^.]*$/i;

function timeline(res: AskResponse, claims: Claim[]): TimelinePoint[] {
  const hits: DateHit[] = [];
  const years = claims.map((c) => Number((c.date || "").slice(0, 4))).filter(Boolean);
  const year = years.length ? Math.max(...years) : new Date().getFullYear();
  const removing = /shut ?down|retire|remov|delet|decommission|sunset/i.test(`${res.question ?? ""} ${res.headline}`);

  const noticeMentioned = claims.some((c) => /notice/i.test(claimText(c)));
  const special = (text: string) => {
    for (const d of contextDates(text, year)) {
      if (DEADLINE_CTX.test(d.context) && /earliest|notice|before|until|deadline/i.test(d.context)) {
        const label = noticeMentioned ? "Notice period ends" : "Earliest allowed";
        hits.push({ iso: d.iso, label, kind: "deadline", priority: -2 });
      } else if (PLAN_CTX.test(d.context)) {
        hits.push({
          iso: d.iso,
          label: removing ? "Planned removal" : "Sprint starts",
          kind: "plan",
          priority: -1,
        });
      }
    }
  };
  special(res.headline);
  for (const c of claims) special(`${c.quote ?? ""} ${c.claim ?? ""}`);

  for (const c of claims) {
    if (!c.date || !(c.source in EVENT_PRIORITY)) continue;
    hits.push({
      iso: c.date.slice(0, 10),
      label: `${systemOf(c.source).name}: ${shortPhrase(c)}`,
      kind: "event",
      priority: EVENT_PRIORITY[c.source],
    });
  }

  // one point per date: the most specific label wins
  const byDate = new Map<string, DateHit>();
  for (const h of hits.sort((a, b) => a.priority - b.priority)) {
    if (!byDate.has(h.iso)) byDate.set(h.iso, h);
  }
  const specials = [...byDate.values()].filter((h) => h.kind !== "event");
  const events = [...byDate.values()].filter((h) => h.kind === "event").sort((a, b) => a.priority - b.priority);
  const chosen = [...specials, ...events.slice(0, Math.max(0, 5 - specials.length))].sort((a, b) =>
    a.iso.localeCompare(b.iso),
  );
  if (chosen.length < 2) return [];

  return chosen.map((h) => ({
    iso: h.iso,
    date: prettyDate(h.iso),
    label: h.label,
    color: h.kind === "deadline" ? "#448361" : h.kind === "plan" ? "#E03E3E" : "#9B9A97",
  }));
}

// --------------------------------------------------------------------------- //
// people and actions

const ROLE: Record<string, string> = {
  owner: "owner",
};

function people(owners: Owner[]): PersonView[] {
  const seen = new Set<string>();
  const out: PersonView[] = [];
  for (const o of owners) {
    if (!o.name || seen.has(o.name)) continue;
    seen.add(o.name);
    let role = ROLE[o.role] ?? o.role.replace(/[-_]/g, " ");
    const ticket = (o.source ?? "").match(TICKET)?.[0];
    if (ticket) role += ` on ${ticket}`;
    out.push({ name: o.name, role, init: initials(o.name), bg: avatarBg(o.name) });
  }
  return out;
}

function firstSentence(s: unknown): string {
  if (typeof s !== "string" || !s) return "";
  const first = sentences(s.replace(/\s+/g, " "))[0] ?? s;
  return first.length > 220 ? `${first.slice(0, 217)}…` : first;
}

/** The first recipient by name: the draft's own to_names, else read off the address or handle. */
function recipient(a: ProposedAction, p: Record<string, unknown>): string {
  if (Array.isArray(p.to_names) && p.to_names.length) return String(p.to_names[0]);
  const to = Array.isArray(p.to) ? String(p.to[0]) : String(a.to ?? p.to ?? "");
  return (to.replace(/^@/, "").split("@")[0] ?? to)
    .split(/[._]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function actionView(a: ProposedAction, claims: Claim[], allPeople: string[]): ActionView {
  const p = (a.payload ?? {}) as Record<string, unknown>;
  let name: string;
  let reason: string;
  switch (a.type) {
    case "email.draft":
      name = `Draft an email to ${recipient(a, p)}`;
      reason = String(p.subject ?? firstSentence(p.body));
      break;
    case "agent.launch": {
      const reviewers = Array.isArray(p.reviewers) ? (p.reviewers as string[]).join(", ") : "the owners";
      name = `Launch a coding agent: ${String(p.title ?? "build on the existing system")}`;
      reason = `Works on a branch in ${String(p.repo ?? "the repo")} and opens a draft PR for ${reviewers} to review. It never merges or deploys.`;
      break;
    }
    case "slack.draft":
      name = `Draft a Slack message to ${recipient(a, p)}`;
      reason = firstSentence(p.text);
      break;
    default:
      name = a.type;
      reason = firstSentence(p.summary ?? p.description);
  }
  const cites: number[] = [];
  claims.forEach((c, i) => {
    if (cites.length < 3 && score(reason, matcher(c, allPeople)) >= 3) cites.push(i + 1);
  });
  return { id: a.id, name, reason, cites, payload: JSON.stringify(a.payload ?? a, null, 2) };
}

// --------------------------------------------------------------------------- //
// the presenter

export function present(res: AskResponse): AnswerView {
  const claims = res.claims ?? [];
  const verdict: Verdict = (res.verdict in VERDICT ? res.verdict : "NOT_FOUND") as Verdict;
  const ownerNames = (res.owners ?? []).map((o) => o.name);

  const lines: LineView[] = answerSentences(res.headline || "").map((text) => ({
    text,
    bold: false,
    cites: [],
  }));

  // INFO answers often have a one-line headline; quote the top records under it.
  if (verdict === "INFO") {
    claims.slice(0, 2).forEach((c, i) => {
      const q = stripEllipsis(c.quote || "");
      if (q && !c.restricted) lines.push({ text: `“${q}”`, bold: false, quote: true, cites: [i + 1] });
    });
  }

  const df = lineDf(lines);
  claims.forEach((c, i) => {
    const n = i + 1;
    if (lines.some((l) => l.quote && l.cites.includes(n))) return;
    const idx = bestLine(lines, matcher(c, ownerNames), df);
    const target = idx >= 0 ? idx : lines.length - 1;
    if (target >= 0 && !lines[target].cites.includes(n)) lines[target].cites.push(n);
  });

  const sources = claims.map((c, i) => sourceView(c, i + 1));
  let groups: GroupView[];
  const railHeading = "Sources";
  if (verdict === "NOT_FOUND" && !claims.length) {
    const rows: SourceView[] = (res.nearest ?? []).map((h, i) => ({
      n: i + 1,
      nodeId: h.node_id,
      system: systemOf(h.source),
      title: h.title,
      quote: "",
      quoteRaw: "",
      date: "",
      author: "",
      askCount: 0,
      restricted: false,
      owner: "",
      accessLabel: "",
      visibility: "internal",
      note:
        h.why?.cover !== undefined
          ? `Covers ${Math.round(h.why.cover * 100)}% of the question · score ${(h.score ?? 0).toFixed(2)}`
          : "",
      muted: true,
    }));
    groups = rows.length
      ? [{ title: "Nearest, not an answer", color: GROUP_STYLE["Nearest, not an answer"], rows }]
      : [];
    sources.push(...rows);
  } else {
    groups = sources.length ? [{ title: "Sources", color: GROUP_STYLE.Sources, rows: sources }] : [];
  }

  let notFound: NotFoundView | null = null;
  if (verdict === "NOT_FOUND") {
    const groupsAcl = res.restricted_groups ?? [];
    const owner = groupsAcl.length
      ? [...new Set(groupsAcl.map(ownerOfGroup))].join(", ")
      : (/ask (.+?) for access/.exec(res.restricted_note ?? "")?.[1] ?? "the owner");
    const count = res.restricted_matches ?? 0;
    notFound = {
      looked: (res.searched?.sources ?? []).map((s) => ({ system: systemOf(s.source), count: s.documents })),
      reason: res.reason ?? "",
      terms: res.searched?.ask_terms ?? res.searched?.terms ?? [],
      hasRestricted: count > 0,
      owner,
      callout:
        count > 0
          ? `${count} matching document${count === 1 ? " is" : "s are"} restricted (${owner}) and ${count === 1 ? "was" : "were"} not read.`
          : "Nothing you can read answers this.",
    };
  }

  const systemsChecked = res.searched?.sources?.length ?? new Set(claims.map((c) => c.source)).size;

  return {
    verdict,
    pill: VERDICT[verdict],
    lines,
    sources,
    groups,
    railHeading,
    timeline: timeline(res, claims),
    people: people(res.owners ?? []),
    actions: (res.proposed_actions ?? []).map((a) => actionView(a, claims, ownerNames)),
    rawActions: res.proposed_actions ?? [],
    notFound,
    design:
      res.design_request && verdict === "INFO" && claims.length
        ? { question: res.question ?? "", ids: claims.map((c) => c.node_id ?? "").filter(Boolean) }
        : null,
    footer: systemsChecked ? `Checked ${systemsChecked} system${systemsChecked === 1 ? "" : "s"}.` : "",
  };
}

/** Lines shown while the answer is on its way, then rewritten once it lands. */
export function workingLines(systemNames: string[], res?: AskResponse): string[] {
  const names = systemNames.length ? systemNames.slice(0, 6).join(", ") : "every connected system";
  const first = `Searching ${names}`;
  if (!res) return [first, "Checking what you can see", "Cross-checking dates"];
  const r = res.restricted_matches ?? 0;
  const second = r
    ? `Found ${r} restricted match${r === 1 ? "" : "es"} (${(res.restricted_groups ?? []).map(ownerOfGroup).join(", ") || (/ask (.+?) for access/.exec(res.restricted_note ?? "")?.[1] ?? "owner")})`
    : `Found ${res.claims.length} record${res.claims.length === 1 ? "" : "s"} you can see`;
  return [first, second, "Cross-checking dates"];
}
