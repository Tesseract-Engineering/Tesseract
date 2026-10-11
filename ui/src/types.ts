// One import point for every type the components use.
// Raw API shapes live in lib/api.ts, view-model shapes in lib/present.ts.

export type {
  AskResponse,
  Claim,
  DocResponse,
  Nearest,
  Owner,
  Proposal,
  ProposalAction,
  ProposedAction,
  Receipt,
  SystemCount,
  Verdict,
  ViewerPreset,
} from "./lib/api";
export type { Company, User } from "./lib/company";
export type {
  ActionView,
  AnswerView,
  GroupView,
  LineView,
  LookedView,
  NotFoundView,
  PersonView,
  Pill,
  SourceView,
  TimelinePoint,
} from "./lib/present";
export type { SystemInfo } from "./lib/systems";
