import { PageHeader } from "@/components/app-shell/page-header";
import { OrgGraphConsole } from "@/components/central-admin/org-graph-console";

export default function OrganizationGraphPage() {
  return <div className="flex flex-col"><PageHeader surface="Console" persona="운영자" title="조직 그래프" description="현재 조직의 User·Agent Card 관계를 안전한 graph projection으로 확인합니다." /><OrgGraphConsole /></div>;
}
