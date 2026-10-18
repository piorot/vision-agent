// Receives Web Push messages and shows a notification that carries the vibration pattern.
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data.json(); } catch (e) {}
  const n = Math.max(0, Math.min(10, Number(d.vibrations) || 0));
  const vibrate = [];
  for (let i = 0; i < n; i++) vibrate.push(300, 200); // 300ms on, 200ms off
  const part = d.total > 1 ? `${d.index + 1}/${d.total}: ` : ""; // several questions in one image
  event.waitUntil(self.registration.showNotification("Vision Vibe", {
    body: `${part}${d.answer ?? ""} (${n} vibration${n === 1 ? "" : "s"})`,
    vibrate,
    tag: "answer",
    renotify: true, // buzz again even though a notification with this tag is still showing
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: "window" }).then((list) =>
    list.length ? list[0].focus() : clients.openWindow("/")));
});
