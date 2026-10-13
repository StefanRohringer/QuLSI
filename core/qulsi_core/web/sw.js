// QuLSI Service Worker: nur für Benachrichtigungen (Web Push). Kein Cache, keine Offline-Seiten.
// Der Server schickt {titel, text, url, tag}. Was auf dem Sperrbildschirm steht, entscheidet er
// (ohne "Details" pro Gerät nur der Titel).
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener("push", (ev) => {
  let d = {};
  try { d = ev.data ? ev.data.json() : {}; } catch (e) { d = {text: ev.data ? ev.data.text() : ""}; }
  // iPhone verlangt zu jedem Push eine sichtbare Mitteilung, sonst entzieht es die Erlaubnis.
  ev.waitUntil(self.registration.showNotification(d.titel || "QuLSI", {
    body: d.text || "",
    icon: "/apple-touch-icon.png",
    badge: "/apple-touch-icon.png",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: {url: typeof d.url === "string" && d.url.startsWith("/") ? d.url : "/"},
  }));
});

self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  const ziel = new URL(ev.notification.data?.url || "/", self.location.origin).href;
  ev.waitUntil((async () => {
    const fenster = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const f of fenster) {
      if (new URL(f.url).origin === self.location.origin) {
        await f.focus();
        if ("navigate" in f) { try { await f.navigate(ziel); } catch (e) {} }
        return;
      }
    }
    await self.clients.openWindow(ziel);
  })());
});
