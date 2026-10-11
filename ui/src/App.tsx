import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Answer, type Message } from "@/components/Answer";
import { Composer } from "@/components/Composer";
import { CiteContext, type CiteState } from "@/components/cite-context";
import { EmptyState } from "@/components/EmptyState";
import { LoginScreen } from "@/components/LoginScreen";
import { SourceRail } from "@/components/rail/SourceRail";
import { SignedInHeader } from "@/components/SignedInHeader";
import { ApiError, api, type SystemCount } from "@/lib/api";
import { type Company, loadCompany, loadUser, saveUser, type User } from "@/lib/company";
import { present, workingLines } from "@/lib/present";
import { systemOf } from "@/lib/systems";

const MIN_WORKING_MS = 2000;
const STEP_MS = 650;

export default function App() {
  const [company, setCompany] = useState<Company | null>(null);
  const [companyError, setCompanyError] = useState<string>();
  const [user, setUser] = useState<User | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [systems, setSystems] = useState<SystemCount[] | null>(null);
  const [systemsError, setSystemsError] = useState<string>();
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const [hovered, setHoveredState] = useState<{ msgId: string; n: number } | null>(null);
  const [openRows, setOpenRows] = useState<Record<string, boolean>>({});
  const rowEls = useRef(new Map<number, HTMLDivElement>());
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadCompany()
      .then((c) => {
        setCompany(c);
        setUser(loadUser(c));
      })
      .catch((e) => setCompanyError(e instanceof ApiError ? e.message : "Couldn't load the company."));
  }, []);

  useEffect(() => {
    if (!user) return;
    setSystems(null);
    setSystemsError(undefined);
    api
      .systems(user.viewer)
      .then((r) => setSystems(r.systems))
      .catch((e) => setSystemsError(e instanceof ApiError ? e.message : "Couldn't load systems."));
  }, [user]);

  const lastReady = [...messages].reverse().find((m) => m.status === "ready");
  const activeId = pinnedId && messages.some((m) => m.id === pinnedId) ? pinnedId : (lastReady?.id ?? null);
  const active = messages.find((m) => m.id === activeId) ?? null;

  const update = useCallback((id: string, patch: Partial<Message>) => {
    setMessages((ms) => ms.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }, []);

  const ask = useCallback(
    (q: string) => {
      if (!user) return;
      const id = `m${Date.now()}`;
      const names = [...new Set((systems ?? []).map((s) => systemOf(s.source).name))];
      setPinnedId(null);
      setMessages((ms) => [...ms, { id, q, status: "working", shown: 1, work: workingLines(names) }]);
      const started = Date.now();
      const t2 = setTimeout(() => update(id, { shown: 2 }), STEP_MS);
      const t3 = setTimeout(() => update(id, { shown: 3 }), STEP_MS * 2);
      const finish = (patch: Partial<Message>) => {
        const wait = Math.max(0, MIN_WORKING_MS - (Date.now() - started));
        setTimeout(() => {
          clearTimeout(t2);
          clearTimeout(t3);
          update(id, patch);
        }, wait);
      };
      api
        .ask(q, user.viewer, user.name)
        .then((res) => {
          update(id, { work: workingLines(names, res) });
          let view: ReturnType<typeof present>;
          try {
            view = present(res);
          } catch {
            finish({ status: "error", error: "The answer came back in a shape I couldn't read." });
            return;
          }
          finish({ status: "ready", view });
        })
        .catch((e) => finish({ status: "error", error: e instanceof ApiError ? e.message : "Something went wrong." }));
    },
    [user, systems, update],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll when a question is added or an answer lands
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages.length, lastReady?.id]);

  const cite: CiteState = useMemo(
    () => ({
      activeId,
      hovered: hovered && hovered.msgId === activeId ? hovered.n : null,
      setHovered: (msgId, n) => {
        if (n === null) setHoveredState(null);
        else {
          setPinnedId(msgId);
          setHoveredState({ msgId, n });
        }
      },
      open: (msgId, n) => {
        setPinnedId(msgId);
        setOpenRows((o) => ({ ...o, [`${msgId}:${n}`]: true }));
        setTimeout(() => rowEls.current.get(n)?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 60);
      },
    }),
    [activeId, hovered],
  );

  if (!company) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-[15px] text-muted-ink">
        {companyError ?? "Loading…"}
      </div>
    );
  }

  if (!user) {
    return (
      <LoginScreen
        company={company}
        onPick={(u) => {
          saveUser(u);
          setUser(u);
        }}
      />
    );
  }

  const signOut = () => {
    saveUser(null);
    setUser(null);
    setMessages([]);
    setOpenRows({});
    setPinnedId(null);
  };

  const working = messages.some((m) => m.status === "working");

  return (
    <CiteContext.Provider value={cite}>
      <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-white text-[15px] leading-[1.45] text-ink">
        <SignedInHeader company={company.name} user={user} onSignOut={signOut} />
        <div className="relative flex min-h-0 flex-1 items-stretch">
          <div className="flex min-w-0 flex-1 flex-col">
            <div ref={scrollRef} className="no-scrollbar relative min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto flex max-w-[680px] flex-col gap-10 px-6 pt-10 pb-6">
                {messages.length === 0 && (
                  <EmptyState first={user.first} suggestions={company.suggestions} onAsk={ask} />
                )}
                {messages.map((m) => (
                  <Answer key={m.id} m={m} first={user.first} approver={user.handle} viewer={user.viewer} />
                ))}
              </div>
            </div>
            <Composer onSend={ask} disabled={working} />
          </div>
          <SourceRail
            answer={active?.view ?? null}
            viewer={user.viewer}
            systems={systems}
            systemsError={systemsError}
            isOpen={(n) => Boolean(activeId && openRows[`${activeId}:${n}`])}
            hovered={cite.hovered}
            onToggle={(n) => activeId && setOpenRows((o) => ({ ...o, [`${activeId}:${n}`]: !o[`${activeId}:${n}`] }))}
            registerRow={(n, el) => {
              if (el) rowEls.current.set(n, el);
              else rowEls.current.delete(n);
            }}
          />
        </div>
      </div>
    </CiteContext.Provider>
  );
}
