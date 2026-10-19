const id = "[A-Za-z0-9_-]+";
const run = `/tasks/${id}/runs/${id}`;
// Only the public Component API is reachable. Backend grants and state access stay private.
const routes = [
  ["GET", /^\/tasks$/, ["limit", "after_task_id"]],
  ["POST", /^\/tasks$/, []],
  ["GET", new RegExp(`^/tasks/${id}$`), []],
  ["DELETE", new RegExp(`^/tasks/${id}$`), []],
  ["GET", /^\/library\/(geometry|families)$/, ["task_id", "run_id", "page_size", "cursor"]],
  ["DELETE", /^\/library\/(geometry|families)$/, ["task_id", "run_id", "reference"]],
  ["GET", /^\/library\/geometry\/info$/, ["task_id", "run_id", "reference"]],
  ["GET", /^\/library\/families\/info$/, ["task_id", "run_id", "reference", "before_version_no", "page_size"]],
  ["POST", /^\/library\/code$/, ["task_id", "run_id"]],
  ["POST", new RegExp(`^/tasks/${id}/attachments$`), []],
  ["POST", new RegExp(`^/tasks/${id}/runs$`), []],
  ["GET", new RegExp(`^${run}$`), []],
  ["GET", new RegExp(`^${run}/snapshot$`), ["limit", "before_sequence"]],
  ["GET", new RegExp(`^${run}/display$`), ["source_kind", "source_ref"]],
  ["POST", new RegExp(`^${run}/display$`), []],
  ["GET", new RegExp(`^${run}/events$`), ["after_sequence"]],
  ["GET", new RegExp(`^${run}/items/${id}$`), []],
  ["GET", new RegExp(`^${run}/items/${id}/content/[0-9]+$`), []],
  ["GET", new RegExp(`^/tasks/${id}/artifacts/${id}/versions/${id}/content$`), []],
  ["POST", new RegExp(`^${run}/(feedback|pause|resume)$`), []],
  ["GET", new RegExp(`^${run}/gates/current$`), []],
  ["POST", new RegExp(`^${run}/gates/${id}/respond$`), []],
];

export async function proxyComponentRequest(request, path, { fetch: fetchRequest = globalThis.fetch, env = process.env } = {}) {
  const incoming = new URL(request.url);
  const publicPath = `/${path.join("/")}`;
  const route = routes.find(([method, pattern]) => method === request.method && pattern.test(publicPath));
  if (!route || [...incoming.searchParams.keys()].some((key) => !route[2].includes(key))) {
    return Response.json({ detail: "route not available" }, { status: 404 });
  }
  const origin = request.headers.get("origin");
  if (origin && origin !== incoming.origin) {
    return Response.json({ detail: "same-origin request required" }, { status: 403 });
  }
  if (!env.COMPONENT_BACKEND_URL || !env.COMPONENT_BACKEND_BEARER) {
    return Response.json({ detail: "connection unavailable" }, { status: 503 });
  }
  let target;
  try { target = new URL(env.COMPONENT_BACKEND_URL); } catch {
    return Response.json({ detail: "connection unavailable" }, { status: 503 });
  }
  if (!/^https?:$/.test(target.protocol) || target.username || target.password || target.search || target.hash) {
    return Response.json({ detail: "connection unavailable" }, { status: 503 });
  }
  target.pathname = `${target.pathname.replace(/\/$/, "")}${publicPath}`;
  target.search = incoming.search;
  const headers = new Headers({ authorization: `Bearer ${env.COMPONENT_BACKEND_BEARER}` });
  for (const name of ["content-type", "accept", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  try {
    const upstream = await fetchRequest(target, {
      method: request.method,
      headers,
      body: request.method === "GET" ? undefined : request.body,
      duplex: "half",
      redirect: "manual",
      signal: request.signal,
    });
    // Never follow a redirect with the server's bearer or expose upstream auth/cookies.
    if (upstream.status >= 300 && upstream.status < 400) {
      await upstream.body?.cancel();
      return Response.json({ detail: "upstream redirect rejected" }, { status: 502 });
    }
    const responseHeaders = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff" });
    for (const name of ["content-type", "content-disposition"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    if (upstream.headers.get("content-type")?.startsWith("text/event-stream")) {
      responseHeaders.set("x-accel-buffering", "no");
    }
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch {
    return Response.json({ detail: "connection unavailable" }, { status: 502 });
  }
}
