import { PageHeader } from "@/components/app-shell/page-header";
import { AdminControlPlanePanel } from "@/components/central-admin/admin-control-plane-panel";
import { AdmissionConsole } from "@/components/onboarding/admission-console";

export default function AdminPage() {
  return <div className="flex flex-col"><PageHeader surface="Admin" persona="운영자" title="Registry 관리" description="현재 Registry User와 Agent Card를 확인하고 추가 등록합니다." /><AdmissionConsole guided={false} /><AdminControlPlanePanel /></div>;
}
