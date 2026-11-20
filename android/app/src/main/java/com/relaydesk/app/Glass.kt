package com.relaydesk.app

import android.app.Activity
import android.app.WallpaperManager
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RenderEffect
import android.graphics.Shader
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.widget.FrameLayout
import android.widget.ImageView

/**
 * 手机端玻璃底：把系统壁纸模糊成一层底，界面用半透明色叠在上面。
 *
 * 桌面端是自绘模糊桌面截图，手机端没有“窗口后面”的概念，最接近的做法就是
 * 壁纸 + 模糊 + 半透明面板。API 31+ 用 RenderEffect 做实时模糊；29/30 用
 * 缩小再放大的一次性位图，效果差一点但同样能看出材质感。
 */
object Glass {

    fun install(activity: Activity, root: FrameLayout) {
        val wallpaper: Drawable? = runCatching {
            WallpaperManager.getInstance(activity).drawable
        }.getOrNull()

        val backdrop = ImageView(activity).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)
            // 深色薄纱：浅色壁纸下正文仍要过 WCAG，纯模糊会把对比度拉太低。
            setColorFilter(Color.argb(56, 0, 0, 0))
        }
        if (wallpaper != null) {
            if (Build.VERSION.SDK_INT >= 31) {
                backdrop.setImageDrawable(wallpaper)
                backdrop.setRenderEffect(
                    RenderEffect.createBlurEffect(40f, 40f, Shader.TileMode.CLAMP)
                )
            } else {
                backdrop.setImageBitmap(blurLegacy(activity, wallpaper))
            }
        }
        root.addView(backdrop, 0)
        activity.window?.setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
    }

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
