import {
  cp,
  lstat,
  mkdir,
  readdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, relative, resolve, sep } from "node:path";

const root = resolve(import.meta.dirname, "..");
const standalone = resolve(root, ".next/standalone");
const staging = resolve(root, ".next/standalone.dereferenced");

// Next's standalone output is deliberately a traced, pnpm-linked subset.  A
// wheel cannot preserve those links, so copy it once with links dereferenced.
// Do not substitute the application's whole production dependency graph here:
// that both defeats output tracing and turns a small release artifact into a
// several-hundred-megabyte one.
await rm(staging, { recursive: true, force: true });
await cp(standalone, staging, { recursive: true, dereference: true, force: true });

const nodeModules = resolve(staging, "node_modules");

function isContained(path) {
  return path === staging || path.startsWith(`${staging}${sep}`);
}

async function exists(path) {
  try {
    await lstat(path);
    return true;
  } catch {
    return false;
  }
}

async function nftFiles(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) found.push(...await nftFiles(path));
    else if (entry.isFile() && entry.name.endsWith(".nft.json")) found.push(path);
  }
  return found;
}

function packageNameForMissingPath(path) {
  const relativePath = relative(nodeModules, path).split(sep);
  if (relativePath.length === 0 || relativePath[0] === "" || relativePath[0] === "..") return null;
  if (relativePath[0] === ".pnpm") return null;
  if (relativePath[0].startsWith("@")) {
    if (relativePath.length < 2 || !relativePath[1]) throw new Error(`invalid scoped package trace: ${relative(staging, path)}`);
    return `${relativePath[0]}/${relativePath[1]}`;
  }
  return relativePath[0];
}

async function tracedPackageSources(name) {
  const pnpm = resolve(nodeModules, ".pnpm");
  const sources = [];
  if (!await exists(pnpm)) return sources;
  const storePrefix = `${name.replace("/", "+")}@`;
  for (const storeEntry of await readdir(pnpm, { withFileTypes: true })) {
    // The pnpm store also contains a package's nested dependencies.  Only the
    // top-level store directory whose name represents this package is the
    // canonical traced source; accepting nested lookalikes would make a
    // version choice accidental.
    if (!storeEntry.isDirectory() || !storeEntry.name.startsWith(storePrefix)) continue;
    const candidate = resolve(pnpm, storeEntry.name, "node_modules", name);
    if (!await exists(candidate)) continue;
    const info = await lstat(candidate);
    if (!info.isDirectory() || info.isSymbolicLink()) {
      throw new Error(`unusable traced package: ${name}`);
    }
    const packageJson = resolve(candidate, "package.json");
    if (!await exists(packageJson)) throw new Error(`traced package metadata missing: ${name}`);
    const metadata = JSON.parse(await readFile(packageJson, "utf8"));
    if (metadata?.name !== name) throw new Error(`traced package identity mismatch: ${name}`);
    sources.push(candidate);
  }
  return sources;
}

async function materializeMissingPackage(name) {
  const destination = resolve(nodeModules, name);
  if (!isContained(destination)) throw new Error(`unsafe package destination: ${name}`);
  const sources = await tracedPackageSources(name);
  if (sources.length !== 1) {
    throw new Error(`ambiguous or absent traced package: ${name}`);
  }
  await mkdir(dirname(destination), { recursive: true });
  await cp(sources[0], destination, { recursive: true, dereference: true, force: true });
}

async function canonicalTracedPackageNames() {
  const pnpm = resolve(nodeModules, ".pnpm");
  const names = new Set();
  if (!await exists(pnpm)) return names;
  for (const storeEntry of await readdir(pnpm, { withFileTypes: true })) {
    if (!storeEntry.isDirectory()) continue;
    const packageRoot = resolve(pnpm, storeEntry.name, "node_modules");
    if (!await exists(packageRoot)) continue;
    for (const candidate of await readdir(packageRoot, { withFileTypes: true })) {
      const candidates = candidate.name.startsWith("@")
        ? (candidate.isDirectory() ? await readdir(resolve(packageRoot, candidate.name), { withFileTypes: true }) : [])
          .filter((entry) => entry.isDirectory())
          .map((entry) => resolve(packageRoot, candidate.name, entry.name))
        : (candidate.isDirectory() ? [resolve(packageRoot, candidate.name)] : []);
      for (const source of candidates) {
        const packageJson = resolve(source, "package.json");
        if (!await exists(packageJson)) continue;
        const metadata = JSON.parse(await readFile(packageJson, "utf8"));
        if (typeof metadata?.name !== "string") throw new Error(`invalid traced package metadata: ${relative(staging, source)}`);
        if (storeEntry.name.startsWith(`${metadata.name.replace("/", "+")}@`)) names.add(metadata.name);
      }
    }
  }
  return names;
}

async function repairTracedRootPackages() {
  const traces = await nftFiles(resolve(staging, ".next", "server"));
  if (traces.length === 0) throw new Error("Next trace metadata missing");
  // A repair can satisfy a later trace, so iterate to a stable state.  Only
  // direct root package aliases are repaired from the already traced pnpm
  // subset; a trace escaping the standalone capability is never accepted.
  for (let pass = 0; pass <= 32; pass += 1) {
    const missingPackages = new Set();
    const missingPaths = [];
    for (const trace of traces) {
      const parsed = JSON.parse(await readFile(trace, "utf8"));
      if (!parsed || !Array.isArray(parsed.files) || !parsed.files.every((file) => typeof file === "string")) {
        throw new Error(`invalid Next trace metadata: ${relative(staging, trace)}`);
      }
      for (const file of parsed.files) {
        const target = resolve(dirname(trace), file);
        if (!isContained(target)) throw new Error(`Next trace escapes standalone: ${file}`);
        if (await exists(target)) continue;
        const packageName = packageNameForMissingPath(target);
        if (packageName === null) {
          missingPaths.push(relative(staging, target));
        } else {
          missingPackages.add(packageName);
        }
      }
    }
    if (missingPackages.size === 0) {
      if (missingPaths.length > 0) throw new Error(`unresolved Next trace: ${missingPaths.join(", ")}`);
      return;
    }
    if (pass === 32) throw new Error("Next trace package repair did not converge");
    for (const name of [...missingPackages].sort()) await materializeMissingPackage(name);
  }
}

await repairTracedRootPackages();

// Some Next runtime `require`s are deliberately outside individual route NFT
// files.  Materialize aliases solely for packages that the standalone output
// already traced into its pnpm store.  It is a bounded projection of the trace
// (not an application dependency closure), and keeps Node's normal root
// module resolution valid after symlinks are forbidden by wheel packaging.
for (const name of [...await canonicalTracedPackageNames()].sort()) {
  if (!await exists(resolve(nodeModules, name))) await materializeMissingPackage(name);
}

for (const [from, to] of [["public", "public"], [".next/static", ".next/static"]]) {
  await rm(resolve(staging, to), { recursive: true, force: true });
  await mkdir(resolve(staging, to), { recursive: true });
  await cp(resolve(root, from), resolve(staging, to), { recursive: true, dereference: true, force: true });
}

// Keep raw request provenance outside Next's route abstraction.  The wrapper
// runs before Next adds x-forwarded-* with ??=, so a browser cannot smuggle a
// value that merely happens to equal a generated loopback/host value.
const nextServer = resolve(staging, "next-server.js");
await rename(resolve(staging, "server.js"), nextServer);
await cp(resolve(root, "scripts", "standalone-admission-provenance-guard.mjs"), resolve(staging, "aon-admission-provenance-guard.mjs"));
await writeFile(resolve(staging, "server.js"), [
  'import { installStandaloneAdmissionProvenanceGuard } from "./aon-admission-provenance-guard.mjs";',
  "installStandaloneAdmissionProvenanceGuard();",
  'await import("./next-server.js");',
  "",
].join("\n"));

async function files(directory) {
  const found = [];
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const full = resolve(directory, item.name);
    const info = await lstat(full);
    const artifactRelative = relative(staging, full).split(sep).join("/");
    // Next's runtime image/response cache is intentionally mutable.  It is
    // outside the release integrity manifest and must never prevent restart.
    if (artifactRelative === ".next/cache" || artifactRelative.startsWith(".next/cache/")) continue;
    if (info.isSymbolicLink()) throw new Error(`symlink forbidden in standalone: ${relative(staging, full)}`);
    if (info.isDirectory()) found.push(...await files(full));
    else if (info.isFile()) found.push(full);
    else throw new Error(`non-regular standalone entry: ${relative(staging, full)}`);
  }
  return found;
}

const manifest = resolve(staging, "aon-standalone-manifest.json");
if (await exists(manifest)) await rm(manifest, { force: true });
const entries = Object.fromEntries(await Promise.all((await files(staging)).map(async (file) => [
  relative(staging, file).split(sep).join("/"),
  createHash("sha256").update(await readFile(file)).digest("hex"),
])));
await writeFile(manifest, JSON.stringify({ version: 1, files: entries }, null, 2));

await rm(standalone, { recursive: true, force: true });
await rename(staging, standalone);
