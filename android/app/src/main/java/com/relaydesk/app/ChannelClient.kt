package com.relaydesk.app

import org.json.JSONObject
import java.io.IOException
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/** HTTP client side of the device channel (flixcli channel.py semantics). */
object ChannelClient {

    class PeerError(val kind: String, detail: String) : IOException("$kind: $detail")

    private fun open(url: String, timeoutMs: Int): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            requestMethod = "POST"
            doOutput = true
        }

    fun postBubble(ip: String, port: Int, bubble: JSONObject,
                   timeoutMs: Int = 8000): String {
        val conn = open("http://$ip:$port/bubble", timeoutMs)
        try {
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(bubble.toString().toByteArray(Charsets.UTF_8)) }
            val text = conn.inputStream.bufferedReader().readText().trim()
            if (conn.responseCode != 200) throw PeerError("rejected", "HTTP ${conn.responseCode} $text")
            return text
        } catch (e: PeerError) {
            throw e
        } catch (e: java.net.SocketTimeoutException) {
            throw PeerError("timeout", "bubble POST timed out")
        } finally {
            conn.disconnect()
        }
    }

    fun pingV2(ip: String, port: Int, myFp: String, alias: String, myPort: Int,
               timeoutMs: Int = 6000): JSONObject {
        val conn = open("http://$ip:$port/ping_v2", timeoutMs)
        try {
            conn.setRequestProperty("Content-Type", "application/json; charset=UTF-8")
            val body = JSONObject().put(
                "deviceModal", Wire.deviceModal(myFp, alias, myPort, from = "manual"))
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val text = (if (code == 200) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.readText()?.trim() ?: ""
            if (code == 400) throw PeerError("bad_request", text)
            if (code != 200) throw PeerError("rejected", "HTTP $code $text")
            return Wire.doubleDecode(text)
        } finally {
            conn.disconnect()
        }
    }

    /** Returns (code, message); 0 = success, per the Dart map toString replies. */
    fun postIntent(ip: String, port: Int, intent: JSONObject,
                   timeoutMs: Int = 8000): Pair<Int, String> {
        val conn = open("http://$ip:$port/intent", timeoutMs)
        try {
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(intent.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val text = (if (code == 200) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.readText() ?: ""
            if (code != 200) throw PeerError("rejected", "HTTP $code ${text.trim()}")
            return Wire.parseIntentReply(text)
        } finally {
            conn.disconnect()
        }
    }

    fun uploadFile(ip: String, port: Int, resourceId: String, name: String,
                   size: Long, content: java.io.InputStream,
                   timeoutMs: Int = 600_000,
                   onProgress: ((Long) -> Unit)? = null) {
        val conn = open(
            "http://$ip:$port/file?share_id=$resourceId&file_name=" +
            java.net.URLEncoder.encode(name, "UTF-8"), timeoutMs)
        try {
            conn.setRequestProperty("Content-Type", "application/octet-stream")
            conn.setFixedLengthStreamingMode(size)
            content.use { input ->
                val out: OutputStream = conn.outputStream
                val buf = ByteArray(256 * 1024)
                var sent = 0L
                var lastTick = 0L
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    sent += n
                    if (onProgress != null && (sent - lastTick >= 1_048_576 || sent == size)) {
                        lastTick = sent
                        onProgress(sent)
                    }
                }
                out.flush()
            }
            val code = conn.responseCode
            val text = (if (code == 200) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.readText()?.trim() ?: ""
            if (code != 200) throw PeerError("upload-failed", "HTTP $code $text")
        } finally {
            conn.disconnect()
        }
    }
}
