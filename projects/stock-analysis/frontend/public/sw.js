// Service Worker: Web Push の受信と通知クリック時の画面表示のみ（オフラインキャッシュはしない）
self.addEventListener("push", (event) => {
  let data = { title: "株分析", body: "", url: "/" };
  try { data = { ...data, ...event.data.json() }; } catch (e) { if (event.data) data.body = event.data.text(); }
  event.waitUntil(self.registration.showNotification(data.title, { body: data.body, data: { url: data.url }, icon: "/icon-192.png" }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data?.url || "/"));
});
