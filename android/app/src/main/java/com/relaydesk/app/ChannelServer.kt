package com.relaydesk.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Environment
import android.provider.MediaStore
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject

/**
 * Device channel server on :8891 mirroring flixcli server.py semantics and the
 * confirmed transfer-security gates: paired senders only, source-IP binding,
 * declared-size limits, no overwrite, atomic (IS_PENDING) commit.
 */
class ChannelServer(
    private val context: Context,
    private val store: Store,
    private val ownFp: String,
    private val alias: String,
    private val port: Int,
    private val onEvent: (String, JSONObject) -> Unit,
) : NanoHTTPD(port) {

    private val ownKey: String
        get() = store.ownPairKey(
            context.getSharedPreferences("relaydesk", Context.MODE_PRIVATE))

    override fun serve(session: IHTTPSession): Response {
        val path = session.uri ?: "/"
        return try {
            when (path) {
                "/ping_v2" -> pingV2(session)
                "/bubble" -> bubble(session)
                "/intent" -> intent(session)
                "/file" -> file(session)
                else -> notFound()
            }
        } catch (_: Exception) {
            newFixedLengthResponse(Response.Status.INTERNAL_ERROR, "text/plain",
                "receive failed")
        }
    }

    private fun readJson(session: IHTTPSession): JSONObject? = try {
        // NanoHTTPD's parseBody decodes with a lossy charset; read the raw
        // bytes ourselves (Content-Length) and decode UTF-8 explicitly.
        val len = session.headers["content-length"]?.toLongOrNull()?.coerceAtMost(4L * 1024 * 1024)
            ?: 0L
        val bytes = ByteArray(len.toInt())
        var off = 0
        while (off < len) {
            val n = session.inputStream.read(bytes, off, (len - off).toInt())
            if (n < 0) break
            off += n
        }
        JSONObject(String(bytes, 0, off, Charsets.UTF_8))
    } catch (_: Exception) {
        null
    }

    private fun text(status: Response.Status, body: String) =
        newFixedLengthResponse(status, "text/plain", body)

    private fun notFound() = text(Response.Status.NOT_FOUND, "Route not found")

    private fun pingV2(session: IHTTPSession): Response {
        val body = readJson(session) ?: return text(Response.Status.BAD_REQUEST,
            "Bad Request")
        val modal = body.optJSONObject("deviceModal")
        if (modal == null || modal.length() == 0) {
            return text(Response.Status.BAD_REQUEST, "Bad Request")
        }
        val from = modal.optString("from")
        return text(Response.Status.OK,
            Wire.doubleEncode(Wire.deviceModal(ownFp, alias, port, from)))
    }

    private fun bubble(session: IHTTPSession): Response {
        val bubble = readJson(session) ?: return text(Response.Status.BAD_REQUEST,
            "receive bubble failed")
        val id = bubble.optString("id")
        val from = bubble.optString("from")
        if (id.isEmpty() || from.isEmpty()) {
            return text(Response.Status.BAD_REQUEST, "receive bubble failed")
        }
        val type = bubble.optInt("type")
        val groupId = bubble.optString("groupId")
        when (type) {
            Wire.TYPE_TEXT -> {
                val content = bubble.optString("content")
                store.addMessage(id, from, bubble.optString("to"), type,
                    bubble.optLong("time"), groupId, text = content)
                notifyText(from, content)
                onEvent("message", JSONObject().put("from", from).put("id", id))
            }
            Wire.TYPE_FILE -> {
                val content = bubble.optJSONObject("content")
                val meta = content?.optJSONObject("meta")
                val name = meta?.optString("name") ?: ""
                if (content == null || name.isEmpty()) {
                    return text(Response.Status.BAD_REQUEST, "receive bubble failed")
                }
                val rid = meta?.optString("resourceId")?.ifEmpty { id } ?: id
                store.armIncoming(rid, id, from, name, meta?.optLong("size") ?: 0L)
                // Paired senders get the official confirmReceive ack, then /file.
                if (store.isPaired(from)) {
                    val senderIp = store.deviceIp(from)
                    val senderPort = store.device(from)?.optInt("port", Wire.DEVICE_PORT)
                        ?: Wire.DEVICE_PORT
                    if (senderIp != null) {
                        Thread {
                            runCatching {
                                ChannelClient.postIntent(senderIp, senderPort,
                                    Wire.confirmReceiveIntent(ownFp, id))
                            }
                        }.start()
                    }
                }
                onEvent("message", JSONObject().put("from", from).put("id", id))
            }
        }
        return text(Response.Status.OK, "received bubble")
    }

    private fun intent(session: IHTTPSession): Response {
        val intent = readJson(session)
            ?: return text(Response.Status.INTERNAL_ERROR, "receive intent failed")
        return when (intent.optString("action")) {
            "clipboard" -> {
                val extra = intent.optJSONObject("extra")
                val inner = extra?.optString("text")
                var applied = false
                if (!inner.isNullOrEmpty()) {
                    try {
                        val payload = JSONObject(inner)
                        val manager = context.getSystemService(
                            Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
                        manager.setPrimaryClip(android.content.ClipData.newPlainText(
                            "relaydesk", payload.optString("text")))
                        applied = true
                    } catch (_: Exception) {
                        applied = false
                    }
                }
                if (applied) text(Response.Status.OK, "{0: success}")
                else text(Response.Status.INTERNAL_ERROR, "receive intent failed")
            }
            "askPairDevice" -> {
                val extra = intent.optJSONObject("extra") ?: JSONObject()
                val verify = extra.optString("verifyDevice")
                val accept = when (extra.opt("accept")) {
                    is String -> extra.optString("accept").lowercase() != "false"
                    else -> extra.optBoolean("accept", true)
                }
                if (verify.isEmpty()) text(Response.Status.OK, "{301: pair Device Null}")
                else if (verify != ownFp) text(Response.Status.OK,
                    "{302: device not match}")
                else {
                    if (accept) {
                        store.addPair(intent.optString("deviceId"), ownKey)
                        notifyPair(intent.optString("deviceId"))
                    }
                    text(Response.Status.OK, "{0: success}")
                }
            }
            "confirmPairDevice" -> {
                val extra = intent.optJSONObject("extra") ?: JSONObject()
                val who = extra.optString("verifyDevice")
                    .ifEmpty { intent.optString("deviceId") }
                val accept = when (extra.opt("accept")) {
                    is String -> extra.optString("accept").lowercase() != "false"
                    else -> extra.optBoolean("accept", true)
                }
                if (who.isEmpty()) text(Response.Status.OK, "{301: pair Device Null}")
                else {
                    if (accept) {
                        store.addPair(who, ownKey)
                        notifyPair(who)
                    }
                    text(Response.Status.OK, "{0: success}")
                }
            }
            "confirmReceive" -> text(Response.Status.OK, "{0: success}")
            else -> text(Response.Status.INTERNAL_ERROR, "receive intent failed")
        }
    }

    /** Streaming upload with the desktop security gates, landing via MediaStore. */
    private fun file(session: IHTTPSession): Response {
        val rid = session.parameters["share_id"]?.firstOrNull() ?: ""
        val supplied = session.parameters["file_name"]?.firstOrNull() ?: ""
        android.util.Log.e("RD", "file: rid=$rid supplied=$supplied remote=${session.remoteIpAddress}")
        val rec = store.incoming(rid) ?: run {
            android.util.Log.e("RD", "file: no incoming record"); return notFound()
        }
        val sender = rec.optString("sender")
        if (!store.isPaired(sender)) {
            return text(Response.Status.FORBIDDEN, "sender not paired")
        }
        val expectedIp = store.deviceIp(sender)
        val remote = session.remoteIpAddress
        if (!expectedIp.isNullOrEmpty() && !remote.isNullOrEmpty() &&
            expectedIp != remote) {
            return text(Response.Status.FORBIDDEN, "source mismatch")
        }
        if (rec.optString("path").isNotEmpty() ||
            !rec.optBoolean("waiting", true)) {
            return text(Response.Status.CONFLICT, "transfer already committed")
        }
        val declared = rec.optLong("size", -1L)
        val name = rec.optString("name")
        val cleanName = name.substringAfterLast('/').substringAfterLast('\\')
        if (cleanName.isEmpty() || cleanName == "." || cleanName == ".." ||
            cleanName != supplied.substringAfterLast('/').substringAfterLast('\\')) {
            return text(Response.Status.BAD_REQUEST, "bad file name")
        }
        if (declared < 0 || declared > Wire.MAX_RECEIVE_BYTES) {
            return text(Response.Status.PAYLOAD_TOO_LARGE, "file too large")
        }
        if (mediaNameExists(cleanName)) {
            return text(Response.Status.CONFLICT, "destination exists")
        }
        val resolver = context.contentResolver
        val values = android.content.ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, "$cleanName.part")
            put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
            put(MediaStore.Downloads.RELATIVE_PATH,
                Environment.DIRECTORY_DOWNLOADS + "/RelayDesk")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return text(Response.Status.INTERNAL_ERROR, "cannot create file")
        var got = 0L
        try {
            resolver.openOutputStream(uri)?.use { out ->
                // NO .use on the session stream: closing it closes the whole
                // socket before NanoHTTPD can deliver the response.
                val input = session.inputStream
                val buf = ByteArray(256 * 1024)
                // Read EXACTLY the declared length; a socket stream has no
                // EOF between keep-alive requests, so "read until -1" would
                // block until the socket timeout and kill the transfer.
                while (got < declared) {
                    val n = input.read(buf, 0,
                        minOf(buf.size.toLong(), declared - got).toInt())
                    if (n < 0) break
                    got += n
                    if (got > declared) {
                        throw IllegalStateException("payload exceeds declared size")
                    }
                    out.write(buf, 0, n)
                }
                out.flush()
            } ?: throw IllegalStateException("no output stream")
            if (got != declared) {
                throw IllegalStateException("size mismatch: $got != $declared")
            }
            // Commit: clear IS_PENDING and set the final name (no overwrite).
            val done = android.content.ContentValues().apply {
                put(MediaStore.Downloads.IS_PENDING, 0)
                put(MediaStore.Downloads.DISPLAY_NAME, cleanName)
            }
            resolver.update(uri, done, null, null)
            store.commitIncoming(rid, uri.toString())
            android.util.Log.e("RD", "file committed got=$got uri=$uri")
            onEvent("message", JSONObject().put("from", sender)
                .put("id", rec.optString("bubbleId")))
            notifyReceived(cleanName, got)
            return text(Response.Status.OK, "ok")
        } catch (e: Exception) {
            android.util.Log.e("RD", "file FAILED got=$got", e)
            runCatching { resolver.delete(uri, null, null) }
            return text(Response.Status.BAD_REQUEST, e.message ?: "transfer failed")
        }
    }

    private fun mediaNameExists(name: String): Boolean {
        val resolver = context.contentResolver
        resolver.query(
            MediaStore.Downloads.EXTERNAL_CONTENT_URI,
            arrayOf(MediaStore.Downloads._ID),
            "${MediaStore.Downloads.RELATIVE_PATH}=? AND ${MediaStore.Downloads.DISPLAY_NAME}=?",
            arrayOf(Environment.DIRECTORY_DOWNLOADS + "/RelayDesk", name),
            null)?.use { return it.count > 0 }
        return false
    }

    private fun notifyPair(peer: String) {
        val alias = store.device(peer)?.optString("alias") ?: peer.take(8)
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            val pm = context.getSystemService(NotificationManager::class.java)
            if (pm.areNotificationsEnabled()) {
                ensureChannel(pm)
                val notification = android.app.Notification.Builder(context, "relaydesk")
                    .setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("设备已配对")
                    .setContentText(alias)
                    .build()
                pm.notify(2, notification)
            }
        }
    }

    private fun notifyReceived(name: String, size: Long) {
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            val pm = context.getSystemService(NotificationManager::class.java)
            if (!pm.areNotificationsEnabled()) return
            ensureChannel(pm)
            val intent = Intent(context, MainActivity::class.java)
            val pending = PendingIntent.getActivity(context, 0, intent,
                PendingIntent.FLAG_IMMUTABLE)
            val notification = android.app.Notification.Builder(context, "relaydesk")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("收到文件")
                .setContentText("$name (${size} B)")
                .setContentIntent(pending)
                .build()
            pm.notify(1, notification)
        }
    }

    private fun notifyText(peer: String, content: String) {
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            val pm = context.getSystemService(NotificationManager::class.java)
            if (!pm.areNotificationsEnabled()) return
        }
        ensureChannel(pm())
        val alias = store.device(peer)?.optString("alias") ?: peer.take(8)
        val intent = Intent(context, MainActivity::class.java)
        val pending = PendingIntent.getActivity(context, 0, intent,
            PendingIntent.FLAG_IMMUTABLE)
        val preview = content.take(60)
        val notification = android.app.Notification.Builder(context, "relaydesk")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(alias)
            .setContentText(preview)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        pm().notify(3, notification)
    }

    private fun pm(): NotificationManager =
        context.getSystemService(NotificationManager::class.java)

    private fun ensureChannel(pm: NotificationManager) {
        pm.createNotificationChannel(NotificationChannel(
            "relaydesk", "RelayDesk", NotificationManager.IMPORTANCE_DEFAULT))
    }
}
