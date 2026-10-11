import type { SystemCount } from "@/lib/api";
import { systemOf } from "@/lib/systems";
import { SystemLogo } from "./SystemLogo";

export function SystemsList({ systems, error }: { systems: SystemCount[] | null; error?: string }) {
  if (error) return <div className="px-2 py-2 text-[13px] text-muted-ink">{error}</div>;
  if (!systems) return <div className="px-2 py-2 text-[13px] text-faint">Loading systems…</div>;
  return (
    <div>
      {systems.map((s) => {
        const sys = systemOf(s.source);
        return (
          <div key={s.source} className="flex items-center gap-2.5 rounded p-2 text-sm">
            <SystemLogo system={sys} />
            <span className="flex-1">{sys.name}</span>
            <span className="text-xs text-faint">{s.documents.toLocaleString()} docs</span>
            <div className="size-1.5 rounded-full bg-green" />
          </div>
        );
      })}
    </div>
  );
}
