import { useEffect, useRef, useState } from "react";
import { ApiError, api, type DocResponse } from "@/lib/api";
import { findQuote } from "@/lib/highlight";
import type { SourceView } from "@/lib/present";
import { prettyDate } from "@/lib/present";
import { cn } from "@/lib/utils";
import { Padlock } from "./Padlock";
import { SystemLogo } from "./SystemLogo";

interface Props {
  row: SourceView;
  viewer: string;
  open: boolean;
  highlighted: boolean;
  onToggle: () => void;
  rowRef?: (el: HTMLDivElement | null) => void;
}

function grantLabel(group: string): string {
  if (!group) return "";
  if (group.includes(":user:")) return " · in your mailbox";
  if (group.startsWith("sharepoint:"))
    return ` · you're in ${group.split(":")[1].replace(/\b\w/g, (c) => c.toUpperCase())}`;
  return ` · via ${group}`;
}

type DocState = { state: "loading" } | { state: "ok"; doc: DocResponse } | { state: "error"; message: string };

function FullText({ text, quote }: { text: string; quote: string }) {
  const markRef = useRef<HTMLElement | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const span = findQuote(text, quote);
  useEffect(() => {
    const box = boxRef.current;
    const mark = markRef.current;
    if (box && mark) box.scrollTop = Math.max(0, mark.offsetTop - box.offsetTop - 40);
  }, []);
  return (
    <div
      ref={boxRef}
      className="no-scrollbar max-h-[320px] overflow-y-auto whitespace-pre-wrap break-words rounded bg-hover p-3 text-[13px] leading-[1.6] text-ink"
      style={{ textWrap: "pretty" }}
    >
      {span ? (
        <>
          {text.slice(0, span[0])}
          <mark ref={markRef} className="rounded-sm bg-amber-bg px-0.5 py-px text-ink">
            {text.slice(span[0], span[1])}
          </mark>
          {text.slice(span[1])}
        </>
      ) : (
        text
      )}
    </div>
  );
}

export function SourceRow({ row, viewer, open, highlighted, onToggle, rowRef }: Props) {
  const [doc, setDoc] = useState<DocState | null>(null);

  useEffect(() => {
    if (!open || doc || !row.nodeId) return;
    setDoc({ state: "loading" });
    api
      .doc(row.nodeId, viewer)
      .then((d) => setDoc({ state: "ok", doc: d }))
      .catch((e) =>
        setDoc({
          state: "error",
          message:
            e instanceof ApiError && e.status === 403
              ? e.message
              : e instanceof ApiError && e.status === 404
                ? "No full text for this source."
                : "Couldn't load the full text.",
        }),
      );
  }, [open, doc, row.nodeId, viewer]);

  const meta: [string, string][] = [];
  if (doc?.state === "ok") {
    const d = doc.doc;
    meta.push(["Author", d.author || row.author || "Unknown"]);
    meta.push(["Date", d.date ? prettyDate(d.date) : row.date || "Undated"]);
    meta.push(["System", row.system.name]);
    meta.push([
      "Access",
      d.visibility === "internal" || d.visibility === "public"
        ? `${d.visibility.charAt(0).toUpperCase()}${d.visibility.slice(1)}`
        : `${d.visibility.charAt(0).toUpperCase()}${d.visibility.slice(1)}${grantLabel(d.access)}`,
    ]);
    if (d.path) meta.push(["Path", d.path]);
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: the row holds nested interactive content
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
      className={cn(
        "flex cursor-pointer gap-2.5 rounded-md px-2 py-2.5 transition-[background,box-shadow] duration-100 hover:bg-hover",
        highlighted && "bg-[#E7F3F8] shadow-[0_0_0_1px_#2383E2] hover:bg-[#E7F3F8]",
        row.muted && "opacity-80",
      )}
    >
      <div className="w-4 flex-none pt-0.5 text-right text-xs font-medium text-faint">{row.n}</div>
      <div className="flex min-w-0 flex-1 flex-col gap-[5px]">
        <div className="flex min-w-0 items-center gap-2">
          <SystemLogo system={row.system} />
          <div className="min-w-0 text-sm leading-[1.35]" style={{ textWrap: "pretty" }}>
            <span className="text-muted-ink">{row.system.name} · </span>
            <span className={cn("font-medium break-words", row.muted && "font-normal text-quote")}>{row.title}</span>
          </div>
        </div>
        {row.quote && (
          <div
            className="border-l-2 border-[#D3D1CB] pl-2.5 text-sm leading-[1.45] text-quote"
            style={{ textWrap: "pretty" }}
          >
            “{row.quote}”
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 text-xs text-faint">
          {row.date && <span>{row.date}</span>}
          {row.author && <span>{row.author}</span>}
          {row.note && <span className="text-muted-ink">{row.note}</span>}
          {row.askCount > 0 && (
            <span className="whitespace-nowrap rounded-full bg-chip px-2 py-px text-quote">
              {row.askCount} {row.askCount === 1 ? "person" : "people"} asked about this
            </span>
          )}
          {row.accessLabel && (
            <span className="inline-flex items-center gap-1 whitespace-nowrap text-muted-ink">
              <Padlock />
              {row.accessLabel}
            </span>
          )}
        </div>
        {open && (
          // biome-ignore lint/a11y/noStaticElementInteractions: stops the row from collapsing while reading
          // biome-ignore lint/a11y/useKeyWithClickEvents: same
          <div className="mt-1.5 flex cursor-auto flex-col gap-2.5" onClick={(e) => e.stopPropagation()}>
            {!row.nodeId && <div className="text-[13px] text-muted-ink">No full text for this source.</div>}
            {doc?.state === "loading" && <div className="text-[13px] text-muted-ink">Loading full text…</div>}
            {doc?.state === "error" && <div className="text-[13px] text-muted-ink">{doc.message}</div>}
            {doc?.state === "ok" && (
              <>
                {doc.doc.text ? (
                  <FullText text={doc.doc.text} quote={row.quoteRaw} />
                ) : (
                  <div className="text-[13px] text-muted-ink">This source has no text body.</div>
                )}
                <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                  {meta.map(([k, v]) => (
                    <div key={k} className="contents">
                      <div className="text-faint">{k}</div>
                      <div className="break-words text-ink">{v}</div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
