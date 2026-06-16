const CACHE = 'astra-v3';
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
  if (url.origin !== self.location.origin) return;

  // Hammasi uchun: avval tarmoqdan (yangi versiya), faqat internet yo'q bo'lsa keshdan
  e.respondWith(
    fetch(req).then(res => {
      // muvaffaqiyatli javobni keshga yozamiz (offline uchun)
      if (res && res.status === 200 && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(()=>{});
      }
      return res;
    }).catch(() => caches.match(req).then(hit => hit || caches.match('/')))
  );
});