// Shared UI types still imported by live components. All four product surfaces
// (/ask, /inbox, /console, /author) are wired to the real backend now — their
// mock data was removed. Domain language follows CONTEXT.md.

import type { StatusTone } from "@/components/ui/status-badge";

/* ---------------------------- /ask — shared types ----------------------------- */

export interface SourceCard {
  id: string;
  conceptId: string;
  title: string;
  owner: string;
  domain: string;
  updatedAt: string;
}

export interface RoutingStep {
  id: string;
  label: string;
  state: "done" | "active" | "pending";
}

/* --------------------------- /console — shared type ------------------------------- */

export interface ConsoleMetric {
  id: string;
  label: string;
  value: string;
  unit: string;
  tone: StatusTone;
  hint: string;
}
