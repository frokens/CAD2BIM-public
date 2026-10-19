import { proxyComponentRequest } from "../proxy.mjs";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ path: string[] }> };

async function forward(request: Request, context: RouteContext) {
  const { path } = await context.params;
  return proxyComponentRequest(request, path);
}

export const GET = forward;
export const POST = forward;
export const DELETE = forward;
