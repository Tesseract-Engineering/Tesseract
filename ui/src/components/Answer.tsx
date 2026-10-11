import type { AnswerView } from "@/lib/present";
import { AnswerLines } from "./AnswerLines";
import { ApprovalBlock } from "./ApprovalBlock";
import { DesignDiagram } from "./DesignDiagram";
import { NotFoundBlock } from "./NotFoundBlock";
import { Timeline } from "./Timeline";
import { VerdictPill } from "./VerdictPill";
import { WhoToTalkTo } from "./WhoToTalkTo";
import { WorkingLines } from "./WorkingLines";

export interface Message {
  id: string;
  q: string;
  status: "working" | "ready" | "error";
  shown: number;
  work: string[];
  view?: AnswerView;
  error?: string;
}

interface Props {
  m: Message;
  first: string;
  approver: string;
  viewer: string;
}

export function Answer({ m, first, approver, viewer }: Props) {
  return (
    <div className="flex flex-col gap-[22px]">
      <div className="flex justify-end">
        <div
          className="max-w-[78%] rounded-[18px] bg-chip px-4 py-2.5 text-[15px] leading-normal"
          style={{ textWrap: "pretty" }}
        >
          {m.q}
        </div>
      </div>

      {m.status === "working" && <WorkingLines lines={m.work} shown={m.shown} />}

      {m.status === "error" && (
        <div className="rounded bg-[#FFE2DD] px-3.5 py-3 text-sm text-[#5D1715]">
          {m.error ?? "Something went wrong."}
        </div>
      )}

      {m.status === "ready" && m.view && (
        <div className="fade-up flex flex-col gap-6">
          <VerdictPill pill={m.view.pill} first={first} />
          <AnswerLines msgId={m.id} lines={m.view.lines} />
          {m.view.design && <DesignDiagram question={m.view.design.question} ids={m.view.design.ids} viewer={viewer} />}
          <Timeline points={m.view.timeline} />
          <WhoToTalkTo people={m.view.people} />
          <ApprovalBlock msgId={m.id} actions={m.view.actions} rawActions={m.view.rawActions} approver={approver} />
          {m.view.notFound && <NotFoundBlock nf={m.view.notFound} />}
          {m.view.footer && <div className="text-[13px] text-faint">{m.view.footer}</div>}
        </div>
      )}
    </div>
  );
}
