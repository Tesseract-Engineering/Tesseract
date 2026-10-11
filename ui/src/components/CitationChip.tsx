import { cn } from "@/lib/utils";
import { useCite } from "./cite-context";

interface Props {
  msgId: string;
  n: number;
  small?: boolean;
}

export function CitationChip({ msgId, n, small }: Props) {
  const { setHovered, open } = useCite();
  return (
    <button
      type="button"
      aria-label={`Source ${n}`}
      onClick={() => open(msgId, n)}
      onMouseEnter={() => setHovered(msgId, n)}
      onMouseLeave={() => setHovered(msgId, null)}
      onFocus={() => setHovered(msgId, n)}
      onBlur={() => setHovered(msgId, null)}
      className={cn(
        "inline-flex items-center justify-center border border-line bg-white font-semibold leading-none text-blue transition-colors hover:border-blue hover:bg-blue hover:text-white",
        small
          ? "ml-[3px] h-4 min-w-4 rounded-[3px] px-1 align-[1px] text-[10px]"
          : "ml-1 h-[18px] min-w-[18px] rounded px-[5px] align-[2px] text-[11px]",
      )}
    >
      {n}
    </button>
  );
}
