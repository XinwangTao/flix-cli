package com.relaydesk.app

import android.app.Activity
import android.app.WallpaperManager
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.RenderEffect
import android.graphics.Shader
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.util.Log
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.widget.FrameLayout
import android.widget.ImageView

/**
 * 手机端玻璃底。
 *
 * 优先用系统壁纸做模糊底；但不少国产 ROM 直接不给第三方应用壁纸（getDrawable 与
 * peekDrawable 都返回 null，给了读图权限也一样），所以必须有一条不依赖壁纸的
 * 兜底：自绘一层柔和的极光渐变（主题色系的几团大半径径向渐变）再高斯模糊，
 * 叠上半透明面板后与桌面端自绘模糊背景是同一个观感，而不是死黑屏。
 */
object Glass {

    private const val TAG = "RelayDeskGlass"

    fun install(activity: Activity, root: FrameLayout) {
        val backdrop = ImageView(activity).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)
            // 薄纱：浅色壁纸下正文也要过 WCAG，纯模糊会把对比度拉太低。
            setColorFilter(Color.argb(48, 0, 0, 0))
        }

        val wallpaper = loadWallpaper(activity)
        Log.i(TAG, "wallpaper=" + (wallpaper != null))
        if (wallpaper != null) {
            if (Build.VERSION.SDK_INT >= 31) {
                backdrop.setImageDrawable(wallpaper)
                backdrop.setRenderEffect(
                    RenderEffect.createBlurEffect(40f, 40f, Shader.TileMode.CLAMP)
                )
            } else {
                backdrop.setImageBitmap(blurLegacy(activity, wallpaper))
            }
        } else {
            val bitmap = aurora(activity)
            backdrop.setImageBitmap(bitmap)
            if (Build.VERSION.SDK_INT >= 31) {
                backdrop.setRenderEffect(
                    RenderEffect.createBlurEffect(60f, 60f, Shader.TileMode.CLAMP)
                )
            }
        }

        root.addView(backdrop, 0)
        activity.window?.setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
    }

    /** ROM 不给壁纸时返回 null，由调用方走极光兜底。 */
    private fun loadWallpaper(activity: Activity): Drawable? {
        val manager = runCatching { WallpaperManager.getInstance(activity) }.getOrNull()
            ?: return null
        return runCatching { manager.drawable }.getOrNull()
            ?: runCatching { manager.peekDrawable() }.getOrNull()
    }

    /**
     * 自绘极光底：深色时是墨绿基底 + 玉色/琥珀/靛蓝几团光斑，浅色时是米白基底。
     * 画在 1/8 尺寸再放大，模糊后看不出色带，成本只有几毫秒。
     */
    private fun aurora(activity: Activity): Bitmap {
        val metrics = activity.resources.displayMetrics
        val width = metrics.widthPixels.coerceAtLeast(1)
        val height = metrics.heightPixels.coerceAtLeast(1)
        val scale = 8
        val small = Bitmap.createBitmap(
            (width / scale).coerceAtLeast(32),
            (height / scale).coerceAtLeast(32),
            Bitmap.Config.ARGB_8888
        )
        val canvas = Canvas(small)
        val light = isLightTheme(activity)
        canvas.drawColor((activity.getColor(R.color.bg) or 0xFF000000.toInt()))

        val w = small.width.toFloat()
        val h = small.height.toFloat()
        val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        data class Blob(val x: Float, val y: Float, val r: Float, val color: Int)
        val blobs = if (light) listOf(
            Blob(0.14f, 0.12f, 0.72f, 0x6612795B),
            Blob(0.92f, 0.30f, 0.66f, 0x55A96300),
            Blob(0.68f, 0.92f, 0.80f, 0x4D2F6F86),
        ) else listOf(
            Blob(0.12f, 0.10f, 0.78f, 0x552FB98D),
            Blob(0.92f, 0.26f, 0.62f, 0x44FFC857),
            Blob(0.72f, 0.94f, 0.86f, 0x443E6BD8),
            Blob(0.30f, 0.62f, 0.55f, 0x330F8F6B),
        )
        for (blob in blobs) {
            val cx = blob.x * w
            val cy = blob.y * h
            val radius = blob.r * maxOf(w, h)
            paint.shader = RadialGradient(
                cx, cy, radius, intArrayOf(blob.color, blob.color and 0x00FFFFFF), null,
                Shader.TileMode.CLAMP
            )
            canvas.drawCircle(cx, cy, radius, paint)
        }
        paint.shader = null

        val out = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        Canvas(out).drawBitmap(
            small, null, android.graphics.Rect(0, 0, width, height),
            Paint(Paint.FILTER_BITMAP_FLAG)
        )
        return out
    }

    private fun isLightTheme(activity: Activity): Boolean =
        (activity.resources.configuration.uiMode and
            android.content.res.Configuration.UI_MODE_NIGHT_MASK) ==
            android.content.res.Configuration.UI_MODE_NIGHT_NO

    /** API 29/30 没有 RenderEffect：缩到 1/16 再放大，视觉上就是模糊。 */
    private fun blurLegacy(activity: Activity, drawable: Drawable): Bitmap {
        val metrics = activity.resources.displayMetrics
        val width = metrics.widthPixels.coerceAtLeast(1)
        val height = metrics.heightPixels.coerceAtLeast(1)
        val small = Bitmap.createBitmap(
            (width / 16).coerceAtLeast(1), (height / 16).coerceAtLeast(1),
            Bitmap.Config.ARGB_8888
        )
        val canvas = Canvas(small)
        drawable.setBounds(0, 0, canvas.width, canvas.height)
        drawable.draw(canvas)
        val out = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        Canvas(out).drawBitmap(small, null, android.graphics.Rect(0, 0, width, height),
            Paint(Paint.FILTER_BITMAP_FLAG))
        return out
    }
}
