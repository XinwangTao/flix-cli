package com.relaydesk.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.view.View

/**
 * Ambient light field behind the glass surfaces. Large, very soft colour pools
 * give the translucent cards something worth refracting; drawn as radial
 * gradients rather than a bitmap so it costs one paint per frame and scales to
 * any tablet size.
 */
class AmbientBackdropView(context: Context, private val light: Boolean) : View(context) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)

    init {
        setLayerType(LAYER_TYPE_HARDWARE, null)
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        canvas.drawColor(if (light) 0xFFF4F2EC.toInt() else 0xFF060B0A.toInt())

        if (light) {
            glow(canvas, w * 0.12f, h * 0.04f, w * 0.95f, 0x3312795B)
            glow(canvas, w * 0.96f, h * 0.30f, w * 0.85f, 0x221E5F7A)
            glow(canvas, w * 0.48f, h * 1.02f, w * 0.95f, 0x22A96300)
        } else {
            glow(canvas, w * 0.10f, h * 0.02f, w * 1.00f, 0x662FB59A)
            glow(canvas, w * 0.98f, h * 0.28f, w * 0.90f, 0x553F78E0)
            glow(canvas, w * 0.52f, h * 1.04f, w * 1.00f, 0x44B77CFF)
        }
    }

    private fun glow(canvas: Canvas, cx: Float, cy: Float, radius: Float, argb: Int) {
        paint.shader = RadialGradient(
            cx, cy, radius, argb, argb and 0x00FFFFFF, Shader.TileMode.CLAMP)
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), paint)
    }
}
