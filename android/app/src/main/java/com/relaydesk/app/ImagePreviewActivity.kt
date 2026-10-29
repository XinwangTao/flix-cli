package com.relaydesk.app

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import kotlin.math.abs
import kotlin.math.min

/**
 * Full-screen image viewer (v2E): swipe through the images of one
 * conversation, pin/double-tap to zoom, save into Download/RelayDesk.
 * Framework views only — no ViewPager, no AndroidX.
 */
class ImagePreviewActivity : Activity() {

    private lateinit var image: ImageView
    private lateinit var counter: TextView
    private lateinit var sources: ArrayList<String>
    private lateinit var names: ArrayList<String>
    private var index = 0
    private var bitmap: Bitmap? = null

    private val matrix = Matrix()
    private val handler = Handler(Looper.getMainLooper())
    private var zoom = 1f
    private var panX = 0f
    private var panY = 0f
    private var lastX = 0f
    private var lastY = 0f

    override fun onCreate(savedInstanceState: Bundle?) {
        setTheme(if (getSharedPreferences("relaydesk", MODE_PRIVATE)
                .getString("theme", "dark") == "light")
            R.style.Theme_RelayDesk_Light else R.style.Theme_RelayDesk)
        super.onCreate(savedInstanceState)
        sources = intent.getStringArrayListExtra(EXTRA_SOURCES) ?: arrayListOf()
        names = intent.getStringArrayListExtra(EXTRA_NAMES) ?: arrayListOf()
        if (sources.isEmpty()) {
            finish()
            return
        }
        index = intent.getIntExtra(EXTRA_INDEX, 0).coerceIn(0, sources.size - 1)

        val root = FrameLayout(this).apply { setBackgroundColor(0xFF000000.toInt()) }
        val bar = FrameLayout(this).apply { setBackgroundColor(0x66000000) }
        bar.addView(TextView(this).apply {
            text = "✕"; setTextColor(0xFFFFFFFF.toInt()); textSize = 22f
            setPadding(dp(18), dp(14), dp(18), dp(14))
            setOnClickListener { finish() }
        }, FrameLayout.LayoutParams(WRAP, WRAP).apply { gravity = Gravity.START })
        counter = TextView(this).apply {
            setTextColor(0xFFFFFFFF.toInt()); textSize = 13f
            setPadding(dp(8), dp(18), dp(8), dp(18))
            layoutParams = FrameLayout.LayoutParams(WRAP, WRAP).apply { gravity = Gravity.CENTER }
        }
        bar.addView(counter)
        bar.addView(TextView(this).apply {
            text = "保存"; setTextColor(0xFFFFFFFF.toInt()); textSize = 14f
            setPadding(dp(18), dp(16), dp(20), dp(16))
            setOnClickListener { saveCurrent() }
        }, FrameLayout.LayoutParams(WRAP, WRAP).apply { gravity = Gravity.END })

        image = ImageView(this).apply {
            // FIT_CENTER until the user zooms: a MATRIX drawable set before the
            // first layout would render into empty bounds and stay invisible.
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(0xFF000000.toInt())
            layoutParams = FrameLayout.LayoutParams(MATCH, MATCH)
        }
        root.addView(image)
        root.addView(bar, FrameLayout.LayoutParams(MATCH, WRAP).apply { gravity = Gravity.TOP })
        setContentView(root)

        val gestures = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true

            override fun onFling(e1: MotionEvent?, e2: MotionEvent,
                                 vx: Float, vy: Float): Boolean {
                if (e1 == null || zoom > 1.05f) return false
                val dx = e2.x - e1.x
                if (abs(dx) < dp(60) || abs(dx) < abs(e2.y - e1.y)) return false
                show(if (dx < 0) index + 1 else index - 1)
                return true
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                zoom = if (zoom > 1.05f) 1f else 2.5f
                panX = 0f; panY = 0f
                applyMatrix()
                return true
            }
        })
        val pinch = ScaleGestureDetector(this,
            object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
                override fun onScale(detector: ScaleGestureDetector): Boolean {
                    zoom = (zoom * detector.scaleFactor).coerceIn(1f, 6f)
                    if (zoom <= 1.05f) { panX = 0f; panY = 0f }
                    applyMatrix()
                    return true
                }
            })
        image.setOnTouchListener { _, event ->
            pinch.onTouchEvent(event)
            gestures.onTouchEvent(event)
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> { lastX = event.x; lastY = event.y }
                MotionEvent.ACTION_MOVE -> if (zoom > 1.05f && event.pointerCount == 1 &&
                    !pinch.isInProgress) {
                    panX += event.x - lastX
                    panY += event.y - lastY
                    lastX = event.x; lastY = event.y
                    applyMatrix()
                }
                MotionEvent.ACTION_UP ->
                    if (zoom > 1.05f) { lastX = event.x; lastY = event.y }
            }
            true
        }
        image.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> applyMatrix() }
        show(index)
    }

    private fun show(target: Int) {
        index = (target + sources.size) % sources.size
        counter.text = "${index + 1} / ${sources.size}"
        zoom = 1f
        panX = 0f; panY = 0f
        image.scaleType = ImageView.ScaleType.FIT_CENTER
        val source = sources[index]
        bitmap?.recycle()
        bitmap = null
        image.setImageBitmap(null)
        Thread {
            val loaded = Media.loadBitmap(this, source, 2600)
            handler.post {
                if (isFinishing) {
                    loaded?.recycle()
                    return@post
                }
                bitmap = loaded
                image.setImageBitmap(loaded)
                if (loaded == null) toast("图片已不可用")
                applyMatrix()
            }
        }.start()
    }

    /** Unzoomed pictures use FIT_CENTER; zoom switches to an explicit matrix. */
    private fun applyMatrix() {
        val bmp = bitmap ?: return
        if (zoom <= 1.02f) {
            image.scaleType = ImageView.ScaleType.FIT_CENTER
            image.imageMatrix = null
            return
        }
        val vw = image.width.toFloat()
        val vh = image.height.toFloat()
        if (vw <= 0f || vh <= 0f) return
        image.scaleType = ImageView.ScaleType.MATRIX
        val fit = min(vw / bmp.width, vh / bmp.height)
        val f = fit * zoom
        val dx = (vw - bmp.width * f) / 2f + panX
        val dy = (vh - bmp.height * f) / 2f + panY
        matrix.setScale(f, f)
        matrix.postTranslate(dx, dy)
        image.imageMatrix = matrix
    }

    private fun saveCurrent() {
        val name = names.getOrNull(index)?.takeIf { it.isNotEmpty() }
            ?: "relaydesk-${index + 1}.jpg"
        val saved = Media.saveToDownloads(this, sources[index], name)
        toast(if (saved != null) "已保存到 Download/RelayDesk/$saved" else "保存失败")
    }

    override fun onDestroy() {
        bitmap?.recycle()
        super.onDestroy()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    companion object {
        const val EXTRA_SOURCES = "sources"
        const val EXTRA_NAMES = "names"
        const val EXTRA_INDEX = "index"
        private const val WRAP = FrameLayout.LayoutParams.WRAP_CONTENT
        private const val MATCH = FrameLayout.LayoutParams.MATCH_PARENT
    }
}
