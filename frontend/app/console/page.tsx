import { PageHeader } from "@/components/app-shell/page-header";
import { SsoUnavailable } from "@/components/session/sso-unavailable";

export default function ConsolePage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Console"
        persona="운영자"
        title="운영 콘솔"
        description="Central OIDC 세션과 현재 Authority 검증이 준비된 뒤에 제공합니다."
      />
      <SsoUnavailable surface="운영 콘솔" />
    </div>
  );
}
