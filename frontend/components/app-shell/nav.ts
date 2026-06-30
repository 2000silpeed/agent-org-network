import {
  MessagesSquare,
  PenLine,
  Inbox,
  MonitorDot,
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

// The four product surfaces of the question-routing org.
export const NAV_ITEMS: NavItem[] = [
  {
    href: "/ask",
    label: "질문하기",
    surface: "Ask",
    description: "사용자 채팅 · 담당 라우팅",
    icon: MessagesSquare,
    persona: "사용자",
  },
  {
    href: "/author",
    label: "지식 저작",
    surface: "Author",
    description: "Owner OKF 저작면",
    icon: PenLine,
    persona: "Owner",
  },
  {
    href: "/inbox",
    label: "처리함",
    surface: "Inbox",
    description: "다툼 · 백업 · 재평가",
    icon: Inbox,
    persona: "Owner",
  },
  {
    href: "/console",
    label: "운영 콘솔",
    surface: "Console",
    description: "라이브 피드 · 워커 승인",
    icon: MonitorDot,
    persona: "운영자",
  },
];
