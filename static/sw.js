const CACHE = 'astra-v1';
const SHELL = ['/', '/static/style.css', '/static/app.js', '/static/logo.svg', '/static/favicon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(()=>{}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // API va kino sahifalari — har doim yangi ma'lumot (network-first)
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/kino/')) {
    e.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }

  // Statik fayllar — avval keshdan (cache-first), keyin tarmoqdan
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy)).catch(()=>{});
      return res;
    }).catch(() => caches.match('/')))
  );
});
