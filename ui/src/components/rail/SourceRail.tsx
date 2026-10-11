import type { SystemCount } from "@/lib/api";
import type { AnswerView } from "@/lib/present";
import { SourceGroup } from "./SourceGroup";
import { SystemsList } from "./SystemsList";

interface Props {
  answer: AnswerView | null;
  viewer: string;
  systems: SystemCount[] | null;
  systemsError?: string;
  isOpen: (n: number) => boolean;
  hovered: number | null;
  onToggle: (n: number) => void;
  registerRow: (n: number, el: HTMLDivElement | null) => void;
}

export function SourceRail({ answer, viewer, systems, systemsError, isOpen, hovered, onToggle, registerRow }: Props) {
  const heading = answer ? answer.railHeading : "Connected systems";
  return (
    <aside className="hidden w-[380px] flex-none flex-col border-l border-line bg-white lg:flex">
      <div className="px-4 pt-3.5 pb-2.5 text-[13px] font-semibold">{heading}</div>
      <div className="no-scrollbar flex-1 overflow-y-auto px-2 pb-6">
        {!answer && <SystemsList systems={systems} error={systemsError} />}
        {answer && !answer.groups.length && (
          <div className="px-2 py-2 text-[13px] text-muted-ink">No sources for this answer.</div>
        )}
        {answer?.groups.map((g) => (
          <SourceGroup
            key={g.title}
            group={g}
            viewer={viewer}
            isOpen={isOpen}
            hovered={hovered}
            onToggle={onToggle}
            registerRow={registerRow}
          />
        ))}
      </div>
    </aside>
  );
}
