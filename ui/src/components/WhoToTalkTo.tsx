import type { PersonView } from "@/lib/present";

export function WhoToTalkTo({ people }: { people: PersonView[] }) {
  if (!people.length) return null;
  return (
    <div className="flex flex-col gap-2">
      <div className="text-[13px] font-medium text-muted-ink">Who to talk to</div>
      <div className="flex flex-col gap-0.5">
        {people.map((p) => (
          <div key={p.name} className="flex items-center gap-2.5 rounded px-2 py-1.5 hover:bg-hover">
            <div
              className="flex size-[26px] flex-none items-center justify-center rounded-full text-[10px] font-semibold text-ink"
              style={{ background: p.bg }}
            >
              {p.init}
            </div>
            <div className="truncate text-sm">
              <span className="font-medium">{p.name}</span>
              <span className="text-muted-ink">, {p.role}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
