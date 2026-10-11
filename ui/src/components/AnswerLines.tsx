import type { LineView } from "@/lib/present";
import { CitationChip } from "./CitationChip";

export function AnswerLines({ msgId, lines }: { msgId: string; lines: LineView[] }) {
  return (
    <div className="flex flex-col gap-2.5 text-[17px] leading-normal">
      {lines.map((ln, i) => (
        <div
          // biome-ignore lint/suspicious/noArrayIndexKey: lines are static per answer
          key={i}
          className={ln.quote ? "border-l-2 border-[#D3D1CB] pl-3 text-quote" : undefined}
          style={{ fontWeight: ln.bold ? 600 : 400, textWrap: "pretty" }}
        >
          <span>{ln.text}</span>
          {ln.cites.map((n) => (
            <CitationChip key={n} msgId={msgId} n={n} />
          ))}
        </div>
      ))}
    </div>
  );
}
