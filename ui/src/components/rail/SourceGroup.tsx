import type { GroupView } from "@/lib/present";
import { RestrictedStub } from "./RestrictedStub";
import { SourceRow } from "./SourceRow";

interface Props {
  group: GroupView;
  viewer: string;
  isOpen: (n: number) => boolean;
  hovered: number | null;
  onToggle: (n: number) => void;
  registerRow: (n: number, el: HTMLDivElement | null) => void;
}

export function SourceGroup({ group, viewer, isOpen, hovered, onToggle, registerRow }: Props) {
  return (
    <div>
      <div
        className="px-2 pt-3 pb-1.5 text-[11px] font-semibold tracking-[0.06em] uppercase"
        style={{ color: group.color }}
      >
        {group.title} ({group.rows.length})
      </div>
      {group.rows.map((r) =>
        r.restricted ? (
          <RestrictedStub
            key={r.n}
            n={r.n}
            owner={r.owner}
            highlighted={hovered === r.n}
            rowRef={(el) => registerRow(r.n, el)}
          />
        ) : (
          <SourceRow
            key={r.n}
            row={r}
            viewer={viewer}
            open={isOpen(r.n)}
            highlighted={hovered === r.n}
            onToggle={() => onToggle(r.n)}
            rowRef={(el) => registerRow(r.n, el)}
          />
        ),
      )}
    </div>
  );
}
