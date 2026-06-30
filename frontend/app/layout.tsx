import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/app-shell/sidebar";
import { Topbar } from "@/components/app-shell/topbar";

export const metadata: Metadata = {
  title: "Agent Org Network — question-routing console",
  description:
    "Governed, observable, audit-friendly question-routing console for the Agent Org Network.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // dark is the default mode (data-theme="dark"); ko is the default locale.
  // Light mode is still supported via the :root token set per color-mode parity.
  return (
    <html lang="ko" data-theme="dark">
      <body className="min-h-screen bg-[var(--ds-color-canvas)] font-sans text-[var(--ds-color-ink)] antialiased">
        <div className="flex min-h-screen">
          <Sidebar />
          <div className="flex min-w-0 flex-1 flex-col">
            <Topbar />
            <main className="min-w-0 flex-1">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
