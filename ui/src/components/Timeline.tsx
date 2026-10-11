import type { TimelinePoint } from "@/lib/present";

export function Timeline({ points }: { points: TimelinePoint[] }) {
  if (points.length < 2) return null;
  return (
    <div className="px-1 pt-2">
      <div className="relative mx-[60px] mt-2.5 h-0.5 bg-line">
        {points.map((t, i) => (
          <div
            key={t.iso}
            className="absolute top-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center"
            style={{ left: `${(i / (points.length - 1)) * 100}%` }}
          >
            <div
              className="size-2.5 rounded-full border-2 border-white"
              style={{ background: t.color, boxShadow: `0 0 0 1px ${t.color}` }}
            />
            <div className="absolute top-4 w-[130px] text-center">
              <div className="whitespace-nowrap text-xs text-faint">{t.date}</div>
              <div className="text-[13px] leading-[1.3] text-ink" style={{ textWrap: "pretty" }}>
                {t.label}
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="h-[76px]" />
    </div>
  );
}
