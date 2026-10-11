import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { NotFoundView } from "@/lib/present";
import { Padlock } from "./rail/Padlock";
import { SystemLogo } from "./rail/SystemLogo";

export function NotFoundBlock({ nf }: { nf: NotFoundView }) {
  const [open, setOpen] = useState(false);
  const [requested, setRequested] = useState(false);
  return (
    <>
      <div className="flex flex-col gap-2.5">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex items-center gap-1 self-start border-0 bg-transparent p-0 text-[13px] font-medium text-muted-ink"
        >
          <span
            className="inline-block text-[10px] transition-transform duration-150"
            style={{ transform: open ? "rotate(90deg)" : "none" }}
          >
            ▸
          </span>
          Search details
        </button>
        {open && (
          <div className="flex flex-col gap-2.5">
            <div className="flex flex-wrap gap-1.5">
              {nf.looked.map((l) => (
                <div
                  key={l.system.key}
                  className="flex h-7 items-center gap-[7px] whitespace-nowrap rounded-full border border-line bg-white px-2.5 text-[13px] text-ink"
                >
                  <SystemLogo system={l.system} size={14} ink="#37352F" />
                  {l.system.name}
                  <span className="text-faint">{l.count.toLocaleString()}</span>
                </div>
              ))}
            </div>
            {nf.terms.length > 0 && (
              <div className="text-[13px] text-muted-ink">Looked for: {nf.terms.map((t) => `“${t}”`).join(", ")}</div>
            )}
            {nf.reason && <div className="text-[13px] text-faint">{nf.reason}</div>}
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded bg-amber-bg px-3.5 py-3 text-amber-fg">
        <div className="flex items-center gap-2 text-sm">
          <Padlock size={14} color="#402C1B" />
          {nf.callout}
        </div>
        {nf.hasRestricted && (
          <Button
            size="sm"
            disabled={requested}
            onClick={() => setRequested(true)}
            className="h-[30px] whitespace-nowrap rounded bg-blue px-3 text-[13px] font-medium text-white shadow-none hover:bg-blue-hover disabled:opacity-100"
          >
            {requested ? `Request noted for ${nf.owner}` : "Request access"}
          </Button>
        )}
      </div>
    </>
  );
}
