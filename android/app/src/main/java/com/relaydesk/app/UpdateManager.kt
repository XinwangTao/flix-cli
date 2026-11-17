package com.relaydesk.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageInstaller
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.provider.Settings
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.Executors

/**
 * Android auto-update: check the repository's latest stable GitHub Release,
 * download the APK on Wi-Fi, verify sha256, then hand it to the system
 * installer. Android never allows a silent install for a sideloaded app: the
 * user confirms in the system dialog and data is preserved by the same
 * signature.
 *
 * Checks run on boot (BootReceiver -> RelayService) and on every app launch,
 * throttled to stay far below GitHub's anonymous API limit.
 */
object UpdateManager {
    const val PREFS = "relaydesk"
    private const val KEY_LAST_CHECK = "update_last_check"
    private const val KEY_PROMPTED = "update_prompted_tag"
    private const val KEY_DOWNLOADED = "update_downloaded_tag"
    private const val KEY_APK_PATH = "update_apk_path"
    private const val CH_UPDATES = "relaydesk_updates"
    private const val NOTIF_AVAILABLE = 2001
    private const val NOTIF_DOWNLOADED = 2002
    private const val API =
        "https://api.github.com/repos/XinwangTao/flix-cli/releases/latest"
    private const val THROTTLE_MS = 6 * 3600_000L
    private const val MAX_APK_BYTES = 200L * 1024 * 1024

    private val exec = Executors.newSingleThreadExecutor { r ->
        Thread(r, "relaydesk-update").apply { isDaemon = true }
    }

    fun maybeCheck(context: Context, force: Boolean = false) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (!force && now - prefs.getLong(KEY_LAST_CHECK, 0L) < THROTTLE_MS) return
        prefs.edit().putLong(KEY_LAST_CHECK, now).apply()
        exec.execute { runCatching { check(context, prefs) } }
    }

    /** Downloaded, sha256-verified APK whose tag is newer than this build. */
    fun downloadedTag(context: Context): String? {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val tag = prefs.getString(KEY_DOWNLOADED, null) ?: return null
        val path = prefs.getString(KEY_APK_PATH, null) ?: return null
        if (!File(path).exists()) return null
        return if (isNewer(tag, BuildConfig.VERSION_NAME)) tag else null
    }

    private fun check(context: Context, prefs: SharedPreferences) {
        val release = httpJson(API) ?: return
        if (release.optBoolean("draft") || release.optBoolean("prerelease")) return
        val tag = release.optString("tag_name").removePrefix("v")
        if (tag.isEmpty() || !isNewer(tag, BuildConfig.VERSION_NAME)) return
        if (prefs.getString(KEY_DOWNLOADED, "") == tag) return
        if (prefs.getString(KEY_PROMPTED, "") == tag) return
        val asset = pickApk(release) ?: return
        val sha = assetSha(release, asset) ?: return
        prefs.edit().putString(KEY_PROMPTED, tag).apply()
        val wifi = isWifi(context)
        notify(context, NOTIF_AVAILABLE, "发现新版本 v$tag",
            if (wifi) "正在 Wi-Fi 下下载…" else "连接 Wi-Fi 后自动下载", install = false)
        if (!wifi) return
        if (download(context, prefs, tag, asset, sha)) {
            prefs.edit().putString(KEY_DOWNLOADED, tag).apply()
            notify(context, NOTIF_DOWNLOADED, "新版本 v$tag 已下载",
                "点按进入系统安装", install = true)
        }
    }

    private fun pickApk(release: JSONObject): JSONObject? {
        val assets = release.optJSONArray("assets") ?: return null
        for (i in 0 until assets.length()) {
            val asset = assets.optJSONObject(i) ?: continue
            if (asset.optString("name").endsWith("-android.apk")) return asset
        }
        return null
    }

    private fun assetSha(release: JSONObject, asset: JSONObject): String? {
        val digest = asset.optString("digest")
        if (digest.startsWith("sha256:")) return digest.substringAfter(':')
        val name = asset.optString("name")
        val assets = release.optJSONArray("assets") ?: return null
        for (i in 0 until assets.length()) {
            val a = assets.optJSONObject(i) ?: continue
            if (a.optString("name") == "$name.sha256") {
                val text = httpText(a.optString("browser_download_url")) ?: return null
                return text.trim().substringBefore(' ').ifEmpty { null }
            }
        }
        return null
    }

    private fun download(context: Context, prefs: SharedPreferences, tag: String,
                         asset: JSONObject, sha: String): Boolean {
        val size = asset.optLong("size", -1L)
        if (size <= 0 || size > MAX_APK_BYTES) return false
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val apk = File(dir, "RelayDesk-$tag.apk")
        return runCatching {
            val conn = (URL(asset.optString("browser_download_url"))
                .openConnection() as HttpURLConnection).apply {
                connectTimeout = 15_000
                readTimeout = 60_000
                instanceFollowRedirects = true
                setRequestProperty("User-Agent", "RelayDesk/${BuildConfig.VERSION_NAME}")
            }
            conn.inputStream.use { input -> apk.outputStream().use { input.copyTo(it) } }
            conn.disconnect()
            if (sha256(apk) != sha.lowercase()) {
                apk.delete()
                false
            } else {
                prefs.edit().putString(KEY_APK_PATH, apk.absolutePath).apply()
                true
            }
        }.getOrDefault(false)
    }

    fun installDownloaded(context: Context) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val path = prefs.getString(KEY_APK_PATH, null) ?: return
        val apk = File(path)
        if (!apk.exists()) return
        if (!context.packageManager.canRequestPackageInstalls()) {
            context.startActivity(Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${context.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        }
        runCatching {
            val installer = context.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
                setAppPackageName(context.packageName)
            }
            val id = installer.createSession(params)
            installer.openSession(id).use { session ->
                session.openWrite("base.apk", 0, apk.length()).use { out ->
                    apk.inputStream().use { it.copyTo(out) }
                    session.fsync(out)
                }
                val intent = Intent(context, InstallResultReceiver::class.java)
                    .setPackage(context.packageName)
                val pending = PendingIntent.getBroadcast(context, id, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
                session.commit(pending.intentSender)
            }
        }
    }

    private fun notify(context: Context, id: Int, title: String, body: String,
                       install: Boolean) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(
            CH_UPDATES, "应用更新", NotificationManager.IMPORTANCE_DEFAULT))
        val intent = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            if (install) putExtra("install_update", true)
            else putExtra("check_update", true)
        }
        val pending = PendingIntent.getActivity(context, id, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        manager.notify(id, Notification.Builder(context, CH_UPDATES)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title).setContentText(body)
            .setContentIntent(pending).setAutoCancel(true)
            .build())
    }

    private fun httpJson(url: String): JSONObject? =
        httpText(url)?.let { runCatching { JSONObject(it) }.getOrNull() }

    private fun httpText(url: String): String? = runCatching {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = 10_000
            readTimeout = 15_000
            instanceFollowRedirects = true
            setRequestProperty("User-Agent", "RelayDesk/${BuildConfig.VERSION_NAME}")
            setRequestProperty("Accept", "application/vnd.github+json")
        }
        try {
            if (conn.responseCode != 200) null
            else conn.inputStream.bufferedReader().use { it.readText() }
        } finally {
            conn.disconnect()
        }
    }.getOrNull()

    private fun isWifi(context: Context): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    }

    private fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(256 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun isNewer(remote: String, local: String): Boolean {
        val r = parts(remote)
        val l = parts(local)
        for (i in 0 until maxOf(r.size, l.size)) {
            val a = r.getOrElse(i) { 0 }
            val b = l.getOrElse(i) { 0 }
            if (a != b) return a > b
        }
        return false
    }

    private fun parts(v: String): List<Int> =
        v.substringBefore('-').split('.').map { it.toIntOrNull() ?: 0 }
}
