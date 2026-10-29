package com.relaydesk.app

import android.content.ContentValues
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream

/**
 * Image and file plumbing for v2E: compress before sending, keep a thumbnail
 * next to the cache copy, and put things the user asks for into
 * Download/RelayDesk. Nothing here touches the messages table.
 */
object Media {
    private val IMAGE_EXT = setOf("jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "heif")
    private const val MAX_SEND_BYTES = 20L * 1024 * 1024
    private const val MAX_SIDE = 1920
    private const val THUMB_SIDE = 480
    private const val JPEG_QUALITY = 85

    /** Extension-based: an image sent from the gallery keeps its name. */
    fun isImage(name: String): Boolean =
        name.substringAfterLast('.', "").lowercase() in IMAGE_EXT

    /** Cache copy plus thumbnail live here; cleared by the system at will. */
    fun cacheDir(context: Context): File = File(context.cacheDir, "out").apply { mkdirs() }

    fun open(context: Context, source: String): InputStream? {
        if (source.isEmpty()) return null
        return context.contentResolver.openInputStream(
            if (source.contains("://")) Uri.parse(source) else Uri.fromFile(File(source)))
    }

    /** Decoded and scaled to [maxPx] on the long side; null when unreadable. */
    fun loadBitmap(context: Context, source: String, maxPx: Int): Bitmap? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        open(context, source)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        android.util.Log.e("RD", "decode src=$source bounds=${bounds.outWidth}x${bounds.outHeight}")
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= maxPx) sample *= 2
        val options = BitmapFactory.Options().apply { inSampleSize = sample }
        val bitmap = open(context, source)?.use {
            BitmapFactory.decodeStream(it, null, options)
        } ?: return null
        val side = maxOf(bitmap.width, bitmap.height)
        if (side <= maxPx) {
            bitmap
        } else {
            val ratio = maxPx.toFloat() / side
            val scaled = Bitmap.createScaledBitmap(bitmap,
                (bitmap.width * ratio).toInt().coerceAtLeast(1),
                (bitmap.height * ratio).toInt().coerceAtLeast(1), true)
            if (scaled != bitmap) bitmap.recycle()
            scaled
        }
    }.onFailure { android.util.Log.e("RD", "decode failed: $source", it) }.getOrNull()

    private fun writeJpeg(bitmap: Bitmap, file: File) {
        FileOutputStream(file).use { bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, it) }
    }

    /** Result of preparing an outgoing image. */
    class Prepared(val file: File, val name: String)

    /**
     * Compress to JPEG and leave a thumbnail beside it. Null means "not worth
     * compressing" (too big, undecodable) — the caller then sends the original.
     */
    fun prepareImage(context: Context, uri: Uri, id: String, name: String,
                     declaredSize: Long): Prepared? = runCatching {
        if (declaredSize > MAX_SEND_BYTES) return null
        val full = loadBitmap(context, uri.toString(), MAX_SIDE) ?: return null
        val file = File(cacheDir(context), "$id.jpg")
        writeJpeg(full, file)
        full.recycle()
        // A tiny PNG can grow as JPEG: keep the original when that happens.
        if (declaredSize in 1 until file.length()) {
            file.delete()
            return null
        }
        val thumb = loadBitmap(context, uri.toString(), THUMB_SIDE)
        if (thumb != null) {
            writeJpeg(thumb, File(cacheDir(context), "$id.thumb.jpg"))
            thumb.recycle()
        }
        Prepared(file, name.substringBeforeLast('.', name) + ".jpg")
    }.getOrNull()

    /** Stable local copy so a retry does not depend on the picker's grant. */
    fun copyToCache(context: Context, uri: Uri, id: String, name: String): File {
        val clean = name.substringAfterLast('/').substringAfterLast('\\')
            .ifEmpty { "file" }
        val file = File(cacheDir(context), "$id-$clean")
        open(context, uri.toString())?.use { input ->
            FileOutputStream(file).use { input.copyTo(it) }
        }
        return file
    }

    /** Bubble thumbnail: our cached one first, then whatever the message holds. */
    fun thumbSource(context: Context, msg: JSONObject): String? {
        val cached = File(cacheDir(context), "${msg.optString("id")}.thumb.jpg")
        if (cached.exists()) return cached.absolutePath
        return msg.optString("filePath").ifEmpty { null }
    }

    /** Copy into Download/RelayDesk; returns the saved display name. */
    fun saveToDownloads(context: Context, source: String, name: String): String? = runCatching {
        val resolver = context.contentResolver
        val clean = name.substringAfterLast('/').substringAfterLast('\\').ifEmpty { "file" }
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, clean)
            put(MediaStore.Downloads.MIME_TYPE,
                resolver.getType(Uri.parse(source)) ?: "application/octet-stream")
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/RelayDesk")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val target = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return null
        try {
            open(context, source)?.use { input ->
                resolver.openOutputStream(target)?.use { out -> input.copyTo(out) }
            } ?: throw IllegalStateException("source unreadable")
            resolver.update(target, ContentValues().apply {
                put(MediaStore.Downloads.IS_PENDING, 0)
            }, null, null)
            clean
        } catch (e: Exception) {
            runCatching { resolver.delete(target, null, null) }
            throw e
        }
    }.getOrNull()
}
