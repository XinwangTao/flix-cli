package com.relaydesk.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Size
import android.view.Gravity
import android.view.Surface
import android.view.TextureView
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import com.google.zxing.BinaryBitmap
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Front-facing QR scanner: Camera2 preview plus a YUV ImageReader decoded by
 * ZXing. Returns the raw payload through [EXTRA_RESULT]; pairing decisions stay
 * in MainActivity so this screen only ever reports text.
 */
class QrScanActivity : Activity(), TextureView.SurfaceTextureListener {

    companion object {
        const val EXTRA_RESULT = "qr_result"
    }

    private lateinit var textureView: TextureView
    private lateinit var hint: TextView
    private var camera: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var bgThread: HandlerThread? = null
    private lateinit var bgHandler: Handler
    private val decoding = AtomicBoolean(false)
    private val zxing = MultiFormatReader()
    private var finished = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (checkSelfPermission(Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED) {
            finish()
            return
        }
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        textureView = TextureView(this).apply {
            surfaceTextureListener = this@QrScanActivity
        }
        root.addView(textureView, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        hint = TextView(this).apply {
            text = "将对方的配对二维码放入取景框"
            setTextColor(Color.WHITE)
            setBackgroundColor(0x99000000.toInt())
            gravity = Gravity.CENTER
            setPadding(24, 28, 24, 28)
        }
        root.addView(hint, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM))
        setContentView(root)
        bgThread = HandlerThread("relaydesk-qr").also { it.start() }
        bgHandler = Handler(bgThread!!.looper)
    }

    override fun onResume() {
        super.onResume()
        if (textureView.isAvailable) openCamera() else textureView.surfaceTextureListener = this
    }

    override fun onPause() {
        closeCamera()
        super.onPause()
    }

    override fun onDestroy() {
        bgThread?.quitSafely()
        bgThread = null
        super.onDestroy()
    }

    // ---------- camera2 ----------

    private fun openCamera() {
        val manager = getSystemService(CameraManager::class.java)
        val id = manager.cameraIdList.firstOrNull { candidate ->
            manager.getCameraCharacteristics(candidate)
                .get(CameraCharacteristics.LENS_FACING) ==
                CameraCharacteristics.LENS_FACING_BACK
        } ?: manager.cameraIdList.firstOrNull() ?: run {
            finish()
            return
        }
        val chars = manager.getCameraCharacteristics(id)
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?: return finish()
        val size = chooseSize(map.getOutputSizes(ImageFormat.YUV_420_888))
        reader = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
        reader?.setOnImageAvailableListener({ decodeLatest(it) }, bgHandler)
        try {
            manager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(device: CameraDevice) {
                    camera = device
                    startPreview(device)
                }

                override fun onDisconnected(device: CameraDevice) {
                    device.close()
                    if (camera === device) camera = null
                }

                override fun onError(device: CameraDevice, error: Int) {
                    device.close()
                    if (camera === device) camera = null
                    runOnUiThread { hint.text = "相机不可用，请手动输入 IP" }
                }
            }, bgHandler)
        } catch (_: SecurityException) {
            finish()
        }
    }

    private fun chooseSize(sizes: Array<Size>?): Size {
        if (sizes.isNullOrEmpty()) return Size(1280, 720)
        return sizes.filter { it.width >= 640 && it.height >= 480 }
            .minByOrNull { kotlin.math.abs(it.width - 1280) } ?: sizes.first()
    }

    private fun startPreview(device: CameraDevice) {
        val texture = textureView.surfaceTexture ?: return
        texture.setDefaultBufferSize(1280, 720)
        val previewSurface = Surface(texture)
        val readerSurface = reader?.surface ?: return
        val request = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
            addTarget(previewSurface)
            addTarget(readerSurface)
            set(CaptureRequest.CONTROL_AF_MODE,
                CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
        }
        device.createCaptureSession(listOf(previewSurface, readerSurface),
            object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(configured: CameraCaptureSession) {
                    session = configured
                    runCatching { configured.setRepeatingRequest(request.build(), null, bgHandler) }
                }

                override fun onConfigureFailed(configured: CameraCaptureSession) {
                    runOnUiThread { hint.text = "无法启动相机预览" }
                }
            }, bgHandler)
    }

    private fun closeCamera() {
        runCatching { session?.close() }
        session = null
        runCatching { camera?.close() }
        camera = null
        runCatching { reader?.close() }
        reader = null
    }

    // ---------- ZXing ----------

    private fun decodeLatest(reader: ImageReader) {
        val image = reader.acquireLatestImage() ?: return
        try {
            if (finished || !decoding.compareAndSet(false, true)) return
            val text = runCatching { decode(image) }.getOrNull()
            if (!text.isNullOrEmpty()) {
                finished = true
                runOnUiThread {
                    setResult(RESULT_OK, Intent().putExtra(EXTRA_RESULT, text))
                    finish()
                }
            }
        } finally {
            image.close()
            decoding.set(false)
        }
    }

    private fun decode(image: Image): String? {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val rowStride = plane.rowStride
        val pixelStride = plane.pixelStride
        val width = image.width
        val height = image.height
        val gray = ByteArray(width * height)
        val row = ByteArray(rowStride)
        var offset = 0
        for (y in 0 until height) {
            val toRead = minOf(rowStride, buffer.remaining())
            if (toRead <= 0) break
            buffer.get(row, 0, toRead)
            var x = 0
            while (x < width) {
                gray[offset + x] = row[x * pixelStride]
                x++
            }
            offset += width
        }
        val source = PlanarYUVLuminanceSource(gray, width, height, 0, 0, width, height, false)
        return zxing.decode(BinaryBitmap(HybridBinarizer(source)))?.text
    }

    // ---------- TextureView ----------

    override fun onSurfaceTextureAvailable(texture: SurfaceTexture, width: Int, height: Int) {
        openCamera()
    }

    override fun onSurfaceTextureSizeChanged(texture: SurfaceTexture, width: Int, height: Int) = Unit

    override fun onSurfaceTextureDestroyed(texture: SurfaceTexture): Boolean = true

    override fun onSurfaceTextureUpdated(texture: SurfaceTexture) = Unit
}
