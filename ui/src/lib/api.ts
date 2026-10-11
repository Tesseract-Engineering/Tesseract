// The one client for the Tesseract API (tesseract/api.py). Every call returns parsed JSON or throws ApiError.

export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8091").replace(/\/$/, "");

export type Verdict = "INFO" | "NOT_FOUND";

export interface Claim {
  line?: number | null;
  claim?: string;
  source: string;
  title?: string;
  quote?: string;
  node_id?: string;
  date?: string;
  visibility?: string;
  access?: string;
  restricted?: boolean;
  note?: string;
  acl?: string[];
  askCount?: number;
  lastAskedAt?: string;
}

export interface Owner {
  role: string;
  name: string;
  source?: string;
}

export interface ProposedAction {
  id: string;
  type: string;
  target?: string;
  to?: string;
  payload?: Record<string, unknown>;
}

export interface Nearest {
  title: string;
  source: string;
  node_id?: string;
  score?: number;
  label?: string;
  why?: { rank?: number; cover?: number; linked?: boolean };
}

export interface Searched {
  sources?: { source: string; documents: number }[];
  terms?: string[];
  ask_terms?: string[];
  resolved_entities?: { type: string; name: string; id: string }[];
}

export interface AskResponse {
  verdict: Verdict;
  headline: string;
  reason?: string;
  claims: Claim[];
  nearest?: Nearest[];
  owners?: Owner[];
  approval_required?: { who: string; why?: string; source?: string }[];
  proposed_actions?: ProposedAction[];
  viewer?: string;
  searched?: Searched;
  restricted_matches?: number;
  restricted_note?: string;
  restricted_groups?: string[];
  question?: string;
  design_request?: { asked_as: string; tried?: string[] };
  path?: string;
  ms?: number;
}

export interface DiagramItem {
  label: string;
  detail: string;
  kind: "step" | "gate";
  new: boolean;
  source: string;
}

export interface Diagram {
  title: string;
  inputs: DiagramItem[];
  stages: DiagramItem[];
  outputs: DiagramItem[];
  loop: DiagramItem | null;
  plug_in: string;
  sources: { id: string; title: string }[];
}

export interface ProposalAction {
  action_id: string;
  type: string;
  state: string;
  hash: string;
}

export interface Proposal {
  proposal_id: string;
  actions: ProposalAction[];
}

export interface Receipt {
  proposal_id: string;
  action_id: string;
  type: string;
  state: string;
  approver?: string;
  executed_at?: string;
  result_path?: string;
  hash: string;
}

export interface DocResponse {
  node_id: string;
  label: string;
  title: string;
  text: string;
  source: string;
  date: string;
  author: string;
  visibility: string;
  access: string;
  path?: string | null;
}

export interface SystemCount {
  source: string;
  documents: number;
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network", `Can't reach the API at ${API_URL}.`);
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // fall through to the status check
  }
  if (!res.ok) {
    const err = (data ?? {}) as { error?: string; message?: string };
    throw new ApiError(res.status, err.error ?? "error", err.message ?? `HTTP ${res.status}`);
  }
  return data as T;
}

const q = (params: Record<string, string>) => new URLSearchParams(params).toString();

export interface ViewerPreset {
  name: string;
  label: string;
  acl_groups: string[];
}

export interface CompanyResponse {
  name: string;
  viewers: ViewerPreset[];
  default_viewer: string;
  users: { name: string; team: string; viewer: string }[];
  suggestions: string[];
}

export const api = {
  company: () => call<CompanyResponse>("GET", "/company"),
  viewers: () => call<{ viewers: ViewerPreset[]; default: string }>("GET", "/viewers"),
  ask: (question: string, viewer: string, asker?: string) =>
    call<AskResponse>("POST", "/ask", { question, viewer, ...(asker ? { asker } : {}) }),
  propose: (actions: ProposedAction[]) => call<Proposal>("POST", "/propose", { actions }),
  approve: (proposal_id: string, action_id: string, approver: string) =>
    call<ProposalAction>("POST", "/approve", { proposal_id, action_id, approver }),
  execute: (proposal_id: string, action_id: string) => call<Receipt>("POST", "/execute", { proposal_id, action_id }),
  doc: (node_id: string, viewer: string) => call<DocResponse>("GET", `/doc?${q({ node_id, viewer })}`),
  design: (question: string, viewer: string, node_ids: string[]) =>
    call<{ diagram: Diagram | null; why?: string }>("POST", "/design", { question, viewer, node_ids }),
  systems: (viewer: string) => call<{ systems: SystemCount[]; viewer: string }>("GET", `/systems?${q({ viewer })}`),
};
