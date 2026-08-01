import {
  MessagesSquare,
  Inbox,
  MonitorDot,
  ListChecks,
  ShieldCheck,
  GitBranch,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  surface: string;
  description: string;
  icon: LucideIcon;
  persona: string;
}

// The product surfaces of the question-routing org.
export const NAV_ITEMS: NavItem[] = [
  {
    href: "/onboarding",
    label: "온보딩",
    surface: "Onboarding",
    description: "User · Card · Card Owner Installation",
    icon: ListChecks,
    persona: "운영자",
  },
  {
    href: "/admin",
    label: "Registry 관리",
    surface: "Admin",
    description: "User · Card register-only",
    icon: ShieldCheck,
    persona: "운영자",
  },
  {
    href: "/ask",
    label: "질문하기",
    surface: "Ask",
    description: "Question Request · 상태 확인",
    icon: MessagesSquare,
    persona: "사용자",
  },
  {
    href: "/inbox",
    label: "처리함",
    surface: "Inbox",
    description: "다툼 · 백업 · 재평가 · Approval",
    icon: Inbox,
    persona: "Owner",
  },
  {
    href: "/console",
    label: "운영 콘솔",
    surface: "Console",
    description: "라이브 모니터 · 감사 로그",
    icon: MonitorDot,
    persona: "운영자",
  },
  {
    href: "/console/org",
    label: "조직 그래프",
    surface: "Console",
    description: "User · Card · ownership graph",
    icon: GitBranch,
    persona: "운영자",
  },
];
