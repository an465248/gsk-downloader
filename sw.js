/* gsk-downloader service worker: app shell offline, API hamesha network */
const CACHE = "gsk-v2";
const SHELL = ["/", "/manifest.json", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // API calls: hamesha fresh network (kabhi cache mat karo)
  if (url.pathname.startsWith("/api/")) return;
  // ffmpeg CDN + media streams: SW se door rakho (range/progress na toote)
  if (url.hostname.includes("cdn.jsdelivr.net") || url.hostname.includes("googlevideo.com")) return;
  if (e.request.method !== "GET") return;
  e.respondWith(
    caches.match(e.request).then(
      (hit) => hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
    )
  );
});
