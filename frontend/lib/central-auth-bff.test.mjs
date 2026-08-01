import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadModule(name) {
  const source = await readFile(new URL(`./${name}.ts`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    fileName: `${name}.ts`,
    reportDiagnostics: true,
  });
  assert.deepEqual(compiled.diagnostics ?? [], []);
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
}

const auth = await loadModule("central-auth-bff");
const ORIGIN = "https://central.example.test";

function request(route, init = {}) {
  return new Request(`${ORIGIN}/api/auth/${route}`, init);
}

test("Central auth BFF는 정확한 네 endpoint와 fixed loopback upstream만 가진다", () => {
  assert.equal(auth.authUpstreamUrl("login-start", "").toString(), "http://127.0.0.1:8010/v1/browser-auth/login/start");
  assert.equal(auth.authUpstreamUrl("callback", "?code=ok&state=state").toString(), "http://127.0.0.1:8010/v1/browser-auth/callback?code=ok&state=state");
  assert.equal(auth.authUpstreamUrl("session", "").toString(), "http://127.0.0.1:8010/v1/browser-auth/session");
  assert.equal(auth.authUpstreamUrl("logout", "").toString(), "http://127.0.0.1:8010/v1/browser-auth/logout");
});

test("start/logout은 exact Origin·Fetch Metadata와 empty command만 받고 self-claim을 닫는다", async () => {
  const start = request("login/start", {
    method: "POST",
    headers: { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document" },
  });
  assert.equal(await auth.isValidCentralAuthRequest("login-start", start, ORIGIN), true);
  for (const headers of [
    { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty" },
    { Origin: "https://same-site.example.test", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document" },
    { Origin: ORIGIN, "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document" },
    { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", Authorization: "Bearer forged" },
    { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "X-User": "forged" },
  ]) {
    assert.equal(await auth.isValidCentralAuthRequest("login-start", request("login/start", { method: "POST", headers }), ORIGIN), false);
  }
  assert.equal(await auth.isValidCentralAuthRequest("login-start", request("login/start?return_to=/evil", {
    method: "POST", headers: { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document" },
  }), ORIGIN), false);
  assert.equal(await auth.isValidCentralAuthRequest("logout", request("logout", {
    method: "POST", body: "not-empty", headers: { Origin: ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty" },
  }), ORIGIN), false);
});

test("callback/session은 exact input shape만 Central API로 보낸다", async () => {
  assert.equal(await auth.isValidCentralAuthRequest("callback", request("callback?code=code&state=state"), ORIGIN), true);
  for (const suffix of [
    "?code=code&state=state&return_to=/ask", "?code=code&state=state&state=again",
    "?error=access_denied&state=state&code=code", "?error=access_denied", "?state=state",
  ]) assert.equal(await auth.isValidCentralAuthRequest("callback", request(`callback${suffix}`), ORIGIN), false, suffix);
  assert.equal(await auth.isValidCentralAuthRequest("session", request("session", { headers: { Cookie: "__Host-aon-central-session=opaque" } }), ORIGIN), true);
  assert.equal(await auth.isValidCentralAuthRequest("session", request("session?user=forged", { headers: { Cookie: "__Host-aon-central-session=opaque" } }), ORIGIN), false);
  assert.equal(await auth.isValidCentralAuthRequest("session", request("session", { headers: { "X-AON-Role": "admin" } }), ORIGIN), false);
});

test("BFF header relay는 route마다 필요한 browser proof만 Central API에 보낸다", async () => {
  const source = new Headers({
    Cookie: "__Host-aon-central-session=opaque", Origin: ORIGIN, "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "X-AON-CSRF": "csrf",
    Authorization: "Bearer forged", "X-User": "forged", "X-Forwarded-Host": "evil.test", "X-Other": "no",
  });
  assert.deepEqual([...auth.centralAuthRequestHeaders("login-start", source).keys()].sort(), ["origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"]);
  assert.deepEqual([...auth.centralAuthRequestHeaders("callback", source).keys()], ["cookie"]);
  assert.deepEqual([...auth.centralAuthRequestHeaders("session", source).keys()], ["cookie"]);
  assert.deepEqual([...auth.centralAuthRequestHeaders("logout", source).keys()].sort(), ["cookie", "origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "x-aon-csrf"]);
});

test("BFF response relay는 multiple Set-Cookie와 browser-safe header만 보존한다", async () => {
  const upstream = new Response("raw upstream error", {
    status: 401,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Leak": "no", "Set-Cookie": "one=1; Path=/" },
  });
  const relayed = await auth.relayCentralAuthResponse("session", upstream);
  assert.equal(relayed.status, 401);
  assert.equal(await relayed.text(), "raw upstream error");
  assert.equal(relayed.headers.get("x-leak"), null);
  assert.equal(relayed.headers.get("content-type"), "application/json");
  const multi = await auth.relayCentralAuthResponse("callback", new Response(null, {
    status: 303,
    headers: [["Location", "/ask"], ["Set-Cookie", "first=1; Path=/"], ["Set-Cookie", "second=2; Path=/"]],
  }));
  const getter = multi.headers.getSetCookie;
  assert.equal(typeof getter, "function");
  assert.deepEqual(getter.call(multi.headers), ["first=1; Path=/", "second=2; Path=/"]);
});

test("callback redirect는 exact /ask만 relay하고 external/path confusion은 backend body 없이 닫는다", async () => {
  const good = await auth.relayCentralAuthResponse("callback", new Response(null, { status: 303, headers: { Location: "/ask", "Set-Cookie": "first=1; Path=/" } }));
  assert.equal(good.status, 303);
  assert.equal(good.headers.get("location"), "/ask");
  for (const location of ["https://evil.test", "//evil.test", "/ask\\evil", "/%2f%2fevil", "/ask?return_to=evil"]) {
    const result = await auth.relayCentralAuthResponse("callback", new Response("must not leak", { status: 303, headers: { Location: location } }));
    assert.equal(result.status, 502, location);
    assert.equal(result.headers.get("location"), null, location);
    assert.equal(await result.text(), "", location);
  }
});

test("login start redirect는 credential 없는 absolute HTTPS IdP만 relay한다", async () => {
  const good = await auth.relayCentralAuthResponse("login-start", new Response(null, {
    status: 303, headers: { Location: "https://idp.example.test/authorize?opaque=value" },
  }));
  assert.equal(good.status, 303);
  for (const location of ["http://idp.example.test/authorize", "javascript:alert(1)", "/relative", "https://user:secret@idp.example.test/authorize", "https:\\idp.example.test"]) {
    const result = await auth.relayCentralAuthResponse("login-start", new Response("must not leak", { status: 303, headers: { Location: location } }));
    assert.equal(result.status, 502, location);
    assert.equal(result.headers.get("location"), null, location);
    assert.equal(await result.text(), "", location);
  }
});
