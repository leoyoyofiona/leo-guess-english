/* LEO · Service Worker：应用外壳缓存，离线可用（离线时 API 探测失败 → 自动回退本地模式）
   缓存策略：
   - 导航/HTML：网络优先（保证更新即时生效），离线回退缓存
   - 静态资源（js/svg/png/mp4/webmanifest）：缓存优先
   - /api/*：永不缓存 */
const CACHE = "leo-v6";
const SHELL = ["./", "./index.html", "./scenes.st.js", "./icon.svg", "./icon-192.png", "./icon-512.png", "./manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  const isNav = e.request.mode === "navigate";
  if (isNav) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request).then(hit => hit || caches.match("./index.html")))
    );
    return;
  }

  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone();
      if (res.ok && SHELL.includes(url.pathname)) {
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    }))
  );
});
