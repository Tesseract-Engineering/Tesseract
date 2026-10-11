import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, api, type Proposal, type ProposedAction, type Receipt } from "@/lib/api";
import type { ActionView } from "@/lib/present";
import { CitationChip } from "./CitationChip";

type Status = { state: "idle" | "working" | "error" | "done"; error?: string; receipt?: Receipt };

interface Props {
  msgId: string;
  actions: ActionView[];
  rawActions: ProposedAction[];
  approver: string;
}

function receiptLabel(r: Receipt): string {
  const d = r.executed_at ? new Date(r.executed_at) : new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `Executed ${hh}:${mm} · ${(r.hash || "").slice(0, 6)}`;
}

const STEP_MS = 1600;

// A launched coding agent, shown working. It reads the cited records and checks
// out the repo, then stays on the drafting step: the run is long and its output
// is a draft PR for the owners, so the card never claims it is finished.
function AgentRun({ steps, repo }: { steps: string[]; repo: string }) {
  const working = Math.max(
    0,
    steps.findIndex((s) => s.startsWith("Drafting")),
  );
  const [at, setAt] = useState(0);
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setAt((n) => Math.min(n + 1, working)), STEP_MS);
    const c = setInterval(() => setSecs((n) => n + 1), 1000);
    return () => {
      clearInterval(t);
      clearInterval(c);
    };
  }, [working]);
  const clock = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  return (
    <div className="rounded bg-hover px-3 py-2.5">
      <div className="mb-1.5 flex items-center justify-between text-[12px] text-muted-ink">
        <span>
          Coding agent · <span className="font-mono">{repo}</span>
        </span>
        <span className="font-mono">working {clock}</span>
      </div>
      <div className="flex flex-col gap-1">
        {steps.map((s, i) => {
          const done = i < at;
          const active = i === at;
          return (
            <div
              key={s}
              className={`flex items-center gap-2 text-[13px] ${done || active ? "text-ink" : "text-faint"}`}
            >
              <span className="flex w-3.5 flex-none justify-center">
                {done ? (
                  <span className="text-green">✓</span>
                ) : active ? (
                  <span className="inline-block size-3 animate-spin rounded-full border-2 border-blue border-t-transparent" />
                ) : (
                  <span className="size-1.5 rounded-full bg-line" />
                )}
              </span>
              <span>
                {s}
                {active && "…"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function ApprovalBlock({ msgId, actions, rawActions, approver }: Props) {
  const proposal = useRef<Promise<Proposal> | null>(null);
  const [status, setStatus] = useState<Record<string, Status>>({});
  const [openPayload, setOpenPayload] = useState<Record<string, boolean>>({});

  if (!actions.length) return null;

  const approve = async (id: string) => {
    setStatus((s) => ({ ...s, [id]: { state: "working" } }));
    try {
      if (!proposal.current) {
        proposal.current = api.propose(rawActions);
        proposal.current.catch(() => {
          proposal.current = null;
        });
      }
      const p = await proposal.current;
      await api.approve(p.proposal_id, id, approver);
      const receipt = await api.execute(p.proposal_id, id);
      setStatus((s) => ({ ...s, [id]: { state: "done", receipt } }));
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Something went wrong.";
      setStatus((s) => ({ ...s, [id]: { state: "error", error: msg } }));
    }
  };

  const raw = (id: string) => rawActions.find((r) => r.id === id);
  const isAgent = (id: string) => raw(id)?.type === "agent.launch";
  const running = (id: string) => isAgent(id) && status[id]?.state === "done";
  const pending = actions.filter((a) => status[a.id]?.state !== "done" || running(a.id));
  const done = actions.filter((a) => status[a.id]?.state === "done" && !running(a.id));

  return (
    <div className="overflow-hidden rounded-md border border-line bg-white">
      <div className="flex justify-between border-b border-line px-3.5 py-2.5 text-[13px] font-medium">
        <span>Needs your approval</span>
        <span className="font-normal text-muted-ink">{pending.length ? `${pending.length} pending` : "All done"}</span>
      </div>
      {pending.map((a) => {
        const st = status[a.id] ?? { state: "idle" };
        const isOpen = Boolean(openPayload[a.id]);
        return (
          <div key={a.id} className="flex flex-col gap-2 border-b border-line px-3.5 py-3">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[15px] font-medium">{a.name}</div>
                <div className="mt-0.5 text-[13px] text-muted-ink" style={{ textWrap: "pretty" }}>
                  <span>{a.reason}</span>
                  {a.cites.map((n) => (
                    <CitationChip key={n} msgId={msgId} n={n} small />
                  ))}
                </div>
              </div>
              {running(a.id) ? (
                <span className="flex h-[30px] flex-none items-center rounded bg-chip px-3 text-[13px] font-medium text-muted-ink">
                  Launched
                </span>
              ) : (
                <Button
                  size="sm"
                  disabled={st.state === "working"}
                  onClick={() => approve(a.id)}
                  className="h-[30px] flex-none rounded bg-blue px-3 text-[13px] font-medium text-white shadow-none hover:bg-blue-hover"
                >
                  {st.state === "working"
                    ? "Approving…"
                    : st.state === "error"
                      ? "Retry"
                      : isAgent(a.id)
                        ? "Approve & launch"
                        : "Approve"}
                </Button>
              )}
            </div>
            {running(a.id) && (
              <AgentRun
                steps={(raw(a.id)?.payload?.steps as string[] | undefined) ?? []}
                repo={String(raw(a.id)?.payload?.repo ?? "")}
              />
            )}
            {st.state === "error" && (
              <div className="rounded bg-[#FFE2DD] px-2.5 py-1.5 text-[13px] text-[#5D1715]">{st.error}</div>
            )}
            <div>
              <button
                type="button"
                onClick={() => setOpenPayload((o) => ({ ...o, [a.id]: !isOpen }))}
                className="flex items-center gap-1 border-0 bg-transparent p-0 text-[13px] text-muted-ink"
                aria-expanded={isOpen}
              >
                <span
                  className="inline-block text-[10px] transition-transform duration-150"
                  style={{ transform: isOpen ? "rotate(90deg)" : "none" }}
                >
                  ▸
                </span>
                View payload
              </button>
              {isOpen && (
                <pre className="mt-2 mb-0 overflow-x-auto rounded bg-hover px-3 py-2.5 font-mono text-xs leading-normal text-quote whitespace-pre-wrap break-words">
                  {a.payload}
                </pre>
              )}
            </div>
          </div>
        );
      })}
      {done.length > 0 && (
        <>
          <div className="bg-done-bg px-3.5 pt-2 pb-1 text-xs font-medium text-faint">Done</div>
          {done.map((a) => {
            const r = status[a.id]?.receipt;
            return (
              <div key={a.id} className="flex flex-wrap items-center justify-between gap-3 bg-done-bg px-3.5 pt-2 pb-3">
                <div className="text-sm text-muted-ink">{a.name}</div>
                {r && (
                  <div
                    className="flex h-7 flex-none items-center gap-1.5 whitespace-nowrap rounded-full bg-[#DBEDDB] px-3 text-[13px] font-medium text-[#1C3829]"
                    title={r.result_path}
                  >
                    <div className="size-1.5 rounded-full bg-green" />
                    {receiptLabel(r)}
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
