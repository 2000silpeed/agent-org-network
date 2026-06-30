import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/app-shell/sidebar";
import { Topbar } from "@/components/app-shell/topbar";
import { SessionProvider } from "@/components/session/session-context";
import { ThemeProvider } from "@/components/app-shell/theme";

// Apply the saved theme before paint (no flash). dark is the default.
const THEME_INIT = `(function(){try{var t=localStorage.getItem('aon.theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();`;

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
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="min-h-screen bg-[var(--ds-color-canvas)] font-sans text-[var(--ds-color-ink)] antialiased">
        <ThemeProvider>
          <SessionProvider>
            {/* Desktop (lg+): app shell is viewport-fixed — sidebar/topbar stay
                put, only <main> scrolls. Mobile (<lg): document scrolls as before
                (sidebar is hidden, the Topbar is sticky), so no regression. */}
            <div className="flex min-h-screen lg:h-screen lg:overflow-hidden">
              <Sidebar />
              <div className="flex min-w-0 flex-1 flex-col lg:overflow-hidden">
                <Topbar />
                <main className="min-w-0 flex-1 lg:overflow-y-auto">{children}</main>
              </div>
            </div>
          </SessionProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
