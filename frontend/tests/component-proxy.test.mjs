import test from "node:test";
import assert from "node:assert/strict";
import { proxyComponentRequest } from "../app/api/component/proxy.mjs";

const env = { COMPONENT_BACKEND_URL: "http://127.0.0.1:8090", COMPONENT_BACKEND_BEARER: "server-business-identity" };
const request = (path, init) => new Request(`http://localhost:3000/api/component/${path}`, init);

test("proxy streams public responses and uses only its server-side business identity", async () => {
  let forwarded;
  const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode("id: 12\nevent: item\ndata: {}\n\n")); controller.close(); } });
  const response = await proxyComponentRequest(request("tasks/task-1/runs/run-1/events?after_sequence=10", {
    headers: { authorization: "Bearer browser-token", cookie: "session=browser-secret", "last-event-id": "12" },
  }), ["tasks", "task-1", "runs", "run-1", "events"], { env, fetch: async (url, options) => {
    forwarded = { url: String(url), options };
    return new Response(body, { headers: { "content-type": "text/event-stream", "set-cookie": "upstream=secret", authorization: "Bearer internal" } });
  } });
  assert.equal(forwarded.url, "http://127.0.0.1:8090/tasks/task-1/runs/run-1/events?after_sequence=10");
  assert.equal(forwarded.options.headers.get("authorization"), "Bearer server-business-identity");
  assert.equal(forwarded.options.headers.get("cookie"), null);
  assert.equal(forwarded.options.headers.get("last-event-id"), "12");
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("authorization"), null);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.match(await response.text(), /id: 12/);
});

test("proxy rejects private routes, arbitrary URLs, unknown query fields and cross-origin writes", async () => {
  let calls = 0;
  const options = { env, fetch: async () => { calls += 1; return Response.json({}); } };
  for (const path of ["tasks?cursor=old", "tasks/task-1/runs/run-1/state-read-access", "https://other.invalid/", "tasks/task-1/runs/run-1/events?url=https://other.invalid/", "tasks/task-1/runs/run-1/events?grant=secret"]) {
    const response = await proxyComponentRequest(request(path), path.split("?")[0].split("/"), options);
    assert.equal(response.status, 404);
  }
  const response = await proxyComponentRequest(request("tasks", { method: "POST", headers: { origin: "https://other.invalid" }, body: "{}" }), ["tasks"], options);
  assert.equal(response.status, 403);
  assert.equal(calls, 0);
});

test("delete and library routes preserve scope and reject unapproved query fields", async () => {
  for (const [method, path] of [["DELETE", "tasks/task-1"],
    ["DELETE", "library/families?reference=fr_root&task_id=task-1&run_id=run-1"],
    ["DELETE", "library/geometry?reference=gr_root&task_id=task-1&run_id=run-1"],
    ["GET", "library/families?task_id=task-1&run_id=run-1&page_size=20"],
    ["GET", "library/families/info?reference=fv_exact&task_id=task-1&run_id=run-1"],
    ["POST", "library/code?task_id=task-1&run_id=run-1"]]) {
    const response = await proxyComponentRequest(request(path, { method }), path.split("?")[0].split("/"), { env, fetch: async (url, options) => {
      assert.equal(options.method, method);
      assert.equal(new URL(url).pathname + new URL(url).search, `/${path}`);
      return method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({});
    } });
    assert.equal(response.status, method === "DELETE" ? 204 : 200);
  }
  const denied = await proxyComponentRequest(request("library/code?source_path=/private", { method: "POST" }), ["library", "code"], { env, fetch: () => assert.fail("must not forward") });
  assert.equal(denied.status, 404);
  for (const path of ["library/geometry?reference=gr_root&cursor=unused", "library/families/info?reference=fr_root"]) {
    const rejected = await proxyComponentRequest(request(path, { method: "DELETE" }), path.split("?")[0].split("/"), { env, fetch: () => assert.fail("must not forward") });
    assert.equal(rejected.status, 404);
  }
  const crossOrigin = await proxyComponentRequest(request("library/geometry?reference=gr_root", { method: "DELETE", headers: { origin: "https://other.invalid" } }), ["library", "geometry"], { env, fetch: () => assert.fail("must not forward") });
  assert.equal(crossOrigin.status, 403);
});

test("proxy preserves multipart upload body without accepting a browser-selected backend", async () => {
  const body = new FormData();
  body.append("file", new File(["bytes"], "reference.png", { type: "image/png" }));
  body.append("client_request_id", "first:attachment:0");
  let forwarded;
  const response = await proxyComponentRequest(request("tasks/task-1/attachments", { method: "POST", body }), ["tasks", "task-1", "attachments"], { env, fetch: async (url, options) => {
    forwarded = await new Request(url, options).formData();
    return Response.json({ artifact_id: "artifact-1", version_id: "version-1" });
  } });
  assert.equal(response.status, 200);
  assert.equal(forwarded.get("client_request_id"), "first:attachment:0");
  assert.equal(await forwarded.get("file").text(), "bytes");
});

test("missing local identity and upstream redirects do not expose credentials", async () => {
  const absent = await proxyComponentRequest(request("tasks"), ["tasks"], { env: {}, fetch: async () => { throw new Error("should not fetch"); } });
  assert.equal(absent.status, 503);
  const redirected = await proxyComponentRequest(request("tasks"), ["tasks"], { env, fetch: async (_, options) => {
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 302, headers: { location: "https://other.invalid/?token=secret" } });
  } });
  assert.equal(redirected.status, 502);
  assert.equal(redirected.headers.get("location"), null);
  assert.doesNotMatch(await redirected.text(), /secret|127\.0\.0\.1/);
});
