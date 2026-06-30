// Shared UI types + the remaining static mock for the /author surface.
//
// /ask, /inbox, /console are wired to the real backend now — their mock data was
// removed. What stays here: shared UI types (SourceCard, RoutingStep, ConsoleMetric)
// still imported by live components, plus /author's mock (authorSteps, conceptDrafts),
// which remains owner-side gate-external. Domain language follows CONTEXT.md.

import type { StatusTone } from "@/components/ui/status-badge";

/* ---------------------------- /ask — shared types ----------------------------- */

export interface SourceCard {
  id: string;
  conceptId: string;
  title: string;
  owner: string;
  domain: string;
  updatedAt: string;
}

export interface RoutingStep {
  id: string;
  label: string;
  state: "done" | "active" | "pending";
}

/* --------------------------- /author — OKF (mock) --------------------------- */

export interface AuthorStep {
  id: string;
  label: string;
  state: "done" | "active" | "pending";
}

export const authorSteps: AuthorStep[] = [
  { id: "ingest", label: "인제스트", state: "done" },
  { id: "split", label: "개념 분할", state: "active" },
  { id: "core", label: "core_question", state: "pending" },
  { id: "link", label: "연결", state: "pending" },
  { id: "index", label: "인덱싱·배포", state: "pending" },
];

export type ConceptDisposition = "unreviewed" | "edited" | "rejected";

export interface ConceptDraft {
  id: string;
  conceptId: string;
  domain: string;
  inDomain: boolean;
  disposition: ConceptDisposition;
  title: string;
  coreQuestion: string;
  coreQuestionOriginal?: string;
  bodyPreview: string;
  rejectReason?: string;
}

export const conceptDrafts: ConceptDraft[] = [
  {
    id: "c1",
    conceptId: "OKF-HR-0512",
    domain: "인사",
    inDomain: true,
    disposition: "unreviewed",
    title: "연차 이월 규정",
    coreQuestion: "남은 연차는 다음 해로 이월되나요?",
    bodyPreview:
      "미사용 연차는 최대 5일까지 익년 6월 말까지 이월됩니다. 이월 신청은 12월 회계 마감 전에 인사팀에 제출합니다.",
  },
  {
    id: "c2",
    conceptId: "OKF-HR-0513",
    domain: "인사",
    inDomain: true,
    disposition: "edited",
    title: "경조 휴가 일수",
    coreQuestion: "본인 결혼 시 경조 휴가는 며칠인가요?",
    coreQuestionOriginal: "결혼하면 휴가 얼마나 줘요?",
    bodyPreview:
      "본인 결혼 5일(영업일), 자녀 결혼 1일, 배우자 출산 10일. 휴가는 사유 발생일로부터 30일 이내 사용합니다.",
  },
  {
    id: "c3",
    conceptId: "OKF-LEG-0088",
    domain: "법무",
    inDomain: false,
    disposition: "rejected",
    title: "표준 NDA 유효기간",
    coreQuestion: "비밀유지계약의 기본 유효기간은 몇 년인가요?",
    bodyPreview:
      "표준 NDA 유효기간은 계약 종료 후 3년입니다. 다만 영업비밀에 해당하는 정보는 기간 제한 없이 보호됩니다.",
    rejectReason: "권한 밖 domain(법무) — 배포 제외",
  },
];

/* --------------------------- /console — shared type ------------------------------- */

export interface ConsoleMetric {
  id: string;
  label: string;
  value: string;
  unit: string;
  tone: StatusTone;
  hint: string;
}
