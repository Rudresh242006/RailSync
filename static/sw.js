/**
 * RailSync Progressive Web App (PWA) Service Worker
 * Handles offline caching, network fallbacks, and mobile push notifications.
 */

const CACHE_NAME = 'railsync-pwa-v2';
const OFFLINE_URL = '/offline';

const PRECACHE_ASSETS = [
  '/',
  '/offline',
  '/static/manifest.json',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
  'https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js'
];

// Install: Pre-cache critical application shell assets with individual resilience
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      for (const asset of PRECACHE_ASSETS) {
        try {
          await cache.add(asset);
        } catch (err) {
          console.warn('[SW] Pre-caching item skipped:', asset, err);
        }
      }
    }).then(() => self.skipWaiting())
  );
});

// Activate: Clean up old cache versions
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('[SW] Removing old cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: Network-first for pages, Cache-first / Stale-While-Revalidate for static
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = req.url;

  // Skip non-GET, WebSockets, healthchecks, and dynamic REST APIs (ensures real-time accuracy)
  if (
    req.method !== 'GET' ||
    url.includes('/socket.io/') ||
    url.includes('/api/') ||
    url.includes('/user/api/') ||
    url.includes('/admin/api/') ||
    url.includes('/health') ||
    url.includes('/ping') ||
    !url.startsWith('http')
  ) {
    return;
  }

  // HTML page navigations -> Network-first with offline fallback
  if (req.mode === 'navigate' || (req.headers.get('accept') && req.headers.get('accept').includes('text/html'))) {
    event.respondWith(
      fetch(req)
        .then((networkRes) => {
          if (networkRes.status === 200) {
            const resClone = networkRes.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, resClone));
          }
          return networkRes;
        })
        .catch(async () => {
          const cachedRes = await caches.match(req);
          if (cachedRes) return cachedRes;
          const offlinePage = await caches.match(OFFLINE_URL);
          return offlinePage || new Response('<h1>Offline</h1><p>Please reconnect to the internet.</p>', {
            headers: { 'Content-Type': 'text/html' }
          });
        })
    );
    return;
  }

  // Static assets (CSS, JS, Fonts, Images) -> Stale-While-Revalidate
  event.respondWith(
    caches.match(req).then((cachedRes) => {
      const fetchPromise = fetch(req)
        .then((networkRes) => {
          if (networkRes && networkRes.status === 200) {
            const resClone = networkRes.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, resClone));
          }
          return networkRes;
        })
        .catch(() => cachedRes);

      return cachedRes || fetchPromise;
    })
  );
});

// Mobile Push Notifications
self.addEventListener('push', (event) => {
  let data = { title: 'RailSync Alert', body: 'New railway platform update available.', url: '/' };
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: '/static/icons/icon-192x192.png',
    badge: '/static/icons/icon-192x192.png',
    vibrate: [200, 100, 200],
    data: { url: data.url || '/' },
    actions: [
      { action: 'open', title: 'Open RailSync' },
      { action: 'dismiss', title: 'Dismiss' }
    ]
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

// Push notification click
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'dismiss') return;

  const targetUrl = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (let client of windowClients) {
        if (client.url === targetUrl && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
