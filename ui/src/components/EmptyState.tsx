export function EmptyState({
  first,
  suggestions,
  onAsk,
}: {
  first: string;
  suggestions: string[];
  onAsk: (q: string) => void;
}) {
  return (
    <div className="flex flex-col items-center gap-7 pt-[120px]">
      <div className="max-w-[460px] text-center">
        <div className="text-[26px] font-medium tracking-[-0.01em]" style={{ textWrap: "pretty" }}>
          Hi {first}. Ask anything about the company.
        </div>
        <div className="mt-1.5 text-[15px] text-muted-ink" style={{ textWrap: "pretty" }}>
          Every answer comes with a verdict, a few plain lines, and the sources behind them. You only see what you're
          allowed to see.
        </div>
      </div>
      {suggestions.length > 0 && (
        <div className="flex w-full flex-col gap-2">
          {suggestions.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => onAsk(q)}
              className="flex items-center justify-between gap-3 rounded-[14px] border border-line bg-white px-4 py-3 text-left text-[15px] text-ink hover:bg-hover"
            >
              <span>{q}</span>
              <span className="text-[13px] text-faint">↵</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
