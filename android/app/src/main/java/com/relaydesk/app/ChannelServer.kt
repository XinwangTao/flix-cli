package com.relaydesk.app

import android.content.Context
import android.os.Environment
import android.provider.MediaStore
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject

/**
 * Device channel server on the first free port (8891, then 18991) mirroring
 * flixcli server.py semantics and the confirmed transfer-security gates:
 * paired senders only, source-IP binding, declared-size limits, no overwrite,
 * atomic (IS_PENDING) commit. Notifications and unread state live in
 * RelayService; this class only reports events.
 */
class ChannelServer(
    private val context: Context,
    private val store: Store,
    private val ownFp: String,
    private val alias: String,
    private val port: Int,
    private val onEvent: (String, JSONObject) -> Unit,
    private val onProgress: (JSONObject) -> Unit,
    private val confirmPair: (String, String) -> Boolean,
) : NanoHTTPD(port) {

    private val ownKey: String
        get() = store.ownPairKey(
            context.getSharedPreferences("relaydesk", Context.MODE_PRIVATE))

    private val UUID_RE = Regex(
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

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
        if (groupId.isNotEmpty() && store.isGroupLeft(groupId)) {
            // Removed from this group: history stays read-only, new traffic is dropped
            // (still 200 so the sender does not keep retrying).
            return text(Response.Status.OK, "received bubble")
        }
        if (groupId.isNotEmpty() && store.group(groupId) == null) {
            // Peers may know a group we do not; keep the bubble, label it clearly.
            store.upsertGroup(groupId, "未知群")
        }
        when (type) {
            Wire.TYPE_TEXT -> {
                val content = bubble.optString("content")
                store.addMessage(id, from, bubble.optString("to"), type,
                    bubble.optLong("time"), groupId, text = content)
                onEvent("message", JSONObject().put("from", from).put("id", id)
                    .put("groupId", groupId)
                    .put("kind", "text").put("preview", content.take(80)))
            }
            Wire.TYPE_FILE -> {
                val content = bubble.optJSONObject("content")
                val meta = content?.optJSONObject("meta")
                val name = meta?.optString("name") ?: ""
                if (content == null || name.isEmpty()) {
                    return text(Response.Status.BAD_REQUEST, "receive bubble failed")
                }
                val rid = meta?.optString("resourceId")?.ifEmpty { id } ?: id
                store.armIncoming(rid, id, from, name, meta?.optLong("size") ?: 0L,
                    groupId)
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
                onEvent("filePending", JSONObject().put("from", from).put("id", id)
                    .put("groupId", groupId)
                    .put("rid", rid).put("name", name)
                    .put("size", meta?.optLong("size") ?: 0L))
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
                val peer = intent.optString("deviceId")
                val code = extra.optString("verifyCode")
                if (verify.isEmpty()) text(Response.Status.OK, "{301: pair Device Null}")
                else if (verify != ownFp) text(Response.Status.OK,
                    "{302: device not match}")
                else if (!accept || !confirmPair(peer, code)) {
                    // No visible Activity (or the user declined): default reject.
                    text(Response.Status.OK, "{301: pair Device Null}")
                } else {
                    store.addPair(peer, ownKey)
                    onEvent("message", JSONObject().put("from", peer)
                        .put("kind", "pair").put("preview", "设备已配对"))
                    confirmPeer(peer, ownKey)
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
                        onEvent("message", JSONObject().put("from", who)
                            .put("kind", "pair").put("preview", "设备已配对"))
                    }
                    text(Response.Status.OK, "{0: success}")
                }
            }
            "confirmReceive" -> text(Response.Status.OK, "{0: success}")
            Wire.ACTION_GROUP_SYNC -> groupSync(intent)
            else -> text(Response.Status.INTERNAL_ERROR, "receive intent failed")
        }
    }

    /**
     * Group metadata snapshot (v2C). Trust gate: the sender must be paired or
     * already a member; anything else is dropped without touching the store.
     */
    private fun groupSync(intent: JSONObject): Response {
        val extra = intent.optJSONObject("extra")
            ?: return text(Response.Status.INTERNAL_ERROR, "receive intent failed")
        val gid = extra.optString("groupId")
        val name = extra.optString("name").trim()
        val rev = extra.optLong("rev", 0L)
        val sender = intent.optString("deviceId").ifEmpty { extra.optString("from") }
        val raw = extra.optJSONArray("members")
        val bad = text(Response.Status.BAD_REQUEST, "receive intent failed")
        if (!UUID_RE.matches(gid) || name.isEmpty() || rev <= 0 ||
            sender.isEmpty() || raw == null) return bad
        val members = (0 until raw.length()).mapNotNull { i ->
            raw.optString(i).takeIf { it.isNotEmpty() }
        }.distinct()
        if (members.isEmpty()) return bad
        if (!store.isPaired(sender) && !store.groupMembers(gid).contains(sender)) return bad
        if (store.applyGroupSnapshot(gid, name, members, rev)) {
            // The snapshot itself tells us we were removed: history stays, sending stops.
            if (!members.contains(ownFp)) store.markGroupLeft(gid, rev)
            onEvent("groupSync", JSONObject().put("groupId", gid))
        }
        return text(Response.Status.OK, "{0: success}")
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
        var lastTick = 0L
        try {
            onProgress(JSONObject().put("from", sender).put("rid", rid)
                .put("name", cleanName).put("got", 0L)
                .put("total", declared).put("done", false))
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
                    if (got - lastTick >= 1_048_576 || got == declared) {
                        lastTick = got
                        onProgress(JSONObject().put("from", sender).put("rid", rid)
                            .put("name", cleanName).put("got", got)
                            .put("total", declared).put("done", false))
                    }
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
            store.addMessage(rec.optString("bubbleId"), sender, ownFp, Wire.TYPE_FILE,
                System.currentTimeMillis(), rec.optString("groupId"),
                fileName = cleanName, fileSize = got, filePath = uri.toString())
            android.util.Log.e("RD", "file committed got=$got uri=$uri")
            onProgress(JSONObject().put("from", sender).put("rid", rid)
                .put("name", cleanName).put("got", got)
                .put("total", declared).put("done", true))
            onEvent("message", JSONObject().put("from", sender)
                .put("id", rec.optString("bubbleId")).put("groupId", rec.optString("groupId"))
                .put("kind", "file").put("preview", "收到文件 $cleanName"))
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

    /** Official flow: the accepting side replies confirmPairDevice to the asker. */
    private fun confirmPeer(peerFp: String, code: String) {
        val row = store.device(peerFp) ?: return
        val ip = row.optString("ip")
        val peerPort = row.optInt("port", 0).takeIf { it > 0 } ?: Wire.DEVICE_PORT
        if (ip.isEmpty()) return
        Thread {
            runCatching {
                ChannelClient.postIntent(ip, peerPort,
                    Wire.pairIntent(ownFp, peerFp, code,
                        action = "confirmPairDevice", accept = true))
            }
        }.start()
    }
}
