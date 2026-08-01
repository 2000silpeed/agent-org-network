"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Tag } from "@/components/ui/tag";
import { cn } from "@/lib/utils";
import {
  InboxClientError, concurConflict, disposeApproval, disposeBackup, disposeReevaluation,
  getApprovalDetail, getBackupDetail, getConflictDetail, getReevaluationDetail,
  listApprovals, listBackupReviews, listConflicts, listReevaluations, reassignApproval,
  type ApprovalDetail, type ApprovalSummary, type BackupDetail, type BackupSummary,
  type ConflictDetail, type ConflictSummary, type ReevaluationDetail, type ReevaluationSummary,
} from "@/lib/inbox-api";

type TabId = "conflicts" | "backup" | "reevaluations" | "approvals";
type SessionState = "loading" | "authenticated" | "anonymous" | "unavailable";
type Lists = { conflicts: ConflictSummary[]; backup: BackupSummary[]; reevaluations: ReevaluationSummary[]; approvals: ApprovalSummary[] };
type Detail = ConflictDetail | BackupDetail | ReevaluationDetail | ApprovalDetail;
const EMPTY: Lists = { conflicts: [], backup: [], reevaluations: [], approvals: [] };
const TABS: { id: TabId; label: string }[] = [{ id: "conflicts", label: "다툼" }, { id: "backup", label: "백업 검토" }, { id: "reevaluations", label: "재평가" }, { id: "approvals", label: "Approval" }];

function authenticated(value: unknown): boolean { if (typeof value !== "object" || value === null || Array.isArray(value)) return false; const raw = value as Record<string, unknown>; return Object.keys(raw).length === 4 && raw.authenticated === true && typeof raw.registry_user_ref === "string" && raw.registry_user_ref.length > 0 && typeof raw.expires_at === "string" && Array.isArray(raw.actions) && raw.actions.length === 1 && raw.actions[0] === "session.read"; }
function key(): string { return `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(2)).join("")}`; }
function idOf(detail: Detail): string { if ("case_id" in detail) return detail.case_id; if ("review_id" in detail) return detail.review_id; if ("reevaluation_id" in detail) return detail.reevaluation_id; return detail.approval_item_id; }

export function CentralInbox(): JSX.Element {
  const [session, setSession] = useState<SessionState>("loading");
  const [active, setActive] = useState<TabId>("conflicts");
  const [lists, setLists] = useState<Lists>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const loadEpoch = useRef(0);
  const detailEpoch = useRef(0);
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const detailHeading = useRef<HTMLHeadingElement | null>(null);

  const stopSession = useCallback(() => { setSession("anonymous"); setLists(EMPTY); setDetail(null); listController.current?.abort(); detailController.current?.abort(); }, []);

  const refresh = useCallback(async () => {
    const epoch = ++loadEpoch.current;
    listController.current?.abort();
    const controller = new AbortController(); listController.current = controller;
    setLoading(true); setError("");
    try {
      const [conflicts, backup, reevaluations, approvals] = await Promise.all([listConflicts(controller.signal), listBackupReviews(controller.signal), listReevaluations(controller.signal), listApprovals(controller.signal)]);
      if (epoch !== loadEpoch.current || controller.signal.aborted) return;
      setLists({ conflicts, backup, reevaluations, approvals });
    } catch (reason) {
      if (controller.signal.aborted || epoch !== loadEpoch.current) return;
      if (reason instanceof InboxClientError && reason.status === 401) { stopSession(); return; }
      setError(reason instanceof InboxClientError ? reason.message : "처리함을 불러오지 못했습니다.");
      setLists(EMPTY);
    } finally { if (epoch === loadEpoch.current && !controller.signal.aborted) setLoading(false); }
  }, [stopSession]);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store", signal: controller.signal }).then(async (response) => {
      if (response.status === 401) { setSession("anonymous"); return; }
      if (!response.ok || !authenticated(await response.json())) { setSession("unavailable"); return; }
      setSession("authenticated");
    }).catch(() => { if (!controller.signal.aborted) setSession("unavailable"); });
    return () => { controller.abort(); loadEpoch.current += 1; detailEpoch.current += 1; listController.current?.abort(); detailController.current?.abort(); };
  }, []);
  useEffect(() => { if (session === "authenticated") void refresh(); }, [session, refresh]);
  useEffect(() => { setDetail(null); detailEpoch.current += 1; detailController.current?.abort(); setError(""); setNotice(""); }, [active]);
  useEffect(() => { if (detail) detailHeading.current?.focus(); }, [detail]);

  async function openDetail(id: string, preserve = false): Promise<void> {
    const epoch = ++detailEpoch.current; detailController.current?.abort();
    const controller = new AbortController(); detailController.current = controller;
    if (!preserve) { setDetail(null); setDetailLoading(true); }
    setError("");
    try {
      const loaded = active === "conflicts" ? await getConflictDetail(id, controller.signal) : active === "backup" ? await getBackupDetail(id, controller.signal) : active === "reevaluations" ? await getReevaluationDetail(id, controller.signal) : await getApprovalDetail(id, controller.signal);
      if (epoch !== detailEpoch.current || controller.signal.aborted) return;
      setDetail(loaded);
    } catch (reason) {
      if (controller.signal.aborted || epoch !== detailEpoch.current) return;
      if (reason instanceof InboxClientError && reason.status === 401) { stopSession(); return; }
      setError(reason instanceof InboxClientError ? reason.message : "상세를 불러오지 못했습니다.");
      if (reason instanceof InboxClientError && reason.status === 404) setDetail(null);
      if (reason instanceof InboxClientError && (reason.status === 404 || reason.reload)) void refresh();
    } finally { if (!preserve && epoch === detailEpoch.current && !controller.signal.aborted) setDetailLoading(false); }
  }

  async function reloadSelected(): Promise<void> {
    if (!detail) { await refresh(); return; }
    await Promise.all([refresh(), openDetail(idOf(detail), true)]);
  }

  function keyboard(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    const keyName = event.key;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(keyName)) return;
    event.preventDefault();
    const next = keyName === "Home" ? 0 : keyName === "End" ? TABS.length - 1 : (index + (keyName === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length;
    setActive(TABS[next].id);
    document.getElementById(`inbox-tab-${TABS[next].id}`)?.focus();
  }

  if (session === "loading") return <Gate message="Central Session을 확인하고 있습니다." loading />;
  if (session === "anonymous") return <Gate message="처리함을 보려면 조직 SSO 로그인이 필요합니다." login />;
  if (session === "unavailable") return <Gate message="Central Session을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요." />;
  const items = lists[active];

  return <div className="px-ds-16 py-ds-16 md:px-ds-24">
    <div aria-live="polite" aria-atomic="true" className="sr-only">{error || notice || (loading ? "처리함을 불러오는 중" : "")}</div>
    <div className="flex items-end gap-ds-4 border-b border-[var(--ds-color-border)]">
      <div role="tablist" aria-label="처리함 종류" className="ds-scrollbar-thin flex min-w-0 flex-1 gap-ds-4 overflow-x-auto">
        {TABS.map((tab, index) => <button key={tab.id} id={`inbox-tab-${tab.id}`} role="tab" aria-selected={active === tab.id} aria-controls={`inbox-panel-${tab.id}`} tabIndex={active === tab.id ? 0 : -1} onClick={() => setActive(tab.id)} onKeyDown={(event) => keyboard(event, index)} className={cn("shrink-0 border-b-2 px-ds-12 py-ds-8 text-sm", active === tab.id ? "border-[var(--ds-color-primary)] text-[var(--ds-color-ink)]" : "border-transparent text-[var(--ds-color-ink-subtle)]")}>{tab.label} <span className="ml-ds-4 text-xs">{lists[tab.id].length}</span></button>)}
      </div>
      <Button type="button" variant="ghost" size="sm" className="ml-auto" onClick={() => void refresh()} disabled={loading}><RefreshCw aria-hidden className={cn("h-4 w-4", loading && "animate-ds-spin")} />새로고침</Button>
    </div>
    {error && <Alert message={error} />}{notice && <p role="status" className="my-ds-12 text-sm text-[var(--ds-color-success)]">{notice}</p>}
    <div id={`inbox-panel-${active}`} role="tabpanel" aria-labelledby={`inbox-tab-${active}`} tabIndex={0} className="mt-ds-16 grid gap-ds-16 lg:grid-cols-[minmax(260px,0.8fr)_minmax(0,1.4fr)]">
      <section aria-label="항목 목록"><h2 className="mb-ds-8 text-sm font-semibold text-[var(--ds-color-ink)]">목록</h2>{loading ? <Loading /> : items.length === 0 ? <Empty /> : <div className="flex flex-col gap-ds-8">{items.map((item) => { const itemId = "case_id" in item ? item.case_id : "review_id" in item ? item.review_id : "reevaluation_id" in item ? item.reevaluation_id : item.approval_item_id; return <button type="button" key={itemId} onClick={() => void openDetail(itemId)} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-12 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ds-color-primary)]"><span className="block text-sm font-medium text-[var(--ds-color-ink)]">{itemId}</span><span className="mt-ds-4 block text-xs text-[var(--ds-color-ink-subtle)]">요청 {item.request_id} · revision {item.revision}</span></button>; })}</div>}</section>
      <section aria-label="선택한 항목 상세">{detailLoading ? <Loading /> : detail ? <DetailView detail={detail} headingRef={detailHeading} busyNotice={setNotice} reportError={(message) => setError(message)} onSessionStop={stopSession} reload={refresh} reloadSelected={reloadSelected} onDone={() => setDetail(null)} /> : <p className="rounded-md border border-dashed border-[var(--ds-color-border)] p-ds-24 text-sm text-[var(--ds-color-ink-subtle)]">목록에서 항목을 선택하면 상세를 불러옵니다.</p>}</section>
    </div>
  </div>;
}

function DetailView({ detail, headingRef, busyNotice, reportError, onSessionStop, reload, reloadSelected, onDone }: { detail: Detail; headingRef: React.RefObject<HTMLHeadingElement>; busyNotice: (message: string) => void; reportError: (message: string) => void; onSessionStop: () => void; reload: () => Promise<void>; reloadSelected: () => Promise<void>; onDone: () => void }): JSX.Element {
  const [kind, setKind] = useState(""); const [rationale, setRationale] = useState(""); const [text, setText] = useState(""); const [targetUser, setTargetUser] = useState(""); const [targetCard, setTargetCard] = useState(""); const [candidate, setCandidate] = useState(""); const [stance, setStance] = useState<"keep_as_complement" | "withdraw">("withdraw"); const [busyAction, setBusyAction] = useState(false);
  const replay = useRef<{ payload: string; key: string } | null>(null);
  const actionController = useRef<AbortController | null>(null);
  useEffect(() => () => actionController.current?.abort(), []);
  const actionKey = (payload: object): string => { const serialized = JSON.stringify(payload); if (replay.current?.payload !== serialized) replay.current = { payload: serialized, key: key() }; return replay.current.key; };
  async function act(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault(); if (busyAction) return; setBusyAction(true); reportError(""); busyNotice(""); const controller = new AbortController(); actionController.current = controller;
    try {
      if ("case_id" in detail) { const body = { on_candidate_card_id: candidate, stance, rationale, expected_case_revision: detail.expected_case_revision, expected_request_revision: detail.expected_request_revision, expected_round: detail.expected_round }; const result = await concurConflict(detail.case_id, body, actionKey(body), controller.signal); busyNotice(`합의 결과: ${result.outcome}`); }
      else if ("review_id" in detail) { const body = kind === "correct" ? { kind: "correct" as const, corrected_text: text, rationale, expected_revision: detail.revision } : { kind: kind as "approve" | "dismiss", rationale, expected_revision: detail.revision }; await disposeBackup(detail.review_id, body, actionKey(body), controller.signal); busyNotice("백업 검토 처분을 기록했습니다."); }
      else if ("reevaluation_id" in detail) { const body = { kind: kind as "acknowledge" | "request_reanswer", rationale, expected_revision: detail.revision }; await disposeReevaluation(detail.reevaluation_id, body, actionKey(body), controller.signal); busyNotice(kind === "request_reanswer" ? "재답변 요청을 기록했습니다. 재답변 실행 완료를 뜻하지 않습니다." : "재평가 확인을 기록했습니다."); }
      else if (kind === "reassign") { const body = { target_approver_user_id: targetUser, target_approval_card_id: targetCard, expected_approval_item_revision: detail.revision, expected_request_revision: detail.request_revision }; await reassignApproval(detail.approval_item_id, body, actionKey(body), controller.signal); busyNotice("새 Approval 항목으로 재지정했습니다. Approval은 계속 열려 있습니다."); }
      else { const common = { expected_approval_item_revision: detail.revision, expected_request_revision: detail.request_revision }; const body = kind === "approve" ? { kind: "approve" as const, ...common } : kind === "approve_with_edit" ? { kind: "approve_with_edit" as const, edited_text: text, ...common } : { kind: "reject" as const, reason_code: rationale, ...common }; await disposeApproval(detail.approval_item_id, body, actionKey(body), controller.signal); busyNotice(kind === "reject" ? "Approval을 거절했습니다." : "Approval을 승인했습니다."); }
      replay.current = null; onDone(); await reload();
    } catch (reason) { if (controller.signal.aborted) return; if (reason instanceof InboxClientError && reason.status === 401) onSessionStop(); else { reportError(reason instanceof InboxClientError ? reason.message : "처분을 기록하지 못했습니다."); if (reason instanceof InboxClientError && reason.status === 404) { onDone(); await reload(); } else if (reason instanceof InboxClientError && reason.reload) await reloadSelected(); } } finally { if (!controller.signal.aborted) setBusyAction(false); }
  }
  return <Card><CardHeader><h2 ref={headingRef} tabIndex={-1} className="text-md font-semibold text-[var(--ds-color-ink)]">상세 · {idOf(detail)}</h2><p className="text-xs text-[var(--ds-color-ink-subtle)]">요청 {detail.request_id} · revision {detail.revision}</p></CardHeader><CardBody className="flex flex-col gap-ds-12"><DetailContent detail={detail} /><form onSubmit={(event) => void act(event)} className="flex flex-col gap-ds-8"><label className="text-sm font-medium" htmlFor="inbox-action">처분</label><select id="inbox-action" required value={kind} onChange={(event) => setKind(event.target.value)} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm"><option value="">선택</option>{"case_id" in detail ? <option value="concur">합의 제출</option> : "review_id" in detail ? <><option value="approve">승인</option><option value="correct">수정</option><option value="dismiss">기각</option></> : "reevaluation_id" in detail ? <><option value="acknowledge">확인</option><option value="request_reanswer">재답변 요청</option></> : <><option value="approve">승인</option><option value="approve_with_edit">수정 승인</option><option value="reject">거절</option><option value="reassign">재지정</option></>}</select>
  {"case_id" in detail && <><label htmlFor="candidate" className="text-sm">담당 Agent Card</label><select id="candidate" required value={candidate} onChange={(event) => setCandidate(event.target.value)} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm"><option value="">선택</option>{detail.candidates.map((item) => <option key={item.card_id} value={item.card_id}>{item.card_id}</option>)}</select><fieldset><legend className="text-sm">내 후보 처리</legend><label className="mr-ds-12 text-sm"><input type="radio" name="stance" checked={stance === "withdraw"} onChange={() => setStance("withdraw")} /> 제외</label><label className="text-sm"><input type="radio" name="stance" checked={stance === "keep_as_complement"} onChange={() => setStance("keep_as_complement")} /> 보조 근거 유지</label></fieldset></>}
  {(kind === "correct" || kind === "approve_with_edit") && <><label htmlFor="action-text" className="text-sm">수정 본문</label><textarea id="action-text" required value={text} onChange={(event) => setText(event.target.value)} rows={5} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm" /></>}
  {(kind === "reassign") && <><label htmlFor="target-user" className="text-sm">대상 Registry User ID</label><input id="target-user" required value={targetUser} onChange={(event) => setTargetUser(event.target.value)} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm" /><label htmlFor="target-card" className="text-sm">대상 Approval Agent Card ID</label><input id="target-card" required value={targetCard} onChange={(event) => setTargetCard(event.target.value)} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm" /></>}
  {("case_id" in detail || "review_id" in detail || "reevaluation_id" in detail || kind === "reject") && <><label htmlFor="rationale" className="text-sm">{kind === "reject" ? "거절 사유 코드" : "근거"}</label><textarea id="rationale" required value={rationale} onChange={(event) => setRationale(event.target.value)} rows={3} className="rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-8 text-sm" /></>}
  <div className="flex justify-end"><Button type="submit" disabled={!kind || busyAction} loading={busyAction}>처분 기록</Button></div></form></CardBody></Card>;
}

function DetailContent({ detail }: { detail: Detail }): JSX.Element { if ("case_id" in detail) return <><p className="whitespace-pre-wrap text-sm">{detail.question}</p><div className="grid gap-ds-8 sm:grid-cols-2">{detail.candidates.map((item) => <div key={item.card_id} className="rounded-md border border-[var(--ds-color-border)] p-ds-8 text-xs"><strong>{item.card_id}</strong><p>Card revision {item.card_revision}</p><p>Card Owner {item.owner_user_id}</p><p>개념 {item.concept_ref}</p></div>)}</div>{detail.evidence_grants.length > 0 && <div><h3 className="text-sm font-medium">Conflict Evidence Grant metadata</h3>{detail.evidence_grants.map((grant) => <p key={grant.grant_id} className="text-xs text-[var(--ds-color-ink-subtle)]">{grant.grant_id} · {grant.candidate_card_id} · {grant.status} · {grant.expires_at}</p>)}</div>}<Tag tone="neutral">case {detail.expected_case_revision} · request {detail.expected_request_revision} · round {detail.expected_round}</Tag></>; if ("review_id" in detail) return <><p className="text-sm">{detail.question}</p><blockquote className="whitespace-pre-wrap border-l-2 border-[var(--ds-color-primary)] pl-ds-12 text-sm">{detail.backup_answer_text}</blockquote><Tag tone="info">{detail.answering_card_id} · Card Owner {detail.owner_user_id}</Tag></>; if ("reevaluation_id" in detail) return <><p className="text-sm">{detail.question}</p><blockquote className="whitespace-pre-wrap border-l-2 border-[var(--ds-color-primary)] pl-ds-12 text-sm">{detail.answer_text}</blockquote><p className="text-sm text-[var(--ds-color-danger)]">나쁜 평가: {detail.feedback_comment || "의견 없음"}</p></>; return <><p className="text-sm">{detail.question}</p><blockquote className="whitespace-pre-wrap border-l-2 border-[var(--ds-color-primary)] pl-ds-12 text-sm">{detail.candidate_text}</blockquote><Tag tone="info">지정 {detail.assigned_approver_user_id} · {detail.assigned_approval_card_id}</Tag><Tag tone="neutral">item {detail.revision} · request {detail.request_revision}</Tag></>; }
function Gate({ message, loading, login }: { message: string; loading?: boolean; login?: boolean }): JSX.Element { return <div className="m-ds-16 rounded-md border border-[var(--ds-color-border)] bg-[var(--ds-color-surface)] p-ds-24 text-sm text-[var(--ds-color-ink-muted)]">{loading && <Loader2 aria-hidden className="mr-ds-8 inline h-4 w-4 animate-ds-spin" />}{message}{login && <form method="post" action="/api/auth/login/start" className="mt-ds-12"><Button type="submit">조직 SSO 로그인</Button></form>}</div>; }
function Loading(): JSX.Element { return <p className="flex items-center gap-ds-8 p-ds-16 text-sm text-[var(--ds-color-ink-subtle)]"><Loader2 aria-hidden className="h-4 w-4 animate-ds-spin" />불러오는 중</p>; }
function Empty(): JSX.Element { return <p className="rounded-md border border-dashed border-[var(--ds-color-border)] p-ds-16 text-sm text-[var(--ds-color-ink-subtle)]">처리할 항목이 없습니다.</p>; }
function Alert({ message }: { message: string }): JSX.Element { return <div role="alert" className="my-ds-12 flex gap-ds-8 rounded-md border border-[var(--ds-color-danger)] p-ds-12 text-sm"><AlertTriangle aria-hidden className="h-4 w-4 text-[var(--ds-color-danger)]" />{message}</div>; }
