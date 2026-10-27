/* Build placeholders are replaced by tools/pwa-build.mjs. Never cache page/API URLs. */
const VERSION = __VM_VERSION__;
const ASSETS = new Set(__VM_ASSETS__);
const OFFLINE = __VM_OFFLINE__;
const PREFIX = "vendormap-static-";
const CACHE = `${PREFIX}${VERSION}`;
const VERSIONED = /^\/assets\/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css|svg|png|webp|woff2|html)$/;

function isStatic(request, url) {
  return request.method === "GET" && url.origin === self.location.origin && !url.search
    && VERSIONED.test(url.pathname) && !request.headers.has("authorization")
    && !request.headers.has("range");
}
function validAsset(response, pathname) {
  const expected = {js: /(?:java|ecma)script/, css: /text\/css/, svg: /image\/svg\+xml/, png: /image\/png/,
    webp: /image\/webp/, woff2: /(?:font\/woff2|application\/octet-stream)/, html: /text\/html/};
  return response.status === 200 && !response.redirected
    && expected[pathname.split(".").at(-1)]?.test(response.headers.get("content-type") || "");
}

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    try {
      await Promise.all([...ASSETS].map(async pathname => {
        const response = await fetch(new Request(pathname, {credentials: "omit", cache: "no-store"}));
        if (!validAsset(response, pathname)) throw new Error("Static build asset unavailable");
        await cache.put(pathname, response);
      }));
    } catch (error) {await caches.delete(CACHE); throw error;}
    // Updates wait for closed windows or an explicit Reload action.
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    // Retain immutable old chunks while open windows may still import them.
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    if (!windows.length) {
      await Promise.all((await caches.keys()).filter(name => name.startsWith(PREFIX) && name !== CACHE).map(name => caches.delete(name)));
    }
  })());
});

self.addEventListener("message", event => {
  if (!event.source?.url || new URL(event.source.url).origin !== self.location.origin) return;
  if (event.data?.type === "ACTIVATE_UPDATE") {
    event.waitUntil(self.skipWaiting());
  }
  if (event.data?.type === "CLIENT_READY" && ASSETS.has(event.data.entry) && event.data.entry.endsWith(".js")) {
    event.waitUntil((async () => {
      const active = self.registration.active;
      const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
      if (windows.length !== 1 || windows[0].id !== event.source.id) return;
      const names = await caches.keys();
      for (const name of names) {
        // Do not remove a waiting/installing build, or assets needed by an older window.
        if (active?.state !== "activated" || self.registration.installing || self.registration.waiting) return;
        if (name.startsWith(PREFIX) && name !== CACHE) await caches.delete(name);
      }
    })());
  }
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
    // The API client already uses no-store. Let the browser send this request
    // directly; a worker fetch adds an unnecessary second network initiator.
    return;
  }
  if (request.mode === "navigate" && url.origin === self.location.origin) {
    event.respondWith(fetch(request, {cache: "no-store"}).catch(async () => {
      const cache = await caches.open(CACHE);
      return await cache.match(OFFLINE) || new Response("Connect to the internet to open VendorMap.", {status: 503, headers: {"Content-Type": "text/plain", "Cache-Control": "no-store"}});
    }));
    return;
  }
  if (!isStatic(request, url)) return;
  event.respondWith((async () => {
    const ownCaches = (await caches.keys()).filter(name => name.startsWith(PREFIX));
    for (const name of [CACHE, ...ownCaches.filter(name => name !== CACHE)]) {
      const cached = await (await caches.open(name)).match(url.pathname);
      if (cached) return cached;
    }
    // Unknown hashed paths may be fetched, but only this build's allowlist may be stored.
    const response = await fetch(request, {credentials: "omit", cache: "no-store"});
    if (ASSETS.has(url.pathname) && validAsset(response, url.pathname)) {
      await (await caches.open(CACHE)).put(url.pathname, response.clone());
    }
    return response;
  })());
});
