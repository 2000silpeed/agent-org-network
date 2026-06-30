import { PageHeader } from "@/components/app-shell/page-header";
import { InboxTabs } from "@/components/inbox/inbox-tabs";

export default function InboxPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Inbox"
        persona="Owner"
        title="처리함"
        description="담당이 갈리는 다툼, 부재 중 백업 답변, 지식 변경으로 stale된 과거 답변을 1인칭으로 처리합니다."
      />
      <InboxTabs />
    </div>
  );
}
