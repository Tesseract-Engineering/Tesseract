import type { Pill } from "@/lib/present";

export function VerdictPill({ pill, first }: { pill: Pill; first: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <div className="rounded px-2.5 py-[3px] text-[13px] font-medium" style={{ background: pill.bg, color: pill.fg }}>
        {pill.label}
      </div>
      <div className="text-[13px] text-muted-ink">Using {first}'s access</div>
    </div>
  );
}
