// Static mock data for the four product surfaces. No backend wiring yet.
// Domain language follows CONTEXT.md (Owner, OKF, core_question, Contested,
// Backup, Escalation, Unowned). End-user surfaces never expose routing internals
// (scores, candidate math) — only owner / confidence / source.

import type { StatusTone } from "@/components/ui/status-badge";

/* ---------------------------- (A) /ask — chat ---------------------------- */

export type ConfidenceState = "approved" | "draft" | "backup";

export const confidenceMeta: Record<
  ConfidenceState,
  { label: string; tone: StatusTone }
> = {
  approved: { label: "승인 완료", tone: "success" },
  draft: { label: "초안 대기", tone: "warning" },
  backup: { label: "백업 답변", tone: "info" },
};

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

export interface ChatTurn {
  id: string;
  role: "user" | "org";
  text: string;
  owner?: string;
  ownerDomain?: string;
  confidence?: ConfidenceState;
  sources?: SourceCard[];
  trace?: RoutingStep[];
}

export const askThread: ChatTurn[] = [
  {
    id: "u1",
    role: "user",
    text: "신규 입사자 노트북은 입사 며칠 전까지 신청해야 하나요?",
  },
  {
    id: "o1",
    role: "org",
    owner: "it_ops",
    ownerDomain: "정보시스템",
    confidence: "approved",
    text: "입사 5영업일 전까지 자산관리 포털에서 신청하면 입사 첫날 지급됩니다. 5일 미만으로 촉박하면 it_ops에 직접 요청해 임시 장비를 받을 수 있습니다.",
    trace: [
      { id: "t1", label: "담당 찾는 중", state: "done" },
      { id: "t2", label: "it_ops 전달됨", state: "done" },
      { id: "t3", label: "답변 작성 중", state: "done" },
    ],
    sources: [
      {
        id: "s1",
        conceptId: "OKF-IT-0142",
        title: "신규 입사자 장비 지급 절차",
        owner: "it_ops",
        domain: "정보시스템",
        updatedAt: "3일 전 승인",
      },
    ],
  },
  {
    id: "u2",
    role: "user",
    text: "출장 숙박비 한도는 직급별로 어떻게 되나요?",
  },
  {
    id: "o2",
    role: "org",
    owner: "finance_ops",
    ownerDomain: "재무",
    confidence: "draft",
    text: "현재 직급별 숙박비 한도표 개정안이 검토 중입니다. 초안 기준 사원·대리 12만원, 과장·차장 15만원, 부장 이상 18만원입니다. 승인 전 초안이라 실제 정산 시 달라질 수 있습니다.",
    trace: [
      { id: "t1", label: "담당 찾는 중", state: "done" },
      { id: "t2", label: "finance_ops 전달됨", state: "done" },
      { id: "t3", label: "답변 작성 중", state: "active" },
    ],
    sources: [
      {
        id: "s2",
        conceptId: "OKF-FIN-0307",
        title: "국내 출장 경비 한도",
        owner: "finance_ops",
        domain: "재무",
        updatedAt: "초안 · 미승인",
      },
    ],
  },
];

/* --------------------------- (B) /author — OKF --------------------------- */

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

/* ---------------------------- (C) /inbox -------------------------------- */

export interface CandidateCoverage {
  owner: string;
  domain: string;
  matchedConcepts: string[];
  coverageLabel: string;
  coverageTone: StatusTone;
}

export interface ContestedCase {
  id: string;
  question: string;
  askedAt: string;
  candidates: CandidateCoverage[];
  relatedConcepts: string[];
}

export const contestedCases: ContestedCase[] = [
  {
    id: "ct1",
    question: "재택근무 중 발생한 통신비도 비용 처리가 되나요?",
    askedAt: "12분 전",
    relatedConcepts: ["재택근무 규정", "복리후생 비용", "통신비 보조"],
    candidates: [
      {
        owner: "hr_ops",
        domain: "인사",
        matchedConcepts: ["재택근무 규정", "복리후생 비용"],
        coverageLabel: "개념 2건 매칭",
        coverageTone: "success",
      },
      {
        owner: "finance_ops",
        domain: "재무",
        matchedConcepts: ["통신비 보조"],
        coverageLabel: "개념 1건 매칭",
        coverageTone: "warning",
      },
    ],
  },
  {
    id: "ct2",
    question: "협력사 보안 점검 주기는 분기인가요 반기인가요?",
    askedAt: "41분 전",
    relatedConcepts: ["협력사 관리", "정보보안 점검"],
    candidates: [
      {
        owner: "security_ops",
        domain: "정보보안",
        matchedConcepts: ["정보보안 점검"],
        coverageLabel: "개념 1건 매칭",
        coverageTone: "warning",
      },
      {
        owner: "contract_ops",
        domain: "구매·계약",
        matchedConcepts: ["협력사 관리"],
        coverageLabel: "개념 1건 매칭",
        coverageTone: "warning",
      },
    ],
  },
];

export interface BackupAnswer {
  id: string;
  question: string;
  answeredBy: string;
  absentOwner: string;
  answeredAt: string;
  answerPreview: string;
}

export const backupAnswers: BackupAnswer[] = [
  {
    id: "bk1",
    question: "사내 주차 등록은 어디서 하나요?",
    answeredBy: "ga_backup",
    absentOwner: "ga_ops",
    answeredAt: "오늘 09:14",
    answerPreview:
      "지하 1층 안내데스크 또는 총무 포털 '주차등록' 메뉴에서 차량번호를 등록하면 당일 적용됩니다.",
  },
  {
    id: "bk2",
    question: "법인카드 분실 시 즉시 연락처는?",
    answeredBy: "fin_backup",
    absentOwner: "finance_ops",
    answeredAt: "어제 17:50",
    answerPreview:
      "카드사 분실신고센터에 먼저 정지 요청 후, finance_ops 핫라인(내선 2200)으로 보고합니다.",
  },
];

export interface ReevaluationCase {
  id: string;
  question: string;
  changedConcept: string;
  pastAnsweredAt: string;
  reason: string;
}

export const reevaluationCases: ReevaluationCase[] = [
  {
    id: "re1",
    question: "원격 접속 VPN 클라이언트는 어떤 걸 쓰나요?",
    changedConcept: "OKF-IT-0091 원격 접속 정책",
    pastAnsweredAt: "2개월 전 답변",
    reason: "VPN 솔루션 교체로 기존 판례가 stale 상태",
  },
  {
    id: "re2",
    question: "경조사 화환은 회사에서 지원되나요?",
    changedConcept: "OKF-HR-0410 경조 지원 규정",
    pastAnsweredAt: "5개월 전 답변",
    reason: "지원 한도 개정으로 과거 답변 재검토 필요",
  },
];

/* --------------------------- (D) /console ------------------------------- */

export interface ConsoleMetric {
  id: string;
  label: string;
  value: string;
  unit: string;
  tone: StatusTone;
  hint: string;
}

export const consoleMetrics: ConsoleMetric[] = [
  {
    id: "m1",
    label: "분당 질문",
    value: "14",
    unit: "건/분",
    tone: "info",
    hint: "최근 5분 평균",
  },
  {
    id: "m2",
    label: "Routed",
    value: "82",
    unit: "%",
    tone: "success",
    hint: "담당 매칭 성공률",
  },
  {
    id: "m3",
    label: "Contested 대기",
    value: "3",
    unit: "건",
    tone: "warning",
    hint: "담당 지정 필요",
  },
  {
    id: "m4",
    label: "연결 워커",
    value: "7",
    unit: "명",
    tone: "neutral",
    hint: "현재 SSE 연결",
  },
];

export type FeedKind = "contested" | "answered" | "worker" | "escalation";

export interface FeedEvent {
  id: string;
  kind: FeedKind;
  time: string;
  text: string;
  tags: string[];
}

export const feedEvents: FeedEvent[] = [
  {
    id: "f1",
    kind: "contested",
    time: "14:32:08",
    text: "라우팅 Contested — 후보 2명(hr_ops · finance_ops)",
    tags: ["routing", "contested"],
  },
  {
    id: "f2",
    kind: "answered",
    time: "14:31:55",
    text: "답 전송 — cs_ops 승인된 답변 전달",
    tags: ["answer", "cs_ops"],
  },
  {
    id: "f3",
    kind: "worker",
    time: "14:31:20",
    text: "워커 연결 — contract_ops 세션 시작",
    tags: ["worker", "online"],
  },
  {
    id: "f4",
    kind: "escalation",
    time: "14:30:47",
    text: "라우팅 Unowned — \"점심 뭐 먹지\" → 루트 User escalation",
    tags: ["routing", "unowned", "escalation"],
  },
  {
    id: "f5",
    kind: "answered",
    time: "14:29:33",
    text: "답 전송 — it_ops 승인된 답변 전달",
    tags: ["answer", "it_ops"],
  },
];

export interface PendingWorker {
  id: string;
  worker: string;
  domain: string;
  admissionToken: string;
  requestedAt: string;
}

export const pendingWorkers: PendingWorker[] = [
  {
    id: "w1",
    worker: "finance_ops",
    domain: "재무",
    admissionToken: "adm_7f3a…c901",
    requestedAt: "2분 전 요청",
  },
];

export interface AuditEntry {
  id: string;
  time: string;
  actor: string;
  action: string;
}

export const auditEntries: AuditEntry[] = [
  {
    id: "a1",
    time: "14:28",
    actor: "cs_ops",
    action: "답변 승인 → 사용자 전송 (OKF-CS-0233)",
  },
  {
    id: "a2",
    time: "14:21",
    actor: "operator",
    action: "Contested 케이스 hr_ops로 담당 지정",
  },
  {
    id: "a3",
    time: "14:09",
    actor: "system",
    action: "OKF-IT-0091 변경 감지 → 재평가 큐 2건 등록",
  },
];
