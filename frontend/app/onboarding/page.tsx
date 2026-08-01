import { PageHeader } from "@/components/app-shell/page-header";
import { AdmissionConsole } from "@/components/onboarding/admission-console";

export default function OnboardingPage() {
  return (
    <div className="flex flex-col">
      <PageHeader
        surface="Onboarding"
        persona="운영자"
        title="조직 온보딩"
        description="Central SSO 세션에서 Registry User, Agent Card, Card Owner Installation handoff를 순서대로 진행합니다."
      />
      <AdmissionConsole guided />
    </div>
  );
}
