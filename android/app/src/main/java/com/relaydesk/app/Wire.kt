package com.relaydesk.app

import org.json.JSONObject
import org.json.JSONTokener
import java.util.UUID

/**
 * Wire formats ported 1:1 from flixcli (src/flixcli/model.py, discovery.py).
 * Any deviation breaks interop with the Windows end and official clients.
 */
object Wire {
    const val MULTICAST_GROUP = "224.0.0.168"
    const val DEVICE_PORT = 8891
    const val ANNOUNCE_BURST = 11
    const val TYPE_TEXT = 0
    const val TYPE_FILE = 3
    const val MAX_RECEIVE_BYTES = 4L * 1024 * 1024 * 1024
    const val APP_VERSION = 1

    fun newId(): String = UUID.randomUUID().toString()

    fun deviceModal(fingerprint: String, alias: String, port: Int, from: String = ""): JSONObject =
        JSONObject().apply {
            put("alias", alias)
            put("deviceModel", "Android")
            put("deviceType", "tablet")
            put("fingerprint", fingerprint)
            put("port", port)
            put("version", APP_VERSION)
            put("ip", "")
            put("host", "")
            put("from", from)
            put("os", "android")
        }

    /** json.dumps(json.dumps(x)) equivalent: a JSON string containing JSON. */
    fun doubleEncode(o: JSONObject): String = JSONObject.quote(o.toString())

    /** Inverse of doubleEncode; also accepts plain JSON objects. */
    fun doubleDecode(text: String): JSONObject {
        val once = JSONTokener(text).nextValue()
        return when (once) {
            is String -> JSONObject(once)
            is JSONObject -> once
            else -> throw IllegalArgumentException("unexpected announce shape")
        }
    }

    fun buildAnnounce(fingerprint: String, alias: String, port: Int): String =
        doubleEncode(JSONObject().put("deviceModal", deviceModal(fingerprint, alias, port)))

    /** Receiver takes the peer IP from the datagram source; ip/host stay empty on the wire. */
    fun parseAnnounce(data: ByteArray, srcIp: String): JSONObject? {
        return try {
            val outer = doubleDecode(String(data, Charsets.UTF_8))
            val modal = outer.optJSONObject("deviceModal")
            if (modal == null || modal.optString("fingerprint").isEmpty()) {
                null
            } else {
                if (modal.optString("ip").isEmpty()) modal.put("ip", srcIp)
                modal.put("source", srcIp)
                modal
            }
        } catch (_: Exception) {
            null
        }
    }

    fun textBubble(from: String, to: String, content: String, groupId: String = ""): JSONObject =
        JSONObject().apply {
            put("id", newId()); put("from", from); put("to", to)
            put("type", TYPE_TEXT); put("content", content)
            put("time", System.currentTimeMillis()); put("groupId", groupId)
        }

    fun fileBubble(from: String, to: String, resourceId: String, name: String,
                   mime: String, size: Long, groupId: String = ""): JSONObject =
        JSONObject().apply {
            put("id", resourceId); put("from", from); put("to", to)
            put("type", TYPE_FILE)
            put("content", JSONObject().apply {
                put("state", 2); put("progress", 0.0); put("speed", 0); put("receiveBytes", 0)
                put("meta", JSONObject().apply {
                    put("name", name); put("mimeType", mime); put("nameWithSuffix", name)
                    put("androidContentUri", ""); put("size", size)
                    put("width", 0); put("height", 0); put("resourceId", resourceId)
                })
                put("waitingForAccept", true)
            })
            put("time", System.currentTimeMillis()); put("groupId", groupId)
        }

    fun pairIntent(myFp: String, peerFp: String, code: String,
                   action: String = "askPairDevice", accept: Boolean = true): JSONObject =
        JSONObject().apply {
            put("deviceId", myFp); put("bubbleId", ""); put("action", action)
            put("extra", JSONObject().apply {
                put("verifyDevice", peerFp); put("verifyCode", code)
                if (action == "confirmPairDevice") put("accept", accept)
            })
        }

    fun confirmReceiveIntent(myFp: String, bubbleId: String): JSONObject =
        JSONObject().apply {
            put("deviceId", myFp); put("bubbleId", bubbleId)
            put("action", "confirmReceive"); put("extra", JSONObject())
        }

    /** Official /intent replies are Dart map toString: `{0: success}` / `{301: ...}`. */
    fun parseIntentReply(text: String): Pair<Int, String> {
        val t = text.trim()
        if (t.startsWith("{") && t.endsWith("}")) {
            val body = t.substring(1, t.length - 1)
            val idx = body.indexOf(": ")
            return if (idx > 0) {
                val code = body.substring(0, idx).trim().toIntOrNull() ?: -1
                code to body.substring(idx + 2).trim()
            } else -1 to t
        }
        return -1 to t
    }
}
