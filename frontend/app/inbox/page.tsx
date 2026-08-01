import { PageHeader } from "@/components/app-shell/page-header";
import { CentralInbox } from "@/components/inbox/inbox-tabs";

export default function InboxPage(): JSX.Element {
  return <div className="flex min-h-full flex-col"><PageHeader surface="Inbox" persona="Card Owner · Approver" title="처리함" description="Central Session 권한으로 다툼, 백업 검토, 재평가, Approval을 안전하게 조회하고 처분합니다." /><CentralInbox /></div>;
}
