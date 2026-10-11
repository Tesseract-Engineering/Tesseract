import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { User } from "@/lib/company";

export function Avatar({ user, size = 30 }: { user: Pick<User, "init" | "bg">; size?: number }) {
  return (
    <div
      className="flex flex-none items-center justify-center rounded-full font-semibold text-ink"
      style={{ width: size, height: size, background: user.bg, fontSize: size * 0.37 }}
    >
      {user.init}
    </div>
  );
}

export function Wordmark({ company }: { company?: string }) {
  return (
    <div className="flex flex-none items-center gap-2.5">
      <div className="whitespace-nowrap text-[15px] font-semibold tracking-[-0.01em]">Tesseract</div>
      {company && (
        <span className="whitespace-nowrap rounded-full bg-chip px-2 py-0.5 text-[11px] font-medium text-muted-ink">
          {company}
        </span>
      )}
    </div>
  );
}

export function SignedInHeader({
  company,
  user,
  onSignOut,
}: {
  company?: string;
  user: User | null;
  onSignOut?: () => void;
}) {
  return (
    <header className="flex h-14 flex-none items-center gap-4 border-b border-line px-5">
      <Wordmark company={company} />
      {user && (
        <div className="ml-auto flex flex-none items-center gap-2.5">
          <div className="whitespace-nowrap text-[13px]">
            <span className="font-medium">{user.name}</span>
            {user.team && <span className="text-muted-ink"> · {user.team}</span>}
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" aria-label="Account menu" className="rounded-full border-0 bg-transparent p-0">
                <Avatar user={user} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              <DropdownMenuLabel className="font-normal">
                <div className="text-[13px] font-medium">{user.name}</div>
                <div className="text-xs text-muted-ink">{user.team}</div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={onSignOut} className="text-[13px]">
                Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </header>
  );
}
