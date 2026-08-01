import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, root), "utf8");

test("조직 콘솔은 dedicated graph BFF와 cookie credentials를 사용한다", async () => {
  const page = await read("app/console/org/page.tsx");
  const component = await read("components/central-admin/org-graph-console.tsx");
  assert.match(page, /OrgGraphConsole/);
  assert.match(component, /\/api\/console\/org/);
  assert.match(component, /credentials:\s*["']include["']/);
  for (const status of ["401", "403", "404", "503"]) assert.match(component, new RegExp(status));
  assert.doesNotMatch(component, /dangerouslySetInnerHTML|owner-api|legacy/i);
});

test("관리자 화면은 AdmissionConsole을 유지하고 read-only control-plane panel만 마운트한다", async () => {
  const page = await read("app/admin/page.tsx");
  const component = await read("components/central-admin/admin-control-plane-panel.tsx");
  assert.match(page, /AdmissionConsole/);
  assert.match(page, /AdminControlPlanePanel/);
  assert.match(component, /\/api\/admin\/policy/);
  assert.match(component, /\/api\/admin\/scorecard/);
  assert.match(component, /credentials:\s*["']include["']/);
  for (const status of ["401", "403", "404", "503"]) assert.match(component, new RegExp(status));
  assert.doesNotMatch(component, /owner-transfers|revocations|dangerouslySetInnerHTML|owner-api|legacy/i);
});
