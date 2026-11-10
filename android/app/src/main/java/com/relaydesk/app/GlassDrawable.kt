package com.relaydesk.app

import android.graphics.Canvas
import android.graphics.ColorFilter
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.drawable.Drawable

/**
 * A "liquid glass" surface: translucent gradient fill, a specular top band and
 * a hairline border that fades from bright at the top to almost invisible at
 * the bottom. Drawn by hand because GradientDrawable only supports one solid
 * stroke, which loses the light-catching rim that makes glass read as glass.
 */
class GlassDrawable(
    private val radius: Float,
    private val fillTop: Int,
    private val fillBottom: Int,
    private val borderTop: Int,
    private val borderBottom: Int,
    private val highlight: Int,
    private val strokeWidth: Float,
) : Drawable() {

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val borderPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        this.strokeWidth = this@GlassDrawable.strokeWidth
    }
    private val highlightPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    override fun onBoundsChange(bounds: Rect) {
        val r = RectF(bounds)
        if (r.width() <= 0f || r.height() <= 0f) return
        fillPaint.shader = LinearGradient(
            r.left, r.top, r.right, r.bottom, fillTop, fillBottom, Shader.TileMode.CLAMP)
        borderPaint.shader = LinearGradient(
            r.left, r.top, r.right, r.bottom, borderTop, borderBottom, Shader.TileMode.CLAMP)
        highlightPaint.shader = LinearGradient(
            r.left, r.top, r.left, r.top + r.height() * 0.46f,
            highlight, highlight and 0x00FFFFFF, Shader.TileMode.CLAMP)
    }

    override fun draw(canvas: Canvas) {
        val r = RectF(bounds)
        if (r.width() <= 0f || r.height() <= 0f) return
        val inset = strokeWidth / 2f
        val surface = RectF(r.left + inset, r.top + inset,
                            r.right - inset, r.bottom - inset)
        canvas.drawRoundRect(surface, radius, radius, fillPaint)

        val clip = Path().apply {
            addRoundRect(surface, radius, radius, Path.Direction.CW)
        }
        val save = canvas.save()
        canvas.clipPath(clip)
        canvas.drawRect(surface.left, surface.top, surface.right,
                        surface.top + surface.height() * 0.46f, highlightPaint)
        canvas.restoreToCount(save)

        canvas.drawRoundRect(surface, radius, radius, borderPaint)
    }

    override fun setAlpha(alpha: Int) {
        fillPaint.alpha = alpha
        borderPaint.alpha = alpha
        highlightPaint.alpha = alpha
    }

    override fun setColorFilter(colorFilter: ColorFilter?) {
        fillPaint.colorFilter = colorFilter
        borderPaint.colorFilter = colorFilter
        highlightPaint.colorFilter = colorFilter
    }

    @Deprecated("Deprecated in Java")
    override fun getOpacity(): Int = PixelFormat.TRANSLUCENT
}
