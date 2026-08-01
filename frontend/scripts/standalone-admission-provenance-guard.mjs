/**
 * Next 14 adds x-forwarded-* with ??= after the Node request has entered the
 * server.  At an application route that makes a caller supplied value
 * indistinguishable from that generated value.  This entrypoint guard is the
 * trust boundary: inspect raw headers before Next, erase every caller supplied
 * forwarding value, and leave an internal classification for the BFF.
 *
 * The marker is not an authority claim.  It is overwritten for every request
 * before any Next listener runs, and it is never forwarded to Central.
 */
import { Server } from "node:http";
import { randomBytes } from "node:crypto";

const PROVENANCE_HEADER = "x-aon-admission-proxy-provenance";
const PROOF_HEADER = "aon-standalone-provenance-proof";
const PROOF_SYMBOL = Symbol.for("agent-org-network.standalone-provenance-proof");
const CLEAN = "next-standalone-clean";
const CLAIMED = "next-standalone-caller-claimed";
const FORWARDED_HEADER = "forwarded";

export function installStandaloneAdmissionProvenanceGuard() {
  const marker = Symbol.for("agent-org-network.standalone-admission-provenance-guard");
  if (globalThis[marker]) return;
  globalThis[PROOF_SYMBOL] = randomBytes(32).toString("base64url");
  globalThis[marker] = true;
  const emit = Server.prototype.emit;
  Server.prototype.emit = function guardedEmit(event, request, ...rest) {
    if (event === "request" && isIncomingRequest(request)) classifyRawForwarding(request);
    return emit.call(this, event, request, ...rest);
  };
}

function isIncomingRequest(value) {
  return value !== null && typeof value === "object" && Array.isArray(value.rawHeaders)
    && value.headers !== null && typeof value.headers === "object";
}

function classifyRawForwarding(request) {
  let callerClaimed = false;
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    const name = String(request.rawHeaders[index]).toLowerCase();
    if (
      name === FORWARDED_HEADER
      || name === PROVENANCE_HEADER
      || name === PROOF_HEADER
      || name.startsWith("x-forwarded-")
    ) {
      callerClaimed = true;
    }
  }
  // Header names on Node's IncomingMessage are lower-case.  Delete rather
  // than reinterpret; Next will synthesize its own transport facts later.
  for (const name of Object.keys(request.headers)) {
    if (
      name === FORWARDED_HEADER
      || name === PROVENANCE_HEADER
      || name === PROOF_HEADER
      || name.startsWith("x-forwarded-")
    ) {
      delete request.headers[name];
    }
  }
  request.headers[PROVENANCE_HEADER] = callerClaimed ? CLAIMED : CLEAN;
  request.headers[PROOF_HEADER] = globalThis[PROOF_SYMBOL];
}
