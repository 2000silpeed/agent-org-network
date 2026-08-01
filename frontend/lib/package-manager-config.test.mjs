import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8")
);
const workspaceConfig = await readFile(
  new URL("../pnpm-workspace.yaml", import.meta.url),
  "utf8"
);

test("pnpm v11은 검증된 버전과 검토한 단일 dependency build만 허용한다", () => {
  assert.equal(packageJson.packageManager, "pnpm@11.18.0");
  assert.equal(packageJson.engines?.node, ">=24");
  assert.equal(packageJson.type, "module");
  assert.equal(workspaceConfig, "allowBuilds:\n  unrs-resolver: true\n");
  assert.doesNotMatch(workspaceConfig, /dangerouslyAllowAllBuilds/);
});
