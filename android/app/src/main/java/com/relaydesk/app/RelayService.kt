package com.relaydesk.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.IBinder
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * Always-on host for discovery, the device channel and presence probes.
 * The Activity can be destroyed without losing incoming messages; Android
 * keeps this alive as a dataSync foreground service (with the required
 * ongoing notification).
 */
class RelayService : Service() {

    companion object {
        const val CH_SERVICE = "relaydesk_service"
        const val CH_MESSAGES = "relaydesk_messages"
        const val ACTION_REFRESH = "com.relaydesk.app.REFRESH"
        private const val NOTIF_SERVICE = 1
        private const val NOTIF_SUMMARY = 1000
        private const val FALLBACK_PORT = 18991

        fun start(context: Context) {
            val intent = Intent(context, RelayService::class.java)
            context.startForegroundService(intent)
        }
    }

    private lateinit var store: Store
    private lateinit var fingerprint: String
    private lateinit var alias: String
    private var server: ChannelServer? = null
    private var discovery: Discovery? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var worker: ScheduledExecutorService? = null

    override fun onCreate() {
        super.onCreate()
        store = Store(this)
        fingerprint = Identity.fingerprint(this)
        alias = Identity.alias(this)
        createChannels()
        // Must be foreground within 5s of startForegroundService.
        startForeground(NOTIF_SERVICE, serviceNotification("正在启动…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        Runtime.service = this

        val wifi = applicationContext.getSystemService(WifiManager::class.java)
        multicastLock = wifi.createMulticastLock("relaydesk").apply {
            setReferenceCounted(false)
            acquire()
        }

        val port = startChannel()
        Runtime.servicePort = port ?: 0
        Runtime.serviceRunning = true
        if (port == null) {
            updateServiceNotification("设备通道不可用：8891/18991 均被占用")
            Runtime.emit("error", "设备通道端口被占用")
        } else {
            startDiscovery(port)
            updateServiceNotification("设备通道 :$port · 后台接收已开启")
        }

        worker = Executors.newSingleThreadScheduledExecutor { r ->
            Thread(r, "relaydesk-probe").apply { isDaemon = true }
        }
        worker?.scheduleWithFixedDelay({ probeDevices() }, 10, 60, TimeUnit.SECONDS)
        worker?.scheduleWithFixedDelay({ discovery?.announce(times = 2) },
            30, 30, TimeUnit.SECONDS)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_REFRESH) discovery?.announce()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        worker?.shutdownNow()
        discovery?.stop()
        runCatching { server?.stop() }
        multicastLock?.takeIf { it.isHeld }?.release()
        Runtime.serviceRunning = false
        Runtime.servicePort = 0
        Runtime.service = null
        super.onDestroy()
    }

    fun announceNow() {
        discovery?.announce()
    }

    // ---------- channel / discovery ----------

    private fun startChannel(): Int? {
        for (candidate in intArrayOf(Wire.DEVICE_PORT, FALLBACK_PORT)) {
            val candidateServer = ChannelServer(this, store, fingerprint, alias,
                candidate, ::onServerEvent, ::onFileProgress, ::requestPair)
            try {
                candidateServer.start(5000)
                server = candidateServer
                return candidate
            } catch (_: IOException) {
                candidateServer.stop()
            }
        }
        return null
    }

    private fun startDiscovery(port: Int) {
        val d = Discovery(fingerprint, alias, port) { dev ->
            runCatching {
                store.upsertDevice(dev.optString("fingerprint"),
                    dev.optString("alias"), dev.optString("deviceModel"),
                    dev.optInt("port", Wire.DEVICE_PORT),
                    dev.optInt("version"), dev.optString("source"),
                    dev.optString("os"))
            }
            Runtime.emit("devices")
        }
        if (!d.start()) {
            Runtime.emit("error", "UDP ${Wire.DEVICE_PORT} 绑定失败，只能用手动 IP 添加")
        } else {
            discovery = d
            d.announce()
        }
    }

    // ---------- presence ----------

    private fun probeDevices() {
        for (dev in store.devices()) {
            val fp = dev.optString("fingerprint")
            val ip = dev.optString("ip")
            val port = dev.optInt("port", 0)
            if (fp.isEmpty() || fp == fingerprint || ip.isEmpty() || port <= 0) continue
            val modal = try {
                ChannelClient.pingV2(ip, port, fingerprint, alias,
                    Runtime.servicePort, timeoutMs = 3000)
            } catch (_: Exception) {
                continue // stale seenAt -> UI shows offline
            }
            runCatching {
                // The peer may have re-created its identity behind this address;
                // record the fingerprint that actually answered, never the stale one.
                val realFp = modal.optString("fingerprint").ifEmpty { fp }
                store.upsertDevice(realFp,
                    modal.optString("alias", dev.optString("alias")),
                    modal.optString("deviceModel", dev.optString("deviceModel")),
                    modal.optInt("port", port),
                    modal.optInt("version", dev.optInt("version")),
                    ip, modal.optString("os", dev.optString("os")))
            }
        }
        Runtime.emit("devices")
    }

    // ---------- server events ----------

    private fun requestPair(peer: String, code: String): Boolean =
        Runtime.requestPair(peer, code)

    private fun onFileProgress(payload: JSONObject) {
        Runtime.emit("fileProgress", payload)
    }

    private fun onServerEvent(event: String, payload: JSONObject) {
        val from = payload.optString("from")
        val groupId = payload.optString("groupId")
        when (event) {
            "message" -> {
                if (groupId.isNotEmpty()) {
                    notifyGroup(groupId, from, payload.optString("preview"),
                        payload.optString("kind"))
                    Runtime.emit("groups")
                    if (Runtime.foregroundGroup == groupId) Runtime.emit("messages", groupId)
                } else {
                    notifyPeer(from, payload.optString("preview"), payload.optString("kind"))
                    Runtime.emit("devices")
                    if (Runtime.foregroundPeer == from) Runtime.emit("messages", from)
                }
            }
            "filePending" -> Runtime.emit("filePending", payload)
            "groupSync" -> Runtime.emit("groupSync", payload.optString("groupId"))
        }
    }

    // ---------- notifications ----------

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_SERVICE, "后台服务",
            NotificationManager.IMPORTANCE_LOW).apply {
            description = "RelayDesk 常驻接收通道"
            setShowBadge(false)
        })
        nm.createNotificationChannel(NotificationChannel(CH_MESSAGES, "消息与文件",
            NotificationManager.IMPORTANCE_HIGH).apply {
            description = "收到文本、文件与配对提示"
        })
    }

    private fun openIntent(peer: String? = null, group: String? = null): PendingIntent {
        val intent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            if (!peer.isNullOrEmpty()) putExtra("peer", peer)
            if (!group.isNullOrEmpty()) putExtra("group", group)
        }
        val key = (peer ?: "") + (group ?: "")
        return PendingIntent.getActivity(this, key.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun serviceNotification(text: String): Notification =
        Notification.Builder(this, CH_SERVICE)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("RelayDesk 正在运行")
            .setContentText(text)
            .setContentIntent(openIntent())
            .setOngoing(true)
            .setShowWhen(false)
            .apply {
                if (android.os.Build.VERSION.SDK_INT >= 31) {
                    setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
                }
            }
            .build()

    private fun updateServiceNotification(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIF_SERVICE, serviceNotification(text))
    }

    private fun notifyPeer(peer: String, preview: String, kind: String) {
        if (peer.isEmpty() || Runtime.foregroundPeer == peer) return
        val nm = getSystemService(NotificationManager::class.java)
        if (!nm.areNotificationsEnabled()) return
        val name = store.device(peer)?.optString("alias")
            ?.takeIf { it.isNotEmpty() } ?: peer.take(8)
        val lastRead = getSharedPreferences("relaydesk", MODE_PRIVATE)
            .getLong("lastread_$peer", 0L)
        val unread = store.unreadCount(peer, lastRead).coerceAtLeast(1)
        val title = if (kind == "pair") "已与 $name 配对" else name
        val body = preview.ifEmpty {
            if (kind == "file") "收到文件" else "收到新消息"
        }
        val notification = Notification.Builder(this, CH_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(title)
            .setContentText(body)
            .setNumber(unread)
            .setWhen(System.currentTimeMillis())
            .setAutoCancel(true)
            .setGroup("relaydesk_messages")
            .setContentIntent(openIntent(peer))
            .build()
        nm.notify(peerId(peer), notification)

        val summary = Notification.Builder(this, CH_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("RelayDesk")
            .setContentText("有未读消息")
            .setGroup("relaydesk_messages")
            .setGroupSummary(true)
            .setAutoCancel(true)
            .setContentIntent(openIntent())
            .build()
        nm.notify(NOTIF_SUMMARY, summary)
    }

    private fun peerId(peer: String): Int = 100 + (peer.hashCode() and 0x0FFFFFFF)

    private fun groupId(gid: String): Int = 200 + (gid.hashCode() and 0x0FFFFFFF)

    private fun notifyGroup(gid: String, from: String, preview: String, kind: String) {
        if (gid.isEmpty() || Runtime.foregroundGroup == gid) return
        val nm = getSystemService(NotificationManager::class.java)
        if (!nm.areNotificationsEnabled()) return
        val group = store.group(gid)
        val name = group?.optString("name")?.takeIf { it.isNotEmpty() } ?: "群聊"
        val sender = store.device(from)?.optString("alias")
            ?.takeIf { it.isNotEmpty() } ?: from.take(8)
        val prefs = getSharedPreferences("relaydesk", MODE_PRIVATE)
        val unread = store.groupUnread(gid, fingerprint,
            prefs.getLong("lastread_group_$gid", 0L)).coerceAtLeast(1)
        val body = preview.ifEmpty { if (kind == "file") "收到文件" else "收到新消息" }
        val notification = Notification.Builder(this, CH_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("$name · $sender")
            .setContentText(body)
            .setNumber(unread)
            .setWhen(System.currentTimeMillis())
            .setAutoCancel(true)
            .setGroup("relaydesk_messages")
            .setContentIntent(openIntent(group = gid))
            .build()
        nm.notify(groupId(gid), notification)

        val summary = Notification.Builder(this, CH_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("RelayDesk")
            .setContentText("有未读消息")
            .setGroup("relaydesk_messages")
            .setGroupSummary(true)
            .setAutoCancel(true)
            .setContentIntent(openIntent())
            .build()
        nm.notify(NOTIF_SUMMARY, summary)
    }

    private fun hasUnreadAnywhere(): Boolean {
        val prefs = getSharedPreferences("relaydesk", MODE_PRIVATE)
        val peerUnread = store.devices().any { dev ->
            val fp = dev.optString("fingerprint")
            store.unreadCount(fp, prefs.getLong("lastread_$fp", 0L)) > 0
        }
        if (peerUnread) return true
        return store.groups().any { g ->
            val gid = g.optString("id")
            store.groupUnread(gid, fingerprint,
                prefs.getLong("lastread_group_$gid", 0L)) > 0
        }
    }

    /** Called when the user opens a conversation: drop its notification. */
    fun clearPeerNotifications(peer: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.cancel(peerId(peer))
        if (!hasUnreadAnywhere()) nm.cancel(NOTIF_SUMMARY)
    }

    fun clearGroupNotifications(gid: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.cancel(groupId(gid))
        if (!hasUnreadAnywhere()) nm.cancel(NOTIF_SUMMARY)
    }
}
