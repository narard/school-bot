const TUNNEL_SECRET = "dzetva_sync_2026_secret";
const CACHE_KEY_URL = "https://dzetva.by/__internal_tunnel_cache";
const TELEGRAM_BOT_URL = "https://t.me/SC199_DNEVNIK_bot";

let CURRENT_TUNNEL_URL = "";

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function getExpectedSecret(env) {
  if (env && typeof env.TUNNEL_SECRET === "string" && env.TUNNEL_SECRET.trim()) {
    return env.TUNNEL_SECRET.trim();
  }
  return TUNNEL_SECRET;
}

function normalizeValidTunnelUrl(raw, requestHostname = "") {
  if (typeof raw !== "string") {
    return "";
  }
  const candidate = raw.trim().replace(/\/+$/, "");
  if (!candidate.startsWith("https://")) {
    return "";
  }
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    return "";
  }
  if (parsed.protocol !== "https:") {
    return "";
  }
  const targetHost = parsed.hostname.toLowerCase();
  const currentHost = (requestHostname || "").toLowerCase();
  if (
    !targetHost ||
    targetHost === "dzetva.by" ||
    targetHost.endsWith(".dzetva.by") ||
    (currentHost && targetHost === currentHost)
  ) {
    return "";
  }
  return candidate;
}

async function readTunnelUrl(env, requestHostname = "") {
  const memUrl = normalizeValidTunnelUrl(CURRENT_TUNNEL_URL, requestHostname);
  if (memUrl) {
    return memUrl;
  }

  try {
    if (typeof caches !== "undefined" && caches.default) {
      const cached = await caches.default.match(new Request(CACHE_KEY_URL));
      if (cached && cached.ok) {
        const cachedText = await cached.text();
        const cachedUrl = normalizeValidTunnelUrl(cachedText, requestHostname);
        if (cachedUrl) {
          CURRENT_TUNNEL_URL = cachedUrl;
          return cachedUrl;
        }
      }
    }
  } catch {
    // Ignore Cache API read errors and fall through to env fallback
  }

  if (env && typeof env.ORIGIN_TUNNEL_URL === "string") {
    const envUrl = normalizeValidTunnelUrl(env.ORIGIN_TUNNEL_URL, requestHostname);
    if (envUrl) {
      return envUrl;
    }
  }

  return "";
}

async function writeTunnelUrl(target, ctx) {
  CURRENT_TUNNEL_URL = target;
  try {
    if (typeof caches !== "undefined" && caches.default) {
      const cacheReq = new Request(CACHE_KEY_URL);
      const cacheResp = new Response(target, {
        status: 200,
        headers: {
          "Content-Type": "text/plain; charset=utf-8",
          "Cache-Control": "public, max-age=604800",
        },
      });
      await caches.default.put(cacheReq, cacheResp);
    }
  } catch {
    // Keep in-memory value even if cache write fails
  }
}

function isHtmlRequest(request, url) {
  if (url.pathname.startsWith("/api/")) {
    return false;
  }
  const accept = (request.headers.get("Accept") || "").toLowerCase();
  if (accept.includes("text/html")) {
    return true;
  }
  if (
    url.pathname === "/" ||
    url.pathname === "/app" ||
    url.pathname === "/app/" ||
    url.pathname === "/themes" ||
    url.pathname.endsWith(".html")
  ) {
    return true;
  }
  return !/\.[a-z0-9]{1,8}$/i.test(url.pathname);
}

function renderFallbackHtml(reason = "offline") {
  const statusNote =
    reason === "offline"
      ? "Домашний сервер Dzetva сейчас запускается или обновляет защищённый канал связи."
      : "Сервер Dzetva временно перезагружается или не отвечает. Обычно это занимает 10–20 секунд.";

  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dzetva — dzetva.by</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0b1120;
      --card: #111a2e;
      --border: rgba(148, 163, 184, 0.2);
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-2: #22c55e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: radial-gradient(circle at top, #172554 0%, var(--bg) 60%);
      color: var(--text);
    }
    .card {
      width: 100%;
      max-width: 480px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 32px 28px;
      text-align: center;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
    }
    .logo {
      width: 72px;
      height: 72px;
      margin: 0 auto 18px;
      border-radius: 20px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 12px 28px rgba(56, 189, 248, 0.3);
    }
    h1 {
      margin: 0 0 6px;
      font-size: 28px;
      letter-spacing: -0.02em;
    }
    .domain {
      display: inline-block;
      margin-bottom: 18px;
      padding: 4px 12px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.12);
      color: var(--accent);
      font-size: 13px;
      font-weight: 600;
    }
    p {
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.55;
      font-size: 15px;
    }
    .notice {
      margin: 18px 0 24px;
      padding: 14px 16px;
      border-radius: 14px;
      background: rgba(148, 163, 184, 0.08);
      border: 1px solid var(--border);
      color: #cbd5e1;
      font-size: 14px;
      line-height: 1.5;
    }
    .actions {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 13px 18px;
      border-radius: 14px;
      font-size: 15px;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      border: none;
      transition: transform 0.15s ease, opacity 0.15s ease;
    }
    .btn:hover { transform: translateY(-1px); opacity: 0.95; }
    .btn-primary {
      background: linear-gradient(135deg, #0ea5e9, #2563eb);
      color: #ffffff;
    }
    .btn-secondary {
      background: rgba(148, 163, 184, 0.12);
      color: var(--text);
      border: 1px solid var(--border);
    }
  </style>
</head>
<body>
  <main class="card">
    <div class="logo" aria-hidden="true">
      <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 10v6M2 10l10-5 10 5-10 5z"></path>
        <path d="M6 12v5c3 3 9 3 12 0v-5"></path>
      </svg>
    </div>
    <h1>Dzetva</h1>
    <div class="domain">dzetva.by</div>
    <p>Школьный помощник для родителей: баланс питания ЕРИП, кружки, учёт расходов и дневник Schools.by.</p>
    <div class="notice">${statusNote}</div>
    <div class="actions">
      <a class="btn btn-primary" href="${TELEGRAM_BOT_URL}" target="_blank" rel="noopener noreferrer">
        Открыть Telegram-бот Dzetva
      </a>
      <button class="btn btn-secondary" type="button" onclick="window.location.reload()">
        Обновить страницу
      </button>
    </div>
  </main>
</body>
</html>`;
}

function htmlFallbackResponse(request, reason, status = 503) {
  const body = request.method === "HEAD" ? null : renderFallbackHtml(reason);
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/__dzetva_tunnel_sync") {
      if (request.method !== "POST") {
        return jsonResponse({ ok: false, error: "Method not allowed" }, 405);
      }
      const providedSecret = (
        request.headers.get("X-Tunnel-Secret") ||
        url.searchParams.get("secret") ||
        ""
      ).trim();
      if (!providedSecret || providedSecret !== getExpectedSecret(env)) {
        return jsonResponse({ ok: false, error: "Forbidden" }, 403);
      }

      let payload;
      try {
        payload = await request.json();
      } catch {
        return jsonResponse({ ok: false, error: "Invalid JSON payload" }, 400);
      }

      const target = normalizeValidTunnelUrl(payload && payload.url, url.hostname);
      if (!target) {
        return jsonResponse(
          { ok: false, error: "Invalid tunnel URL (must be https:// and not dzetva.by)" },
          400
        );
      }

      await writeTunnelUrl(target, ctx);
      return jsonResponse({ ok: true, synced: target }, 200);
    }

    if (url.pathname === "/__dzetva_tunnel_status") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return jsonResponse({ ok: false, error: "Method not allowed" }, 405);
      }
      const tunnelUrl = await readTunnelUrl(env, url.hostname);
      return jsonResponse({ ok: true, tunnel_url: tunnelUrl || null }, 200);
    }

    const tunnelUrl = await readTunnelUrl(env, url.hostname);
    const wantsHtml = isHtmlRequest(request, url);

    if (!tunnelUrl) {
      if (wantsHtml) {
        return htmlFallbackResponse(request, "offline", 503);
      }
      return jsonResponse({ ok: false, error: "Tunnel is offline" }, 503);
    }

    const targetUrl = tunnelUrl.replace(/\/$/, "") + url.pathname + url.search;
    const proxyHeaders = new Headers(request.headers);
    proxyHeaders.delete("host");
    proxyHeaders.set("X-Forwarded-Host", url.hostname);
    proxyHeaders.set("X-Forwarded-Proto", "https");
    proxyHeaders.set("X-Dzetva-Public", "1");

    const hasBody = request.method !== "GET" && request.method !== "HEAD";
    try {
      const upstreamResp = await fetch(targetUrl, {
        method: request.method,
        headers: proxyHeaders,
        body: hasBody ? request.body : undefined,
        redirect: "manual",
      });

      if (
        wantsHtml &&
        (upstreamResp.status === 502 || upstreamResp.status === 503 || upstreamResp.status === 504)
      ) {
        return htmlFallbackResponse(request, "upstream_unavailable", 503);
      }

      return new Response(upstreamResp.body, {
        status: upstreamResp.status,
        statusText: upstreamResp.statusText,
        headers: new Headers(upstreamResp.headers),
      });
    } catch {
      if (wantsHtml) {
        return htmlFallbackResponse(request, "network_error", 503);
      }
      return jsonResponse({ ok: false, error: "Upstream tunnel unreachable" }, 502);
    }
  },
};
