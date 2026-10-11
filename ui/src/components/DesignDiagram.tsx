import { useEffect, useState } from "react";
import { ApiError, api, type Diagram, type DiagramItem } from "@/lib/api";

// The existing system a build request should reuse, drawn from the records the
// viewer can read: inputs (one marked as the asker's plug-in point), the
// pipeline stages in order (gates can stop a run), outputs, and the feedback loop.

function Box({ item, n }: { item: DiagramItem; n?: number }) {
  const gate = item.kind === "gate";
  return (
    <div
      className={`relative rounded-md border bg-white px-3 py-2 ${
        item.new ? "border-dashed border-blue bg-[#E7F3F8]" : gate ? "border-[#E9C46A]" : "border-line"
      }`}
      title={item.source || "your addition"}
    >
      <div className="flex items-center gap-2 text-[13px] font-medium">
        {n !== undefined && (
          <span className="flex size-[18px] flex-none items-center justify-center rounded-full bg-chip text-[10px] text-muted-ink">
            {n}
          </span>
        )}
        <span>{item.label}</span>
        {gate && <span className="rounded bg-amber-bg px-1.5 text-[10px] font-medium text-amber-fg">gate</span>}
        {item.new && <span className="rounded bg-blue px-1.5 text-[10px] font-medium text-white">you add this</span>}
      </div>
      {item.detail && <div className="mt-0.5 text-[12px] leading-snug text-muted-ink">{item.detail}</div>}
    </div>
  );
}

function Arrow() {
  return <div className="mx-auto h-4 w-px bg-line" aria-hidden />;
}

export function DesignDiagram({ question, ids, viewer }: { question: string; ids: string[]; viewer: string }) {
  const [state, setState] = useState<{ d?: Diagram | null; why?: string; loading: boolean }>({ loading: true });

  useEffect(() => {
    let live = true;
    api
      .design(question, viewer, ids)
      .then((r) => live && setState({ d: r.diagram, why: r.why, loading: false }))
      .catch((e) => live && setState({ d: null, why: e instanceof ApiError ? e.message : "failed", loading: false }));
    return () => {
      live = false;
    };
  }, [question, viewer, ids]);

  if (state.loading) {
    return (
      <div className="rounded-md border border-line px-3.5 py-3 text-[13px] text-muted-ink">
        <span className="mr-2 inline-block size-2 animate-pulse rounded-full bg-blue align-middle" />
        Drawing the system design from {ids.length} source{ids.length === 1 ? "" : "s"}…
      </div>
    );
  }
  const d = state.d;
  if (!d) return null;

  return (
    <div className="fade-up overflow-hidden rounded-md border border-line bg-white">
      <div className="flex items-center justify-between border-b border-line px-3.5 py-2.5 text-[13px] font-medium">
        <span>System design · {d.title}</span>
        <span className="font-normal text-muted-ink">existing, drawn from {d.sources.length} records</span>
      </div>
      <div className="flex flex-col px-3.5 py-3.5">
        <div className="mb-1 text-[11px] font-medium tracking-wide text-faint uppercase">Inputs</div>
        <div className="grid grid-cols-2 gap-2">
          {d.inputs.map((it) => (
            <Box key={it.label} item={it} />
          ))}
        </div>
        <Arrow />
        <div className="relative flex gap-3">
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="mb-1 text-[11px] font-medium tracking-wide text-faint uppercase">Pipeline</div>
            {d.stages.map((s, i) => (
              <div key={`${s.label}:${s.source}`} className="flex flex-col">
                <Box item={s} n={i + 1} />
                {i < d.stages.length - 1 && <Arrow />}
              </div>
            ))}
          </div>
          {d.loop && (
            <div className="flex w-[30%] flex-none flex-col pt-5">
              <div className="flex h-full flex-col justify-center rounded-md border border-dashed border-[#E9C46A] bg-[#FFFBF0] px-2.5 py-2">
                <div className="text-[12px] font-medium">↺ {d.loop.label}</div>
                <div className="mt-0.5 text-[11px] leading-snug text-muted-ink">{d.loop.detail}</div>
              </div>
            </div>
          )}
        </div>
        {d.outputs.length > 0 && (
          <>
            <Arrow />
            <div className="mb-1 text-[11px] font-medium tracking-wide text-faint uppercase">Outputs</div>
            <div className="grid grid-cols-2 gap-2">
              {d.outputs.map((it) => (
                <Box key={it.label} item={it} />
              ))}
            </div>
          </>
        )}
        {d.plug_in && (
          <div className="mt-3.5 rounded bg-[#E7F3F8] px-3 py-2 text-[13px] leading-snug">
            <span className="font-medium">Where you plug in: </span>
            {d.plug_in}
          </div>
        )}
        <div className="mt-2.5 text-[11px] text-faint">
          Sources: {d.sources.map((s) => s.title || s.id).join(" · ")}
        </div>
      </div>
    </div>
  );
}
