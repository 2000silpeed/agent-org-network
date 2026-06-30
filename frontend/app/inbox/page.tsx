import { PageHeader } from "@/components/app-shell/page-header";
import { InboxTabs } from "@/components/inbox/inbox-tabs";
import { LoginGate } from "@/components/session/login-gate";

export default function InboxPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Inbox"
        persona="Owner"
        title="처리함"
        description="담당이 갈리는 다툼, 부재 중 백업 답변, 지식 변경으로 stale된 과거 답변을 1인칭으로 처리합니다."
      />
      <LoginGate surface="처리함" requiredRole="owner">
        <InboxTabs />
      </LoginGate>
    </div>
  );
}
