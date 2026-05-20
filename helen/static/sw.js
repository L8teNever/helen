// HELEN service worker: minimal cache-first for static assets, network-only for everything else.
const CACHE = "helen-v1";
const STATIC = [
  "/",
  "/static/css/m3.css",
  "/static/css/m3-expressive.css",
  "/static/js/app.js",
  "/static/icon.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC).catch(() => null)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.pathname.startsWith("/events") || url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    e.respondWith(
      caches.open(CACHE).then(async (c) => {
        const hit = await c.match(e.request);
        if (hit) return hit;
        const res = await fetch(e.request);
        if (res.ok) c.put(e.request, res.clone());
        return res;
      })
    );
  }
});
