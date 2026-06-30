import { PageHeader } from "@/components/app-shell/page-header";
import { ConsoleView } from "@/components/console/console-view";
import { LoginGate } from "@/components/session/login-gate";

export default function ConsolePage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Console"
        persona="운영자"
        title="운영 콘솔"
        description="라우팅·답변·조직 이벤트를 감사 로그로 관찰하고, 매니저 에스컬레이션 큐와 조직 맵을 한 화면에서 확인합니다."
      />
      <LoginGate surface="운영 콘솔" requiredRole="operator">
        <ConsoleView />
      </LoginGate>
    </div>
  );
}
