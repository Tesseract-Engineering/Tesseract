import type { Company, User } from "@/lib/company";
import { Avatar, SignedInHeader } from "./SignedInHeader";

export function LoginScreen({ company, onPick }: { company: Company; onPick: (u: User) => void }) {
  return (
    <div className="flex h-full flex-col">
      <SignedInHeader company={company.name} user={null} />
      <main className="flex flex-1 items-start justify-center overflow-y-auto px-4">
        <div className="flex w-full max-w-[460px] flex-col gap-7 pt-[120px]">
          <div className="text-center">
            <div className="text-[26px] font-medium tracking-[-0.01em]">Who are you?</div>
            <div className="mt-1.5 text-[15px] text-muted-ink" style={{ textWrap: "pretty" }}>
              Pick a person. Every answer uses their access, so each one sees different sources.
            </div>
          </div>
          <div className="flex flex-col gap-2">
            {company.users.map((u) => (
              <button
                key={u.id}
                type="button"
                onClick={() => onPick(u)}
                className="flex items-center gap-3 rounded-[14px] border border-line bg-white px-4 py-3 text-left text-[15px] text-ink hover:bg-hover"
              >
                <Avatar user={u} />
                <span className="flex-1">
                  <span className="font-medium">{u.name}</span>
                  {u.team && <span className="text-muted-ink"> · {u.team}</span>}
                </span>
                <span className="text-[13px] text-faint">↵</span>
              </button>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
