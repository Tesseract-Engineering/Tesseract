import { useState } from "react";

export function Composer({ onSend, disabled }: { onSend: (q: string) => void; disabled?: boolean }) {
  const [value, setValue] = useState("");
  const ready = value.trim().length > 0 && !disabled;
  const send = () => {
    if (!ready) return;
    onSend(value.trim());
    setValue("");
  };
  return (
    <div className="flex-none bg-white px-5 pt-2 pb-4">
      <div className="mx-auto max-w-[680px] px-5">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
          className="flex h-[52px] items-center gap-2.5 rounded-[26px] border border-line bg-white pr-2 pl-5 shadow-[0_2px_8px_rgba(15,15,15,.06)]"
        >
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Ask anything about the company"
            aria-label="Question"
            className="min-w-0 flex-1 border-0 bg-transparent text-[15px] outline-0 placeholder:text-faint"
          />
          <button
            type="submit"
            aria-label="Send"
            disabled={!ready}
            className="flex size-9 flex-none items-center justify-center rounded-full border-0 text-base transition-colors"
            style={{ background: ready ? "#2383E2" : "#E9E9E7", color: ready ? "#fff" : "#9B9A97" }}
          >
            ↑
          </button>
        </form>
        <div className="mt-2 text-center text-xs text-faint">
          Every answer cites its sources. You only see what you have access to.
        </div>
      </div>
    </div>
  );
}
