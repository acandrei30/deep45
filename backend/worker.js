/**
 * Klar backend — Cloudflare Worker that proxies OpenAI requests on behalf of
 * the Klar desktop app. Friends' apps talk to this Worker instead of OpenAI
 * directly, so they never need their own API key.
 *
 * Required Worker secrets (set with `wrangler secret put`):
 *   OPENAI_API_KEY     — your OpenAI sk-... key
 *   KLAR_SHARED_SECRET — a random string the app sends in X-Klar-Auth header
 *
 * Only chat completions and Whisper transcriptions are forwarded.
 */

const ALLOWED_PATHS = new Set([
  "/v1/chat/completions",
  "/v1/audio/transcriptions",
]);

export default {
  async fetch(request, env) {
    // Auth: shared-secret header. Easy to rotate by updating the secret.
    const auth = request.headers.get("x-klar-auth");
    if (!env.KLAR_SHARED_SECRET || auth !== env.KLAR_SHARED_SECRET) {
      return new Response("unauthorized", { status: 401 });
    }

    const url = new URL(request.url);
    if (!ALLOWED_PATHS.has(url.pathname)) {
      return new Response("path not allowed", { status: 403 });
    }

    if (!env.OPENAI_API_KEY) {
      return new Response("server not configured", { status: 500 });
    }

    // Forward request to OpenAI with our key.
    const headers = new Headers(request.headers);
    headers.set("authorization", `Bearer ${env.OPENAI_API_KEY}`);
    headers.delete("x-klar-auth");
    headers.delete("host");

    const upstream = await fetch("https://api.openai.com" + url.pathname, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    });

    // Stream the response straight back.
    return new Response(upstream.body, {
      status: upstream.status,
      headers: upstream.headers,
    });
  },
};
