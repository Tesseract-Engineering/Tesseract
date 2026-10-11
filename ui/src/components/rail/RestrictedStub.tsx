import { useState } from "react";
import { cn } from "@/lib/utils";
import { Padlock } from "./Padlock";

interface Props {
  n: number;
  owner: string;
  highlighted: boolean;
  rowRef?: (el: HTMLDivElement | null) => void;
}

export function RestrictedStub({ n, owner, highlighted, rowRef }: Props) {
  const [requested, setRequested] = useState(false);
  return (
    <div
      ref={rowRef}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-2 py-3 text-[13px] text-muted-ink transition-shadow",
        highlighted ? "bg-[#EDEDEB] shadow-[0_0_0_1px_#2383E2]" : "bg-chip",
      )}
    >
      <div className="w-4 flex-none text-right text-xs font-medium">{n}</div>
      <Padlock size={14} color="#9B9A97" />
      <div style={{ textWrap: "pretty" }}>
        Restricted · owner: {owner} ·{" "}
        {requested ? (
          <span className="text-ink">Request noted</span>
        ) : (
          <button
            type="button"
            onClick={() => setRequested(true)}
            className="border-0 bg-transparent p-0 text-blue hover:text-ink hover:underline"
          >
            Request access
          </button>
        )}
      </div>
    </div>
  );
}
