export function WorkingLines({ lines, shown }: { lines: string[]; shown: number }) {
  return (
    <div className="flex flex-col gap-2 text-[13px] text-muted-ink">
      {lines.map((text, i) => (
        <div
          key={text}
          className="flex items-center gap-2 transition-opacity duration-300"
          style={{ opacity: i < shown ? 1 : 0 }}
        >
          <div className="size-1.5 rounded-full bg-faint" />
          {text}
        </div>
      ))}
    </div>
  );
}
