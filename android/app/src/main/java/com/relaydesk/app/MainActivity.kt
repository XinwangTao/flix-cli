package com.relaydesk.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.wifi.WifiManager
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : Activity() {

    private lateinit var store: Store
    private var server: ChannelServer? = null
    private var discovery: Discovery? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private val fingerprint: String by lazy {
        getSharedPreferences("relaydesk", MODE_PRIVATE).getString("fingerprint", null)
            ?: Wire.newId().also {
                getSharedPreferences("relaydesk", MODE_PRIVATE).edit()
                    .putString("fingerprint", it).apply()
            }
    }
    private val alias: String by lazy {
        getSharedPreferences("relaydesk", MODE_PRIVATE).getString("alias", null)
            ?: android.os.Build.MODEL.also {
                getSharedPreferences("relaydesk", MODE_PRIVATE).edit()
                    .putString("alias", it).apply()
            }
    }

    // list screen
    private lateinit var listScreen: LinearLayout
    private lateinit var deviceList: ListView
    private lateinit var devicesAdapter: DeviceAdapter

    // chat screen
    private lateinit var chatScreen: LinearLayout
    private lateinit var chatTitle: TextView
    private lateinit var chatSubtitle: TextView
    private lateinit var pairButton: Button
    private lateinit var messageList: ListView
    private lateinit var messagesAdapter: MessageAdapter
    private lateinit var inputEdit: EditText
    private var currentPeer: JSONObject? = null
    private val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val timeFormat = SimpleDateFormat("HH:mm", Locale.getDefault())

    private val ATTACH_REQUEST = 41

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this)
        buildUi()
        startServer()
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
        }
    }

    private fun startServer() {
        server = ChannelServer(this, store, fingerprint, alias, Wire.DEVICE_PORT) {
                _, extra -> mainHandler.post { onIncoming(extra.optString("from")) }
        }
        runCatching { server!!.start(NanoLiteTimeout) }
            .onFailure {
                Toast.makeText(this, "通道端口 ${Wire.DEVICE_PORT} 被占用", Toast.LENGTH_LONG).show()
            }
    }

    private companion object {
        const val NanoLiteTimeout = 5000
    }

    // ---------- UI construction (framework views, dark theme) ----------

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi() {
        val root = FrameLayout(this).apply { setBackgroundColor(getColor(R.color.bg)) }

        // -- device list screen --
        listScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(getColor(R.color.bg))
        }
        val listHeader = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
            setBackgroundColor(getColor(R.color.bgHeader))
            gravity = Gravity.CENTER_VERTICAL
        }
        val brand = TextView(this).apply {
            text = "RelayDesk"
            setTextColor(Color.WHITE); textSize = 22f; setTypeface(typeface, Typeface.BOLD)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val refresh = TextView(this).apply {
            text = "↻"; setTextColor(getColor(R.color.textMuted)); textSize = 20f
            setPadding(dp(14), dp(6), dp(14), dp(6))
        }
        refresh.setOnClickListener { discovery?.announce(); refreshDevices(); toast("已广播") }
        val add = TextView(this).apply {
            text = "+"; setTextColor(getColor(R.color.textMuted)); textSize = 22f
            setPadding(dp(14), dp(6), dp(4), dp(6))
        }
        add.setOnClickListener { manualAddDialog() }
        listHeader.addView(brand); listHeader.addView(refresh); listHeader.addView(add)
        val listLabel = TextView(this).apply {
            text = "设备"; setTextColor(getColor(R.color.textMuted))
            setPadding(dp(16), dp(10), dp(16), dp(4))
        }
        devicesAdapter = DeviceAdapter()
        deviceList = ListView(this).apply {
            adapter = devicesAdapter
            divider = null
            setPadding(dp(8), dp(4), dp(8), dp(8))
            clipToPadding = false
        }
        deviceList.setOnItemClickListener { _, _, position, _ ->
            devicesAdapter.item(position)?.let { openChat(it) }
        }
        listScreen.addView(listHeader)
        listScreen.addView(listLabel)
        listScreen.addView(deviceList,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        // -- chat screen --
        chatScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(getColor(R.color.bg))
            visibility = View.GONE
        }
        val chatHeader = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(12), dp(12), dp(16), dp(12))
            setBackgroundColor(getColor(R.color.bgHeader))
            gravity = Gravity.CENTER_VERTICAL
        }
        val back = TextView(this).apply {
            text = "‹"; setTextColor(Color.WHITE); textSize = 26f
            setPadding(dp(8), dp(2), dp(14), dp(2))
        }
        back.setOnClickListener { closeChat() }
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        chatTitle = TextView(this).apply {
            setTextColor(Color.WHITE); textSize = 17f; setTypeface(typeface, Typeface.BOLD)
        }
        chatSubtitle = TextView(this).apply { setTextColor(getColor(R.color.textMuted)); textSize = 12f }
        titles.addView(chatTitle); titles.addView(chatSubtitle)
        pairButton = Button(this).apply {
            text = "配对"; textSize = 13f
            setTextColor(Color.WHITE); backgroundTintList = android.content.res
                .ColorStateList.valueOf(getColor(R.color.accent))
            setPadding(dp(18), dp(4), dp(18), dp(4))
        }
        pairButton.setOnClickListener { pairCurrent() }
        chatHeader.addView(back); chatHeader.addView(titles); chatHeader.addView(pairButton)
        messagesAdapter = MessageAdapter()
        messageList = ListView(this).apply {
            adapter = messagesAdapter
            divider = null
            setPadding(dp(10), dp(10), dp(10), dp(6))
            clipToPadding = false
            setStackFromBottom(true)
            transcriptMode = ListView.TRANSCRIPT_MODE_NORMAL
        }
        val composer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(10), dp(8), dp(10), dp(10))
            setBackgroundColor(getColor(R.color.bgPanel))
            gravity = Gravity.BOTTOM
        }
        val attach = TextView(this).apply {
            text = "▤"; setTextColor(getColor(R.color.textMuted)); textSize = 20f
            setPadding(dp(10), dp(12), dp(10), dp(12))
        }
        attach.setOnClickListener { attachFile() }
        inputEdit = EditText(this).apply {
            hint = "输入消息"; setHintTextColor(getColor(R.color.textMuted))
            setTextColor(Color.WHITE)
            background = roundRect(getColor(R.color.bgBubbleIn), dp(10))
            setPadding(dp(12), dp(10), dp(12), dp(10))
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val send = Button(this).apply {
            text = "发送"; textSize = 14f
            setTextColor(Color.WHITE); backgroundTintList = android.content.res
                .ColorStateList.valueOf(getColor(R.color.accent))
        }
        send.setOnClickListener { sendText() }
        composer.addView(attach)
        composer.addView(inputEdit)
        composer.addView(send)
        chatScreen.addView(chatHeader)
        chatScreen.addView(messageList,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        chatScreen.addView(composer)

        root.addView(listScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(chatScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)
    }

    private fun roundRect(color: Int, radiusPx: Int): android.graphics.drawable.GradientDrawable =
        android.graphics.drawable.GradientDrawable().apply {
            setColor(color); cornerRadius = radiusPx.toFloat()
        }

    private fun toast(text: String) =
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    // ---------- lifecycle ----------

    override fun onResume() {
        super.onResume()
        val wifi = applicationContext.getSystemService(WifiManager::class.java)
        multicastLock = wifi.createMulticastLock("relaydesk").apply {
            setReferenceCounted(false); acquire()
        }
        discovery = Discovery(fingerprint, alias, Wire.DEVICE_PORT) { dev ->
            mainHandler.post { onDeviceFound(dev) }
        }
        if (!discovery!!.start()) toast("UDP ${Wire.DEVICE_PORT} 绑定失败，仅可手动添加")
        discovery!!.announce()
        refreshDevices()
    }

    override fun onPause() {
        discovery?.stop(); discovery = null
        multicastLock?.takeIf { it.isHeld }?.release()
        multicastLock = null
        super.onPause()
    }

    override fun onDestroy() {
        runCatching { server?.stop() }
        super.onDestroy()
    }

    private fun onDeviceFound(dev: JSONObject) {
        store.upsertDevice(dev.optString("fingerprint"),
            dev.optString("alias"), dev.optString("deviceModel"),
            dev.optInt("port", Wire.DEVICE_PORT), dev.optInt("version"),
            dev.optString("source"), dev.optString("os"))
        refreshDevices()
    }

    // ---------- device list ----------

    private fun refreshDevices() {
        devicesAdapter.reload(store.devices())
    }

    private inner class DeviceAdapter : BaseAdapter() {
        private var items: List<JSONObject> = emptyList()
        fun reload(list: List<JSONObject>) { items = list; notifyDataSetChanged() }
        fun item(position: Int): JSONObject? = items.getOrNull(position)
        override fun getCount() = items.size
        override fun getItem(position: Int) = items[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val dev = items[position]
            val card = convertView ?: LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                background = roundRect(getColor(R.color.bgPanel), dp(12))
                setPadding(dp(14), dp(10), dp(14), dp(10))
                layoutParams = AbsParams(parent.context, dp(70), dp(6))
                addView(TextView(this@MainActivity).apply {
                    id = 1; setTextColor(Color.WHITE); textSize = 15f
                    setTypeface(typeface, Typeface.BOLD)
                })
                addView(TextView(this@MainActivity).apply {
                    id = 2; setTextColor(getColor(R.color.textMuted)); textSize = 12f
                })
            }
            val title = card.findViewById<TextView>(1)
            val subtitle = card.findViewById<TextView>(2)
            val fp = dev.optString("fingerprint")
            val lastRead = this@MainActivity.getSharedPreferences(
                "relaydesk", MODE_PRIVATE).getLong("lastread_$fp", 0L)
            val unread = store.lastIncoming(fp) > lastRead
            android.util.Log.e("RDUI", "getView ${fp.take(8)} unread=$unread lastIn=${store.lastIncoming(fp)} lastRead=$lastRead")
            val baseSub = "${dev.optString("os")} · ${dev.optString("ip")}:${dev.optInt("port")}"
            title.text = (dev.optString("alias").ifEmpty { fp.take(8) }) +
                if (store.isPaired(fp)) "  ●" else ""
            title.setTextColor(if (unread) getColor(R.color.accent) else Color.WHITE)
            subtitle.text = if (unread) "新消息 · $baseSub" else baseSub
            subtitle.setTextColor(if (unread) getColor(R.color.accent)
                                  else this@MainActivity.getColor(R.color.textMuted))
            return card
        }
    }

    private fun AbsParams(context: Context, height: Int, margins: Int): LinearLayout.LayoutParams {
        val p = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, height)
        p.setMargins(margins, margins, margins, 0)
        return p
    }

    private fun manualAddDialog() {
        val input = EditText(this).apply {
            hint = "IP:端口（默认 8891）"
            setTextColor(Color.WHITE)
        }
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("手动添加设备")
            .setView(input)
            .setPositiveButton("添加") { _, _ ->
                val raw = input.text.toString().trim().ifEmpty { return@setPositiveButton }
                val (ip, port) = if (raw.contains(":")) {
                    raw.substringBeforeLast(":") to raw.substringAfterLast(":").toIntOrNull()
                } else raw to null
                if (ip.isEmpty()) return@setPositiveButton
                Thread {
                    try {
                        val modal = ChannelClient.pingV2(ip, port ?: Wire.DEVICE_PORT,
                            fingerprint, alias, Wire.DEVICE_PORT)
                        runOnUiThread {
                            store.upsertDevice(modal.optString("fingerprint"),
                                modal.optString("alias"), modal.optString("deviceModel"),
                                modal.optInt("port", Wire.DEVICE_PORT),
                                modal.optInt("version"), ip, modal.optString("os"))
                            refreshDevices(); toast("已添加 ${modal.optString("alias")}")
                        }
                    } catch (e: Exception) {
                        runOnUiThread { toast("添加失败：${e.message}") }
                    }
                }.start()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // ---------- chat ----------

    private fun openChat(dev: JSONObject) {
        currentPeer = dev
        listScreen.visibility = View.GONE
        chatScreen.visibility = View.VISIBLE
        val fp = dev.optString("fingerprint")
        markRead(fp)
        chatTitle.text = dev.optString("alias").ifEmpty { fp.take(8) }
        chatSubtitle.text = "${dev.optString("os")} · ${dev.optString("ip")}"
        pairButton.visibility = if (store.isPaired(fp)) View.GONE else View.VISIBLE
        refreshMessages()
    }

    private fun closeChat() {
        currentPeer = null
        chatScreen.visibility = View.GONE
        listScreen.visibility = View.VISIBLE
        refreshDevices()
    }

    private fun refreshMessages() {
        val peer = currentPeer ?: return
        messagesAdapter.reload(store.conversation(peer.optString("fingerprint")))
    }

    private fun onIncoming(from: String = "") {
        android.util.Log.e("RDUI", "onIncoming from=${from.take(8)} chatOpen=${currentPeer != null}")
        if (from.isNotEmpty() && currentPeer != null &&
            currentPeer!!.optString("fingerprint") == from) {
            markRead(from)
            refreshMessages()
        }
        refreshDevices()
    }

    private fun markRead(peer: String) {
        getSharedPreferences("relaydesk", MODE_PRIVATE).edit()
            .putLong("lastread_$peer", System.currentTimeMillis()).apply()
    }

    private inner class MessageAdapter : BaseAdapter() {
        private var items: List<JSONObject> = emptyList()
        fun reload(list: List<JSONObject>) { items = list; notifyDataSetChanged() }
        override fun getCount() = items.size
        override fun getItem(position: Int) = items[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val msg = items[position]
            val outgoing = msg.optString("from") == fingerprint
            val wrap = convertView as? FrameLayout ?: FrameLayout(this@MainActivity)
            val old = wrap.findViewById<TextView>(9)
            if (old != null) wrap.removeView(old)
            val bubble = TextView(this@MainActivity).apply {
                id = 9
                textSize = 14f
                setTextColor(Color.WHITE)
                setPadding(dp(12), dp(8), dp(12), dp(8))
                background = roundRect(
                    if (outgoing) getColor(R.color.accent)
                    else getColor(R.color.bgBubbleIn), dp(10))
                val time = runCatching {
                    timeFormat.format(Date(msg.optLong("time")))
                }.getOrDefault("")
                text = when (msg.optInt("type")) {
                    Wire.TYPE_FILE ->
                        "▤  ${msg.optString("fileName")}\n" +
                        formatSize(msg.optLong("fileSize")) +
                        (msg.optString("filePath", "").takeIf { it.isNotEmpty() }
                            ?.let { "\n已保存" } ?: "")
                    else -> msg.optString("text")
                } + " $time"
                maxWidth = dp(280)
            }
            val params = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                if (outgoing) Gravity.END else Gravity.START)
            params.setMargins(dp(24), dp(3), dp(24), dp(3))
            bubble.layoutParams = params
            bubble.setOnLongClickListener {
                val clip = getSystemService(ClipboardManager::class.java)
                clip.setPrimaryClip(ClipData.newPlainText("relaydesk", msg.optString("text")))
                toast("已复制"); true
            }
            wrap.addView(bubble)
            return wrap
        }
    }

    private fun formatSize(bytes: Long): String {
        val units = listOf("B", "KB", "MB", "GB")
        var v = bytes.toDouble()
        for (u in units) {
            if (v < 1024 || u == "GB") {
                return if (u == "B") "${v.toInt()} $u" else "%.1f $u".format(v)
            }
            v /= 1024
        }
        return "$bytes B"
    }

    private fun requirePeer(): JSONObject? {
        val peer = currentPeer
        if (peer == null) toast("未选择设备")
        return peer
    }

    private fun resolve(peer: JSONObject): Pair<String, Int>? {
        val ip = store.deviceIp(peer.optString("fingerprint"))
            ?: peer.optString("ip").takeIf { it.isNotEmpty() }
            ?: return null
        return ip to (peer.optInt("port", 0).takeIf { it > 0 } ?: Wire.DEVICE_PORT)
    }

    private fun pairCurrent() {
        val peer = requirePeer() ?: return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val myKey = store.ownPairKey(
            getSharedPreferences("relaydesk", MODE_PRIVATE))
        Thread {
            try {
                val (code, msg) = ChannelClient.postIntent(target.first, target.second,
                    Wire.pairIntent(fingerprint, peer.optString("fingerprint"), myKey))
                runOnUiThread {
                    if (code == 0) {
                        store.addPair(peer.optString("fingerprint"), myKey)
                        pairButton.visibility = View.GONE
                        toast("已配对 $msg")
                    } else toast("配对被拒：$msg")
                }
            } catch (e: Exception) {
                runOnUiThread { toast("配对失败：${e.message}") }
            }
        }.start()
    }

    private fun sendText() {
        val peer = requirePeer() ?: return
        val text = inputEdit.text.toString().trim()
        if (text.isEmpty()) return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val fp = peer.optString("fingerprint")
        val bubble = Wire.textBubble(fingerprint, fp, text)
        inputEdit.setText("")
        Thread {
            try {
                ChannelClient.postBubble(target.first, target.second, bubble)
                store.addMessage(bubble.optString("id"), fingerprint, fp,
                    Wire.TYPE_TEXT, bubble.optLong("time"), "",
                    text = text)
                runOnUiThread { refreshMessages() }
            } catch (e: Exception) {
                runOnUiThread { toast("发送失败：${e.message}") }
                runOnUiThread { inputEdit.setText(text) }
            }
        }.start()
    }

    private fun attachFile() {
        val peer = requirePeer() ?: return
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
        }
        startActivityForResult(Intent.createChooser(intent, "选择要发送的文件"), ATTACH_REQUEST)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != ATTACH_REQUEST || resultCode != RESULT_OK ||
            data?.data == null) return
        val peer = requirePeer() ?: return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val fp = peer.optString("fingerprint")
        val uri = data.data!!
        Thread {
            try {
                var name = "file"
                var size = 0L
                contentResolver.query(uri, null, null, null, null)?.use { c ->
                    val nameIdx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val sizeIdx = c.getColumnIndex(OpenableColumns.SIZE)
                    if (c.moveToFirst()) {
                        if (nameIdx >= 0) name = c.getString(nameIdx) ?: name
                        if (sizeIdx >= 0) size = c.getLong(sizeIdx)
                    }
                }
                if (size <= 0) {
                    contentResolver.openInputStream(uri)!!.use { s ->
                        var n = 0L
                        val buf = ByteArray(1 shl 20)
                        while (true) { val r = s.read(buf); if (r < 0) break; n += r }
                        size = n
                    }
                }
                val rid = Wire.newId()
                val bubble = Wire.fileBubble(fingerprint, fp, rid, name,
                    contentResolver.getType(uri) ?: "application/octet-stream", size)
                ChannelClient.postBubble(target.first, target.second, bubble)
                contentResolver.openInputStream(uri)!!.use { input ->
                    ChannelClient.uploadFile(target.first, target.second, rid, name,
                        size, input)
                }
                store.addMessage(rid, fingerprint, fp, Wire.TYPE_FILE,
                    bubble.optLong("time"), "", fileName = name, fileSize = size)
                runOnUiThread { refreshMessages(); toast("文件已发送") }
            } catch (e: Exception) {
                runOnUiThread { toast("文件发送失败：${e.message}") }
            }
        }.start()
    }
}
