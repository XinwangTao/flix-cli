package com.relaydesk.app

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.provider.Settings
import android.text.SpannableString
import android.text.Spanned
import android.text.style.BackgroundColorSpan
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Thin UI over [RelayService]: the service owns discovery, the channel and
 * notifications, so closing this Activity no longer cuts off receiving.
 */
class MainActivity : Activity() {

    private lateinit var store: Store
    private lateinit var prefs: SharedPreferences
    private lateinit var fingerprint: String
    private lateinit var alias: String

    // list screen
    private lateinit var listScreen: LinearLayout
    private lateinit var deviceList: ListView
    private lateinit var devicesAdapter: DeviceAdapter
    private lateinit var groupsAdapter: GroupAdapter
    private lateinit var tabDevices: TextView
    private lateinit var tabGroups: TextView
    private lateinit var emptyView: TextView
    private lateinit var serviceState: TextView
    private var listMode = MODE_DEVICES

    // chat screen
    private lateinit var chatScreen: LinearLayout
    private lateinit var chatTitle: TextView
    private lateinit var chatSubtitle: TextView
    private lateinit var pairButton: Button
    private lateinit var atButton: TextView
    private lateinit var transferState: TextView
    private lateinit var messageList: ListView
    private lateinit var messagesAdapter: MessageAdapter
    private lateinit var inputEdit: EditText
    private lateinit var sendButton: Button
    private lateinit var attachButton: TextView
    private var currentPeer: JSONObject? = null
    private var currentGroup: JSONObject? = null
    // dual pane (v2E): wide screens keep the list beside the chat
    private var dualPane = false
    private lateinit var paneHint: TextView
    private lateinit var paneDivider: View

    // settings screen (v2D)
    private lateinit var settingsScreen: LinearLayout
    private lateinit var settingsBody: LinearLayout
    // in-conversation search (v2D)
    private lateinit var searchRow: LinearLayout
    private lateinit var searchEdit: EditText
    private lateinit var searchCount: TextView
    private var messageQuery = ""
    // Open 群设置 dialog + the group it edits, so a peer snapshot can rebuild it.
    private var groupDialog: AlertDialog? = null
    private var groupDialogGid: String? = null
    private var pairDialog: AlertDialog? = null

    private val mainHandler = Handler(Looper.getMainLooper())
    private val timeFormat = SimpleDateFormat("HH:mm", Locale.getDefault())
    private val dayFormat = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())
    private val live = HashMap<String, JSONObject>() // resourceId -> file progress
    private val listener: (String, Any?) -> Unit = { event, payload ->
        mainHandler.post { onRuntimeEvent(event, payload) }
    }

    private val ATTACH_REQUEST = 41
    private val SAVE_AS_REQUEST = 43
    private var saveAsMessage: JSONObject? = null

    private companion object {
        const val ONLINE_TTL_MS = 90_000L
        const val MODE_DEVICES = "devices"
        const val MODE_GROUPS = "groups"
        const val QR_SCAN_REQUEST = 42
        val AVATAR_COLORS = intArrayOf(
            0xFF3478F6.toInt(), 0xFF8E6BFF.toInt(), 0xFF2FB59A.toInt(),
            0xFFE0703B.toInt(), 0xFFC94F7C.toInt(), 0xFF3F9AE0.toInt())
    }

    // ---------- theme (v2E) ----------

    /** The user's choice, not the system's: forced into the Resources here. */
    private fun wantsLightTheme(): Boolean =
        getSharedPreferences("relaydesk", MODE_PRIVATE)
            .getString("theme", "dark") == "light"

    override fun attachBaseContext(newBase: Context) {
        val light = newBase.getSharedPreferences("relaydesk", MODE_PRIVATE)
            .getString("theme", "dark") == "light"
        val cfg = Configuration(newBase.resources.configuration)
        cfg.uiMode = (cfg.uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
            (if (light) Configuration.UI_MODE_NIGHT_NO else Configuration.UI_MODE_NIGHT_YES)
        super.attachBaseContext(newBase.createConfigurationContext(cfg))
    }

    /** A theme switch recreates the Activity: land back in the same chat. */
    private fun restoreAfterRecreate() {
        val peer = prefs.getString("restore_peer", "").orEmpty()
        val group = prefs.getString("restore_group", "").orEmpty()
        if (peer.isEmpty() && group.isEmpty()) return
        prefs.edit().remove("restore_peer").remove("restore_group").apply()
        if (group.isNotEmpty()) {
            store.group(group)?.let { openGroupChat(it) }
        } else {
            store.devices().firstOrNull { it.optString("fingerprint") == peer }
                ?.let { openChat(it) }
        }
    }

    private fun switchTheme(light: Boolean) {
        prefs.edit()
            .putString("theme", if (light) "light" else "dark")
            .putString("restore_peer", currentPeer?.optString("fingerprint").orEmpty())
            .putString("restore_group", currentGroup?.optString("id").orEmpty())
            .apply()
        recreate()
    }

    /** Destructive confirms read as dangerous, not as a normal primary action. */
    private fun AlertDialog.danger(): AlertDialog = apply {
        getButton(AlertDialog.BUTTON_POSITIVE)?.setTextColor(getColor(R.color.danger))
    }

    private fun dlgTheme(): Int = if (wantsLightTheme())
        android.R.style.Theme_Material_Light_Dialog_Alert
    else android.R.style.Theme_Material_Dialog_Alert

    override fun onCreate(savedInstanceState: Bundle?) {
        setTheme(if (wantsLightTheme()) R.style.Theme_RelayDesk_Light else R.style.Theme_RelayDesk)
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("relaydesk", MODE_PRIVATE)
        store = Store(this)
        fingerprint = Identity.fingerprint(this)
        alias = Identity.alias(this)
        buildUi()
        restoreAfterRecreate()
        RelayService.start(this)
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
        }
        handleDeepLink(intent)
    }

    override fun onStart() {
        super.onStart()
        Runtime.addListener(listener)
        Runtime.foregroundPeer = currentPeer?.optString("fingerprint")
        Runtime.foregroundGroup = currentGroup?.optString("id")
        refreshDevices()
        refreshGroups()
        refreshServiceState()
        mainHandler.postDelayed({ refreshServiceState() }, 600)
    }

    override fun onStop() {
        Runtime.removeListener(listener)
        Runtime.foregroundPeer = null
        Runtime.foregroundGroup = null
        super.onStop()
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        if (intent != null) {
            setIntent(intent)
            handleDeepLink(intent)
        }
    }

    /** Notification taps arrive as `peer` (or `group`) extras and open that chat. */
    private fun handleDeepLink(intent: Intent?) {
        if (intent == null) return
        val peer = intent.getStringExtra("peer")?.takeIf { it.isNotEmpty() }
        val group = intent.getStringExtra("group")?.takeIf { it.isNotEmpty() }
        if (peer == null && group == null) return
        intent.removeExtra("peer")
        intent.removeExtra("group")
        if (group != null) {
            val g = store.group(group)
            if (g != null) openGroupChat(g) else toast("群聊暂不可用")
            return
        }
        val dev = store.devices().firstOrNull { it.optString("fingerprint") == peer }
        if (dev != null) openChat(dev) else toast("设备暂不可用，请先从列表打开")
    }

    // ---------- UI construction (framework views, dark theme) ----------

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi() {
        val wide = resources.configuration.smallestScreenWidthDp >= 600
        // Wide screens lose room to the chat pane, so the header tightens up.
        val iconPad = if (wide) dp(8) else dp(13)
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
            setTextColor(getColor(R.color.textPrimary))
            textSize = if (wide) 19f else 22f
            setTypeface(typeface, Typeface.BOLD)
            maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val refresh = TextView(this).apply {
            text = "↻"; setTextColor(getColor(R.color.textMuted)); textSize = 20f
            setPadding(iconPad, dp(6), iconPad, dp(6))
        }
        refresh.setOnClickListener {
            triggerRefresh()
        }
        val qr = TextView(this).apply {
            text = "▦"; setTextColor(getColor(R.color.textMuted)); textSize = 19f
            setPadding(iconPad, dp(6), dp(6), dp(6))
        }
        qr.setOnClickListener { showQrDialog() }
        val add = TextView(this).apply {
            text = "+"; setTextColor(getColor(R.color.textMuted)); textSize = 22f
            setPadding(iconPad, dp(6), dp(4), dp(6))
        }
        add.setOnClickListener {
            if (listMode == MODE_GROUPS) createGroupDialog() else manualAddDialog()
        }
        val readAll = TextView(this).apply {
            text = "✓"; setTextColor(getColor(R.color.textMuted)); textSize = 17f
            setPadding(iconPad, dp(6), iconPad, dp(6))
        }
        readAll.setOnClickListener { markAllRead() }
        val settingsIcon = TextView(this).apply {
            text = "⚙"; setTextColor(getColor(R.color.textMuted)); textSize = 18f
            setPadding(iconPad, dp(6), iconPad, dp(6))
        }
        settingsIcon.setOnClickListener { openSettings() }
        listHeader.addView(brand); listHeader.addView(settingsIcon)
        listHeader.addView(readAll); listHeader.addView(qr)
        listHeader.addView(refresh); listHeader.addView(add)

        tabDevices = TextView(this).apply {
            setPadding(dp(16), dp(8), dp(10), dp(8))
        }
        tabGroups = TextView(this).apply {
            setPadding(dp(10), dp(8), dp(16), dp(8))
        }
        tabDevices.setOnClickListener { switchListMode(MODE_DEVICES) }
        tabGroups.setOnClickListener { switchListMode(MODE_GROUPS) }
        val tabRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        tabRow.addView(tabDevices); tabRow.addView(tabGroups)

        devicesAdapter = DeviceAdapter()
        groupsAdapter = GroupAdapter()
        deviceList = ListView(this).apply {
            adapter = devicesAdapter
            divider = null
            setPadding(dp(8), dp(2), dp(8), dp(8))
            clipToPadding = false
        }
        deviceList.setOnItemClickListener { _, _, position, _ ->
            if (listMode == MODE_GROUPS) {
                groupsAdapter.item(position)?.let { openGroupChat(it) }
            } else {
                devicesAdapter.item(position)?.let { openChat(it) }
            }
        }
        deviceList.setOnItemLongClickListener { _, _, position, _ ->
            showConversationMenu(position)
            true
        }
        // Pull-to-refresh without an AndroidX dependency: a downward drag at
        // the top of the list re-announces and reloads.
        var pullStartY = -1f
        deviceList.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                android.view.MotionEvent.ACTION_DOWN ->
                    pullStartY = if (deviceList.firstVisiblePosition == 0 &&
                        deviceList.childCount > 0) e.rawY else -1f
                android.view.MotionEvent.ACTION_UP,
                android.view.MotionEvent.ACTION_CANCEL -> {
                    if (pullStartY >= 0 && e.rawY - pullStartY > dp(72)) {
                        triggerRefresh()
                    }
                    pullStartY = -1f
                }
            }
            false
        }
        emptyView = TextView(this).apply {
            text = "附近还没有设备\n连上同一 Wi-Fi 后点右上角 ↻ 重新发现"
            setTextColor(getColor(R.color.textMuted)); textSize = 14f
            gravity = Gravity.CENTER; setLineSpacing(dp(6).toFloat(), 1f)
            visibility = View.GONE
        }
        val listContainer = FrameLayout(this)
        listContainer.addView(deviceList, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        listContainer.addView(emptyView, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER))

        serviceState = TextView(this).apply {
            text = "后台服务启动中…"
            setTextColor(getColor(R.color.textMuted)); textSize = 11f
            gravity = Gravity.CENTER
            setPadding(dp(12), dp(6), dp(12), dp(10))
        }
        listScreen.addView(listHeader)
        listScreen.addView(tabRow)
        listScreen.addView(listContainer,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        listScreen.addView(serviceState)

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
            text = "‹"; setTextColor(getColor(R.color.textPrimary)); textSize = 26f
            setPadding(dp(8), dp(2), dp(14), dp(2))
        }
        back.setOnClickListener { closeChat() }
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        chatTitle = TextView(this).apply {
            setTextColor(getColor(R.color.textPrimary)); textSize = 17f; setTypeface(typeface, Typeface.BOLD)
        }
        chatSubtitle = TextView(this).apply { setTextColor(getColor(R.color.textMuted)); textSize = 12f }
        titles.addView(chatTitle); titles.addView(chatSubtitle)
        // Tapping the title opens group management (rename / members / leave).
        titles.setOnClickListener {
            if (currentGroup != null) groupManageDialog() else toast("单聊没有群设置")
        }
        pairButton = Button(this).apply {
            text = "配对"; textSize = 13f
            setTextColor(Color.WHITE); backgroundTintList = android.content.res
                .ColorStateList.valueOf(getColor(R.color.accent))
            setPadding(dp(18), dp(4), dp(18), dp(4))
        }
        pairButton.setOnClickListener { pairCurrent() }
        val searchToggle = TextView(this).apply {
            text = "⌕"; setTextColor(getColor(R.color.textMuted)); textSize = 20f
            setPadding(dp(10), dp(4), dp(10), dp(4))
        }
        searchToggle.setOnClickListener { toggleSearch() }
        chatHeader.addView(back); chatHeader.addView(titles)
        chatHeader.addView(searchToggle); chatHeader.addView(pairButton)

        transferState = TextView(this).apply {
            setTextColor(getColor(R.color.accent)); textSize = 12f
            setPadding(dp(16), dp(6), dp(16), dp(6))
            setBackgroundColor(getColor(R.color.bgPanel))
            visibility = View.GONE
        }

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
        attachButton = attach
        attach.setOnClickListener { attachFile() }
        inputEdit = EditText(this).apply {
            hint = "输入消息"; setHintTextColor(getColor(R.color.textMuted))
            setTextColor(getColor(R.color.textPrimary))
            background = roundRect(getColor(R.color.bgBubbleIn), dp(10))
            setPadding(dp(12), dp(10), dp(12), dp(10))
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        sendButton = Button(this).apply {
            text = "发送"; textSize = 14f
            setTextColor(Color.WHITE); backgroundTintList = android.content.res
                .ColorStateList.valueOf(getColor(R.color.accent))
        }
        sendButton.setOnClickListener { sendText() }
        atButton = TextView(this).apply {
            text = "@"; setTextColor(getColor(R.color.textMuted)); textSize = 18f
            setPadding(dp(10), dp(12), dp(2), dp(12))
            visibility = View.GONE
        }
        atButton.setOnClickListener { showMentionPicker() }
        composer.addView(attach)
        composer.addView(atButton)
        composer.addView(inputEdit)
        composer.addView(sendButton)

        // -- in-conversation search bar (v2D), hidden until ⌕ is tapped --
        searchRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(14), dp(4), dp(10), dp(4))
            setBackgroundColor(getColor(R.color.bgPanel))
            visibility = View.GONE
        }
        searchEdit = EditText(this).apply {
            hint = "搜索聊天记录与文件名"
            setHintTextColor(getColor(R.color.textMuted))
            setTextColor(getColor(R.color.textPrimary)); setSingleLine(); textSize = 14f
            background = null
            setPadding(0, dp(8), dp(8), dp(8))
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        searchEdit.addTextChangedListener(object : android.text.TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: android.text.Editable?) {
                messageQuery = s?.toString().orEmpty()
                refreshMessages()
            }
        })
        searchCount = TextView(this).apply {
            setTextColor(getColor(R.color.textMuted)); textSize = 11f
            setPadding(dp(4), 0, dp(8), 0)
        }
        val searchClose = TextView(this).apply {
            text = "✕"; setTextColor(getColor(R.color.textMuted)); textSize = 16f
            setPadding(dp(14), dp(12), dp(14), dp(12))
        }
        searchClose.setOnClickListener { toggleSearch() }
        searchRow.addView(searchEdit)
        searchRow.addView(searchCount)
        searchRow.addView(searchClose)

        chatScreen.addView(chatHeader)
        chatScreen.addView(transferState)
        chatScreen.addView(searchRow)
        chatScreen.addView(messageList,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        chatScreen.addView(composer)

        // -- settings screen (v2D) --
        settingsScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(getColor(R.color.bg))
            visibility = View.GONE
        }
        val settingsHeader = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(8), dp(12), dp(16), dp(12))
            setBackgroundColor(getColor(R.color.bgHeader))
            gravity = Gravity.CENTER_VERTICAL
        }
        settingsHeader.addView(TextView(this).apply {
            text = "‹"; setTextColor(getColor(R.color.textPrimary)); textSize = 26f
            setPadding(dp(12), dp(2), dp(14), dp(2))
            setOnClickListener { closeSettings() }
        })
        settingsHeader.addView(TextView(this).apply {
            text = "设置"; setTextColor(getColor(R.color.textPrimary)); textSize = 17f
            setTypeface(typeface, Typeface.BOLD)
        })
        settingsBody = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(2), dp(14), dp(28))
        }
        settingsScreen.addView(settingsHeader)
        settingsScreen.addView(ScrollView(this).apply {
            isFillViewport = true
            addView(settingsBody)
        }, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        root.addView(listScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(chatScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        paneHint = TextView(this).apply {
            text = "选择一个会话开始"
            setTextColor(getColor(R.color.textMuted)); textSize = 14f
            gravity = Gravity.CENTER
            visibility = View.GONE
        }
        root.addView(paneHint, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        paneDivider = View(this).apply { setBackgroundColor(getColor(R.color.divider)) }
        root.addView(paneDivider, FrameLayout.LayoutParams(
            dp(1), ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(settingsScreen, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)
        updateTabStyles()
        applyShell()
    }

    /**
     * ≥600dp shows the list and the chat side by side; narrower keeps the
     * one-screen-at-a-time stack. Margins on a FrameLayout keep both screens
     * laid out exactly once, so rotation only re-measures (v2E).
     */
    private fun applyShell() {
        val wide = resources.configuration.smallestScreenWidthDp >= 600
        dualPane = wide
        val width = resources.displayMetrics.widthPixels
        // A 360dp list keeps the header on one line; the chat takes the rest.
        val listWidth = if (wide) minOf(dp(380), (width * 0.46f).toInt()) else 0
        (listScreen.layoutParams as FrameLayout.LayoutParams).rightMargin =
            if (wide) width - listWidth else 0
        (chatScreen.layoutParams as FrameLayout.LayoutParams).leftMargin = listWidth
        (paneHint.layoutParams as FrameLayout.LayoutParams).leftMargin = listWidth
        (paneDivider.layoutParams as FrameLayout.LayoutParams).leftMargin = listWidth
        paneDivider.visibility = if (wide) View.VISIBLE else View.GONE
        listScreen.visibility = View.VISIBLE
        val hasChat = hasChatTarget()
        if (wide) {
            chatScreen.visibility = if (hasChat) View.VISIBLE else View.GONE
            paneHint.visibility = if (hasChat) View.GONE else View.VISIBLE
        } else {
            paneHint.visibility = View.GONE
        }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyShell()
    }

    private fun roundRect(color: Int, radiusPx: Int): android.graphics.drawable.GradientDrawable =
        android.graphics.drawable.GradientDrawable().apply {
            setColor(color); cornerRadius = radiusPx.toFloat()
        }

    private fun circle(color: Int): android.graphics.drawable.GradientDrawable =
        android.graphics.drawable.GradientDrawable().apply {
            shape = android.graphics.drawable.GradientDrawable.OVAL
            setColor(color)
        }

    private fun toast(text: String) =
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    // ---------- runtime events ----------

    private fun onRuntimeEvent(event: String, payload: Any?) {
        when (event) {
            "devices" -> {
                remapCurrentPeer()
                refreshDevices()
                refreshServiceState()
                syncChatHeader()
            }
            "groups" -> {
                refreshGroups()
                refreshServiceState()
                syncChatHeader()
            }
            "groupSync" -> {
                // Name / member list changed on a peer: re-read before redrawing.
                refreshGroups()
                syncChatHeader()
                refreshMessages()
                syncEmptyState()
                refreshGroupDialogIfOpen()
            }
            "messages" -> {
                val key = payload as? String
                if (key != null && currentGroup?.optString("id") == key) {
                    markGroupRead(key)
                    refreshMessages()
                    refreshGroups()
                } else if (key != null && currentPeer?.optString("fingerprint") == key) {
                    markRead(key)
                    refreshMessages()
                    refreshDevices()
                }
            }
            "fileProgress" -> {
                val p = payload as? JSONObject ?: return
                val rid = p.optString("rid")
                live[rid] = p
                if (p.optString("from") == currentPeer?.optString("fingerprint")) {
                    showTransfer(p)
                    refreshMessages()
                } else if (!p.optBoolean("done")) {
                    showTransfer(p)
                }
                if (p.optBoolean("done")) {
                    mainHandler.postDelayed({ hideTransfer(rid) }, 1500)
                }
            }
            "filePending" -> {
                val name = (payload as? JSONObject)?.optString("name") ?: "文件"
                toast("正在接收 $name")
            }
            "pairRequest" -> showPairDialog(payload as? Runtime.PairPrompt)
            "pairTimeout" -> {
                pairDialog?.dismiss()
                pairDialog = null
                toast("配对请求已超时，请对方重新发起")
            }
            "error" -> toast(payload as? String ?: "设备通道异常")
        }
    }

    private fun triggerRefresh() {
        Runtime.service?.announceNow()
        refreshDevices()
        toast("正在重新发现附近设备")
    }

    private fun refreshServiceState() {        serviceState.text = if (Runtime.serviceRunning) {
            val port = if (Runtime.servicePort > 0) " · 端口 ${Runtime.servicePort}" else ""
            "后台接收已开启$port · 退出界面仍可收消息"
        } else {
            "后台服务启动中…"
        }
    }

    // ---------- device list ----------

    private fun refreshDevices() {
        val items = dedupeDevices(store.devices())
        devicesAdapter.reload(items)
        if (listMode == MODE_DEVICES) {
            deviceList.adapter = devicesAdapter
            emptyView.text = "附近还没有设备\n连上同一 Wi-Fi 后点右上角 ↻ 重新发现"
            emptyView.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
            deviceList.visibility = if (items.isEmpty()) View.GONE else View.VISIBLE
        }
    }

    private fun refreshGroups() {
        val items = store.groups()
        groupsAdapter.reload(items)
        if (listMode == MODE_GROUPS) {
            deviceList.adapter = groupsAdapter
            emptyView.text = "还没有群聊\n点右上角 + 选择已配对设备建群"
            emptyView.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
            deviceList.visibility = if (items.isEmpty()) View.GONE else View.VISIBLE
        }
    }

    private fun switchListMode(mode: String) {
        listMode = mode
        updateTabStyles()
        if (mode == MODE_GROUPS) refreshGroups() else refreshDevices()
        syncEmptyState()
    }

    private fun updateTabStyles() {
        val active = getColor(R.color.textPrimary)
        val idle = getColor(R.color.textMuted)
        tabDevices.text = "设备"
        tabGroups.text = "群聊"
        tabDevices.setTextColor(if (listMode == MODE_DEVICES) active else idle)
        tabGroups.setTextColor(if (listMode == MODE_GROUPS) active else idle)
        tabDevices.setTypeface(Typeface.DEFAULT,
            if (listMode == MODE_DEVICES) Typeface.BOLD else Typeface.NORMAL)
        tabGroups.setTypeface(Typeface.DEFAULT,
            if (listMode == MODE_GROUPS) Typeface.BOLD else Typeface.NORMAL)
        tabDevices.textSize = 14f
        tabGroups.textSize = 14f
    }

    /** The active adapter decides whether the empty hint shows. */
    private fun syncEmptyState() {
        val count = if (listMode == MODE_GROUPS) groupsAdapter.getCount()
                    else devicesAdapter.getCount()
        emptyView.visibility = if (count == 0) View.VISIBLE else View.GONE
        deviceList.visibility = if (count == 0) View.GONE else View.VISIBLE
    }

    /**
     * The desktop keeps its fingerprint in prefs; a reset state dir mints a new
     * uuid with the same alias and IP. Show one row per real machine: online
     * first, then paired, then freshest seenAt.
     */
    private fun dedupeDevices(all: List<JSONObject>): List<JSONObject> {
        val best = LinkedHashMap<String, JSONObject>()
        val loose = mutableListOf<JSONObject>()
        for (dev in all) {
            val ip = dev.optString("ip")
            val alias = dev.optString("alias").lowercase()
            if (ip.isEmpty() || alias.isEmpty()) { loose += dev; continue }
            val key = "$alias|$ip"
            val cur = best[key]
            best[key] = if (cur == null || preferred(dev, cur)) dev else cur
        }
        // A pin belongs to the machine, not to the fingerprint it had when it
        // was set: after an identity reset the surviving row is the *other*
        // uuid, and the pin would otherwise vanish from the list (v2D).
        for (dev in best.values + loose) {
            val pinned = machineRows(dev, all).any { isPinnedPeer(it.optString("fingerprint")) }
            dev.put("pinned", if (pinned) 1 else 0)
        }
        return (best.values + loose).sortedWith(
            compareBy<JSONObject> { !(it.optInt("pinned") > 0) }
                .thenBy { it.optString("alias").lowercase() })
    }

    /** Same machine, possibly under several fingerprints (alias + IP). */
    private fun machineRows(dev: JSONObject, all: List<JSONObject>): List<JSONObject> {
        val ip = dev.optString("ip")
        val alias = dev.optString("alias").lowercase()
        val fp = dev.optString("fingerprint")
        if (ip.isEmpty() || alias.isEmpty())
            return all.filter { it.optString("fingerprint") == fp }
        return all.filter {
            it.optString("fingerprint") == fp ||
                (it.optString("ip") == ip && it.optString("alias").lowercase() == alias)
        }
    }

    private fun preferred(a: JSONObject, b: JSONObject): Boolean {
        val oa = isOnline(a); val ob = isOnline(b)
        if (oa != ob) return oa
        val pa = store.isPaired(a.optString("fingerprint"))
        val pb = store.isPaired(b.optString("fingerprint"))
        if (pa != pb) return pa
        return a.optLong("seenAt") > b.optLong("seenAt")
    }

    private fun isOnline(dev: JSONObject): Boolean {
        val seen = dev.optLong("seenAt", 0L)
        return seen > 0 && System.currentTimeMillis() - seen in 0..ONLINE_TTL_MS
    }

    private fun seenText(dev: JSONObject): String {
        val seen = dev.optLong("seenAt", 0L)
        if (seen <= 0) return "未在线"
        val d = System.currentTimeMillis() - seen
        return when {
            d < 60_000 -> "刚刚在线"
            d < 3_600_000 -> "${d / 60_000} 分钟前"
            d < 86_400_000 -> "${d / 3_600_000} 小时前"
            else -> dayFormat.format(Date(seen))
        }
    }

    private fun avatarColor(fp: String): Int =
        AVATAR_COLORS[(fp.hashCode().toLong() and 0x7FFFFFFF).toInt() % AVATAR_COLORS.size]

    private inner class DeviceAdapter : BaseAdapter() {
        private var items: List<JSONObject> = emptyList()
        fun reload(list: List<JSONObject>) { items = list; notifyDataSetChanged() }
        fun item(position: Int): JSONObject? = items.getOrNull(position)
        override fun getCount() = items.size
        override fun getItem(position: Int) = items[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val dev = items[position]
            val fp = dev.optString("fingerprint")
            val name = dev.optString("alias").ifEmpty { fp.take(8) }

            val row = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                background = roundRect(getColor(R.color.bgPanel), dp(14))
                setPadding(dp(12), dp(12), dp(12), dp(12))
                gravity = Gravity.CENTER_VERTICAL
                layoutParams = AbsParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(4))
            }
            if (dualPane && currentPeer?.optString("fingerprint") == fp) {
                (row.background as android.graphics.drawable.GradientDrawable)
                    .setStroke(dp(2), getColor(R.color.accent))
            }
            row.addView(TextView(this@MainActivity).apply {
                text = name.take(1).uppercase()
                setTextColor(Color.WHITE); textSize = 17f
                setTypeface(typeface, Typeface.BOLD)
                gravity = Gravity.CENTER
                background = circle(avatarColor(fp))
                layoutParams = LinearLayout.LayoutParams(dp(42), dp(42))
            })
            val textColumn = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), 0, dp(8), 0)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            val titleRow = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            }
            titleRow.addView(TextView(this@MainActivity).apply {
                text = name; setTextColor(getColor(R.color.textPrimary)); textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
                maxWidth = dp(180)
                ellipsize = android.text.TextUtils.TruncateAt.END
                maxLines = 1
            })
            titleRow.addView(TextView(this@MainActivity).apply {
                text = "●"; textSize = 9f
                setTextColor(if (isOnline(dev)) getColor(R.color.online)
                             else getColor(R.color.textMuted))
                setPadding(dp(6), dp(2), 0, 0)
            })
            if (store.isPaired(fp)) {
                titleRow.addView(TextView(this@MainActivity).apply {
                    text = "已配对"; textSize = 10f
                    setTextColor(getColor(R.color.textMuted))
                    setPadding(dp(8), dp(2), 0, 0)
                })
            }
            // `pinned` is stamped by dedupeDevices so a pin survives the peer
            // re-creating its fingerprint.
            if (dev.optInt("pinned") > 0) {
                titleRow.addView(TextView(this@MainActivity).apply {
                    text = "置顶"; textSize = 10f
                    setTextColor(getColor(R.color.accent))
                    setPadding(dp(8), dp(2), 0, 0)
                })
            }
            val last = store.lastMessage(fp)
            val preview = TextView(this@MainActivity).apply {
                text = previewText(last)
                setTextColor(getColor(R.color.textMuted)); textSize = 12f
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
                setPadding(0, dp(3), 0, 0)
            }
            textColumn.addView(titleRow)
            textColumn.addView(preview)
            textColumn.addView(TextView(this@MainActivity).apply {
                text = "${dev.optString("os")} · ${dev.optString("ip")}"
                setTextColor(getColor(R.color.textMuted)); textSize = 10f
                setPadding(0, dp(2), 0, 0)
            })
            row.addView(textColumn)

            val rightColumn = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.END
            }
            val lastTime = last?.optLong("time", 0L) ?: 0L
            rightColumn.addView(TextView(this@MainActivity).apply {
                text = if (lastTime > 0) shortTime(lastTime) else seenText(dev)
                setTextColor(getColor(R.color.textMuted)); textSize = 10f
                gravity = Gravity.END
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END }
            })
            val lastRead = prefs.getLong("lastread_$fp", 0L)
            val unread = store.unreadCount(fp, lastRead)
            if (unread > 0) {
                rightColumn.addView(TextView(this@MainActivity).apply {
                    text = if (unread > 99) "99+" else unread.toString()
                    setTextColor(Color.WHITE); textSize = 10f
                    gravity = Gravity.CENTER
                    background = circle(getColor(R.color.accent))
                    minWidth = dp(18); minHeight = dp(18)
                    setPadding(dp(5), dp(2), dp(5), dp(2))
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                        topMargin = dp(6); gravity = Gravity.END
                    }
                })
            }
            row.addView(rightColumn)
            return row
        }
    }

    private inner class GroupAdapter : BaseAdapter() {
        private var items: List<JSONObject> = emptyList()
        fun reload(list: List<JSONObject>) {
            items = list
            notifyDataSetChanged()
        }

        fun item(position: Int): JSONObject? = items.getOrNull(position)
        override fun getCount() = items.size
        override fun getItem(position: Int) = items[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val group = items[position]
            val gid = group.optString("id")
            val name = group.optString("name").ifEmpty { "群聊" }
            val row = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                background = roundRect(getColor(R.color.bgPanel), dp(14))
                setPadding(dp(12), dp(12), dp(12), dp(12))
                gravity = Gravity.CENTER_VERTICAL
                layoutParams = AbsParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(4))
            }
            if (dualPane && currentGroup?.optString("id") == gid) {
                (row.background as android.graphics.drawable.GradientDrawable)
                    .setStroke(dp(2), getColor(R.color.accent))
            }
            row.addView(TextView(this@MainActivity).apply {
                text = "群"; setTextColor(Color.WHITE); textSize = 15f
                setTypeface(typeface, Typeface.BOLD); gravity = Gravity.CENTER
                background = circle(avatarColor(gid))
                layoutParams = LinearLayout.LayoutParams(dp(42), dp(42))
            })
            val textColumn = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), 0, dp(8), 0)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            val titleRow = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            }
            titleRow.addView(TextView(this@MainActivity).apply {
                text = name; setTextColor(getColor(R.color.textPrimary)); textSize = 15f
                setTypeface(typeface, Typeface.BOLD); maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
                maxWidth = dp(200)
            })
            if (group.optInt("pinned") > 0) {
                titleRow.addView(TextView(this@MainActivity).apply {
                    text = "置顶"; textSize = 10f
                    setTextColor(getColor(R.color.accent))
                    setPadding(dp(8), dp(2), 0, 0)
                })
            }
            titleRow.addView(TextView(this@MainActivity).apply {
                text = if (group.optLong("leftAt", 0L) > 0) "已退出"
                       else "${group.optInt("memberCount")} 人"
                textSize = 10f
                setTextColor(getColor(R.color.textMuted))
                setPadding(dp(8), dp(2), 0, 0)
            })
            textColumn.addView(titleRow)
            textColumn.addView(TextView(this@MainActivity).apply {
                text = groupPreview(group.optString("id"))
                setTextColor(getColor(R.color.textMuted)); textSize = 12f
                maxLines = 1; setPadding(0, dp(3), 0, 0)
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
            row.addView(textColumn)

            val rightColumn = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL; gravity = Gravity.END
            }
            val last = store.groupLastMessage(gid)
            val lastTime = last?.optLong("time", 0L) ?: 0L
            rightColumn.addView(TextView(this@MainActivity).apply {
                text = if (lastTime > 0) shortTime(lastTime) else ""
                setTextColor(getColor(R.color.textMuted)); textSize = 10f
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END }
            })
            val unread = if (group.optLong("leftAt", 0L) > 0) 0 else store.groupUnread(gid,
                fingerprint, prefs.getLong("lastread_group_$gid", 0L))
            if (unread > 0) {
                rightColumn.addView(TextView(this@MainActivity).apply {
                    text = if (unread > 99) "99+" else unread.toString()
                    setTextColor(Color.WHITE); textSize = 10f; gravity = Gravity.CENTER
                    background = circle(getColor(R.color.accent))
                    minWidth = dp(18); minHeight = dp(18)
                    setPadding(dp(5), dp(2), dp(5), dp(2))
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                        topMargin = dp(6); gravity = Gravity.END
                    }
                })
            }
            row.addView(rightColumn)
            return row
        }
    }

    private fun groupPreview(gid: String): String {
        val last = store.groupLastMessage(gid) ?: return "点按进入群聊"
        val body = if (last.optInt("type") == Wire.TYPE_FILE)
            "▤ ${last.optString("fileName")}" else last.optString("text")
        if (last.optString("from") == fingerprint) return "我: $body"
        val sender = store.device(last.optString("from"))?.optString("alias")
            ?.takeIf { it.isNotEmpty() } ?: last.optString("from").take(6)
        return "$sender: $body"
    }

    private fun previewText(last: JSONObject?): String {
        if (last == null) return "点按开始对话"
        val body = if (last.optInt("type") == Wire.TYPE_FILE)
            "▤ ${last.optString("fileName")}" else last.optString("text")
        val prefix = if (last.optString("from") == fingerprint) "我: " else ""
        return prefix + body
    }

    private fun AbsParams(height: Int, margins: Int): LinearLayout.LayoutParams {
        val p = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, height)
        p.setMargins(margins, margins, margins, margins)
        return p
    }

    private fun shortTime(ms: Long): String {
        val now = System.currentTimeMillis()
        return if (now - ms < 86_400_000) timeFormat.format(Date(ms))
        else SimpleDateFormat("MM-dd", Locale.getDefault()).format(Date(ms))
    }

    private fun manualAddDialog() {
        val input = EditText(this).apply {
            hint = "IP:端口（默认 8891）"
            setTextColor(getColor(R.color.textPrimary))
        }
        AlertDialog.Builder(this, dlgTheme())
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
                            fingerprint, alias, ownPort())
                        mainHandler.post {
                            store.upsertDevice(modal.optString("fingerprint"),
                                modal.optString("alias"), modal.optString("deviceModel"),
                                modal.optInt("port", Wire.DEVICE_PORT),
                                modal.optInt("version"), ip, modal.optString("os"))
                            refreshDevices(); toast("已添加 ${modal.optString("alias")}")
                        }
                    } catch (e: Exception) {
                        mainHandler.post { toast("添加失败：${e.message}") }
                    }
                }.start()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun ownPort(): Int =
        Runtime.servicePort.takeIf { it > 0 } ?: Wire.DEVICE_PORT

    // ---------- groups ----------

    private fun createGroupDialog() {
        val paired = dedupeDevices(store.devices())
            .filter { store.isPaired(it.optString("fingerprint")) }
        if (paired.isEmpty()) {
            toast("先在「设备」里配对至少一台设备")
            return
        }
        val nameInput = EditText(this).apply {
            hint = "群聊名称"
            setTextColor(getColor(R.color.textPrimary)); setHintTextColor(getColor(R.color.textMuted))
            setPadding(dp(20), dp(10), dp(20), dp(10))
        }
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("新建群聊")
            .setView(nameInput)
            .setPositiveButton("下一步") { _, _ ->
                pickGroupMembers(nameInput.text.toString().trim(), paired)
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun pickGroupMembers(name: String, paired: List<JSONObject>) {
        val titles = paired.map { dev ->
            dev.optString("alias").ifEmpty { dev.optString("fingerprint").take(8) }
        }.toTypedArray()
        val checked = BooleanArray(paired.size)
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("选择成员")
            .setMultiChoiceItems(titles, checked) { _, which, isChecked ->
                checked[which] = isChecked
            }
            .setPositiveButton("创建") { _, _ ->
                val members = paired.filterIndexed { i, _ -> checked[i] }
                if (members.isEmpty()) {
                    toast("至少选择一位成员")
                    return@setPositiveButton
                }
                val gid = Wire.newId()
                val groupName = name.ifEmpty { "群聊 ${members.size + 1} 人" }
                store.upsertGroup(gid, groupName, rev = System.currentTimeMillis())
                for (dev in members) store.addGroupMember(gid, dev.optString("fingerprint"))
                store.addGroupMember(gid, fingerprint)
                pushGroupSync(gid)
                switchListMode(MODE_GROUPS)
                refreshGroups()
                store.group(gid)?.let { openGroupChat(it) }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // ---------- group management (v2C) ----------

    private fun sectionLabel(label: String): TextView = TextView(this).apply {
        text = label
        setTextColor(getColor(R.color.textMuted))
        textSize = 11f
        setPadding(0, dp(12), 0, dp(4))
    }

    /**
     * Snapshot fan-out only. A peer that answers 500 (official client / older
     * build) is ignored: our local change stands and the next change re-pushes
     * the newer rev.
     */
    private fun sendGroupSnapshot(gid: String, name: String, members: List<String>, rev: Long) {
        val targets = mutableListOf<Triple<String, String, Int>>()
        for (fp in members) {
            if (fp == fingerprint) continue
            val dev = store.device(fp) ?: continue
            val ip = dev.optString("ip")
            val port = dev.optInt("port", 0)
            if (ip.isEmpty() || port <= 0) continue
            targets.add(Triple(fp, ip, port))
        }
        if (targets.isEmpty()) return
        val intent = Wire.groupSyncIntent(fingerprint, gid, name, members, rev)
        Thread {
            var failed = 0
            for ((_, ip, port) in targets) {
                try {
                    ChannelClient.postIntent(ip, port, intent, timeoutMs = 5000)
                } catch (_: Exception) {
                    // Offline, unpaired (400) or an official client answering
                    // 500. The local snapshot stands and the next change ships
                    // a bigger rev, but the user must not be left believing the
                    // change reached everyone.
                    failed++
                }
            }
            if (failed > 0) mainHandler.post {
                toast("群设置已保存 · $failed 台设备未同步")
            }
        }.start()
    }

    /** Write the local snapshot (self included) then broadcast it to members. */
    private fun pushGroupSync(gid: String, newName: String? = null) {
        if (store.isGroupLeft(gid)) return
        val name = newName ?: store.group(gid)?.optString("name").orEmpty()
        if (name.isEmpty()) return
        val members = LinkedHashSet(store.groupMembers(gid))
        members.add(fingerprint)
        val rev = System.currentTimeMillis()
        store.writeGroup(gid, name, members.toList(), rev)
        sendGroupSnapshot(gid, name, members.toList(), rev)
    }

    /** Rename / add / remove / leave — every path writes locally, then syncs. */
    private fun groupManageDialog() {
        val group = currentGroup ?: return
        val gid = group.optString("id")
        val left = store.isGroupLeft(gid)
        var dialogRef: AlertDialog? = null
        fun reopen() {
            dialogRef?.dismiss()
            groupManageDialog()
        }

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(4), dp(20), dp(4))
        }
        column.addView(sectionLabel(if (left) "群名称 · 已退出，只读" else "群名称"))
        val nameInput = EditText(this).apply {
            setText(store.group(gid)?.optString("name") ?: "")
            setTextColor(getColor(R.color.textPrimary)); setHintTextColor(getColor(R.color.textMuted))
            hint = "群聊名称"; isEnabled = !left; setSingleLine()
        }
        column.addView(nameInput)
        val saveName = Button(this).apply {
            text = "保存名称"; isEnabled = !left
        }
        saveName.setOnClickListener {
            val name = nameInput.text.toString().trim()
            if (name.isEmpty()) {
                toast("群名不能为空")
            } else {
                pushGroupSync(gid, name)
                syncChatHeader(); refreshGroups(); reopen()
                toast("群名已更新")
            }
        }
        column.addView(saveName)

        val members = store.groupMembers(gid)
        column.addView(sectionLabel("成员 ${members.size} 人"))
        if (members.isEmpty()) {
            column.addView(TextView(this).apply {
                text = "还没有成员"; setTextColor(getColor(R.color.textMuted)); textSize = 13f
            })
        }
        for (fp in members) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(6), 0, dp(6))
            }
            val dev = store.device(fp)
            val who = if (fp == fingerprint) "我"
                      else dev?.optString("alias")?.takeIf { it.isNotEmpty() } ?: fp.take(8)
            val online = if (dev != null && isOnline(dev)) " · 在线" else ""
            row.addView(TextView(this).apply {
                text = who + online
                setTextColor(getColor(R.color.textPrimary)); textSize = 14f
                layoutParams = LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
            if (!left && fp != fingerprint) {
                row.addView(TextView(this).apply {
                    text = "移出"; setTextColor(getColor(R.color.accent)); textSize = 13f
                    setPadding(dp(14), dp(8), dp(4), dp(8))
                    setOnClickListener {
                        store.removeGroupMember(gid, fp)
                        pushGroupSync(gid)
                        refreshGroups(); syncChatHeader(); reopen()
                        toast("已移出 $who")
                    }
                })
            }
            column.addView(row)
        }

        if (!left) {
            val addMember = Button(this).apply { text = "添加成员" }
            addMember.setOnClickListener {
                val candidates = dedupeDevices(store.devices())
                    .filter { store.isPaired(it.optString("fingerprint")) }
                    .filter { !members.contains(it.optString("fingerprint")) }
                if (candidates.isEmpty()) {
                    toast("没有可添加的已配对设备")
                    return@setOnClickListener
                }
                val titles = candidates.map {
                    it.optString("alias").ifEmpty { it.optString("fingerprint").take(8) }
                }.toTypedArray()
                val checked = BooleanArray(candidates.size)
                AlertDialog.Builder(this, dlgTheme())
                    .setTitle("添加成员")
                    .setMultiChoiceItems(titles, checked) { _, which, isChecked ->
                        checked[which] = isChecked
                    }
                    .setPositiveButton("添加") { _, _ ->
                        val picked = candidates.filterIndexed { i, _ -> checked[i] }
                        if (picked.isEmpty()) return@setPositiveButton
                        for (dev in picked) {
                            store.addGroupMember(gid, dev.optString("fingerprint"))
                        }
                        pushGroupSync(gid)
                        refreshGroups(); syncChatHeader(); reopen()
                        toast("已添加 ${picked.size} 位成员")
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
            column.addView(addMember)

            val leave = Button(this).apply {
                text = "退出群聊"
                setTextColor(getColor(R.color.danger))
            }
            leave.setOnClickListener {
                val others = members.filter { it != fingerprint }
                val name = store.group(gid)?.optString("name").orEmpty()
                val rev = System.currentTimeMillis()
                sendGroupSnapshot(gid, name, others, rev)
                store.markGroupLeft(gid, rev)
                refreshGroups(); syncChatHeader(); refreshMessages()
                dialogRef?.dismiss()
                toast("已退出群聊，历史保留")
            }
            column.addView(leave)
        }

        val dlg = AlertDialog.Builder(this, dlgTheme())
            .setTitle("群设置")
            .setView(ScrollView(this).apply { addView(column) })
            .setNegativeButton("关闭", null)
            .create()
        dialogRef = dlg
        groupDialog = dlg
        groupDialogGid = gid
        dlg.setOnDismissListener {
            if (groupDialogGid == gid) {
                groupDialogGid = null
                groupDialog = null
            }
        }
        dlg.show()
    }

    /** Rebuild 群设置 after a peer snapshot, so a stale name/member list never
     *  survives — and a group we were just removed from flips to read-only. */
    private fun refreshGroupDialogIfOpen() {
        val gid = groupDialogGid ?: return
        if (currentGroup?.optString("id") != gid) {
            groupDialogGid = null
            groupDialog = null
            return
        }
        groupDialogGid = null
        groupDialog?.dismiss()
        groupDialog = null
        groupManageDialog()
    }

    // ---------- QR pairing ----------

    private fun localIpv4(): String {
        return try {
            java.net.NetworkInterface.getNetworkInterfaces().toList()
                .flatMap { it.inetAddresses.toList() }
                .firstOrNull {
                    !it.isLoopbackAddress && it is java.net.Inet4Address &&
                        it.isSiteLocalAddress
                }?.hostAddress ?: ""
        } catch (_: Exception) {
            ""
        }
    }

    private fun showQrDialog() {
        val payload = buildString {
            append("relaydesk://pair?fp=").append(fingerprint)
            append("&ip=").append(localIpv4())
            append("&port=").append(ownPort())
            append("&code=").append(store.ownPairKey(prefs))
        }
        val size = dp(220)
        val bitmap: Bitmap? = try {
            val hints = java.util.HashMap<com.google.zxing.EncodeHintType, Any>()
            hints[com.google.zxing.EncodeHintType.MARGIN] = 1
            val matrix = QRCodeWriter().encode(payload, BarcodeFormat.QR_CODE, size, size, hints)
            Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888).apply {
                for (x in 0 until size) for (y in 0 until size) {
                    setPixel(x, y, if (matrix.get(x, y)) Color.BLACK else Color.WHITE)
                }
            }
        } catch (_: Exception) {
            null
        }
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(16), dp(24), dp(8))
        }
        if (bitmap != null) {
            column.addView(ImageView(this).apply {
                setImageBitmap(bitmap)
                layoutParams = LinearLayout.LayoutParams(dp(220), dp(220))
            })
        }
        column.addView(TextView(this).apply {
            text = "让另一台设备扫描此码即可配对\nIP ${localIpv4()} · 端口 ${ownPort()}"
            setTextColor(getColor(R.color.textMuted)); textSize = 12f
            gravity = Gravity.CENTER; setPadding(0, dp(10), 0, dp(4))
        })
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("我的配对二维码")
            .setView(column)
            .setPositiveButton("扫码添加设备") { _, _ -> startQrScan() }
            .setNegativeButton("手动输入 IP") { _, _ -> manualAddDialog() }
            .setNeutralButton("关闭", null)
            .show()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>,
                                            grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 8 && grantResults.firstOrNull() ==
            PackageManager.PERMISSION_GRANTED) {
            startQrScan()
        }
    }

    private fun startQrScan() {
        if (checkSelfPermission(Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 8)
            toast("授权相机后再扫码")
            return
        }
        startActivityForResult(Intent(this, QrScanActivity::class.java), QR_SCAN_REQUEST)
    }

    private fun handleScannedCode(raw: String) {
        val uri = runCatching { Uri.parse(raw) }.getOrNull() ?: return
        if (uri.scheme != "relaydesk" || uri.host != "pair") {
            toast("这不是 RelayDesk 配对码")
            return
        }
        val fp = uri.getQueryParameter("fp").orEmpty()
        val ip = uri.getQueryParameter("ip").orEmpty()
        val port = uri.getQueryParameter("port")?.toIntOrNull() ?: Wire.DEVICE_PORT
        val code = uri.getQueryParameter("code").orEmpty()
        if (fp.isEmpty() || ip.isEmpty()) {
            toast("配对码不完整")
            return
        }
        if (fp == fingerprint) {
            toast("这是本机的二维码")
            return
        }
        Thread {
            val modal = runCatching {
                ChannelClient.pingV2(ip, port, fingerprint, alias, ownPort(), timeoutMs = 4000)
            }.getOrNull()
            if (modal == null) {
                mainHandler.post { toast("二维码对应的设备不在线，请确认连在同一 Wi-Fi") }
                return@Thread
            }
            val realFp = modal.optString("fingerprint").takeIf { it.isNotEmpty() } ?: fp
            if (realFp != fp) {
                mainHandler.post { toast("二维码与设备身份不一致，已拒绝") }
                return@Thread
            }
            mainHandler.post {
                val name = modal?.optString("alias")?.takeIf { it.isNotEmpty() } ?: realFp.take(8)
                AlertDialog.Builder(this, dlgTheme())
                    .setTitle("发现设备")
                    .setMessage("「$name」（$ip:$port）\n配对码：${code.ifEmpty { "—" }}\n" +
                        "请与对方屏幕核对后确认。")
                    .setPositiveButton("配对") { _, _ ->
                        pairScanned(realFp, ip, port, name, modal)
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
        }.start()
    }

    private fun pairScanned(fp: String, ip: String, port: Int, name: String,
                            modal: JSONObject?) {
        val myKey = store.ownPairKey(prefs)
        store.upsertDevice(fp, name,
            modal?.optString("deviceModel").orEmpty(), port,
            modal?.optInt("version") ?: 0, ip, modal?.optString("os").orEmpty())
        refreshDevices()
        Thread {
            try {
                val (code, msg) = ChannelClient.postIntent(ip, port,
                    Wire.pairIntent(fingerprint, fp, myKey))
                mainHandler.post {
                    if (code == 0) {
                        store.addPair(fp, myKey)
                        toast("已配对 $msg")
                        refreshDevices()
                        store.devices().firstOrNull { it.optString("fingerprint") == fp }
                            ?.let { openChat(it) }
                    } else toast("配对被拒：$msg")
                }
            } catch (e: Exception) {
                mainHandler.post { toast("配对失败：${e.message}") }
            }
        }.start()
    }

    // ---------- chat ----------

    private fun openChat(dev: JSONObject) {
        currentPeer = dev
        currentGroup = null
        resetSearch()
        Runtime.foregroundPeer = dev.optString("fingerprint")
        Runtime.foregroundGroup = null
        atButton.visibility = View.GONE
        pairButton.visibility = if (store.isPaired(dev.optString("fingerprint")))
            View.GONE else View.VISIBLE
        if (!dualPane) listScreen.visibility = View.GONE
        chatScreen.visibility = View.VISIBLE
        paneHint.visibility = View.GONE
        markRead(dev.optString("fingerprint"))
        syncChatHeader()
        refreshMessages()
        refreshDevices()
    }

    private fun openGroupChat(group: JSONObject) {
        currentGroup = group
        currentPeer = null
        resetSearch()
        Runtime.foregroundGroup = group.optString("id")
        Runtime.foregroundPeer = null
        atButton.visibility = View.VISIBLE
        pairButton.visibility = View.GONE
        if (!dualPane) listScreen.visibility = View.GONE
        chatScreen.visibility = View.VISIBLE
        paneHint.visibility = View.GONE
        if (!store.isGroupLeft(group.optString("id"))) markGroupRead(group.optString("id"))
        syncChatHeader()
        refreshMessages()
        refreshGroups()
    }

    private fun closeChat() {
        currentPeer = null
        currentGroup = null
        Runtime.foregroundPeer = null
        Runtime.foregroundGroup = null
        chatScreen.visibility = View.GONE
        listScreen.visibility = View.VISIBLE
        if (dualPane) paneHint.visibility = View.VISIBLE
        refreshDevices()
        refreshGroups()
        syncEmptyState()
    }

    private fun syncChatHeader() {
        val group = currentGroup
        if (group != null) {
            val fresh = store.group(group.optString("id")) ?: group
            currentGroup = fresh
            chatTitle.text = fresh.optString("name").ifEmpty { "群聊" }
            val members = store.groupMembers(fresh.optString("id"))
            if (fresh.optLong("leftAt", 0L) > 0) {
                chatSubtitle.text = "已退出群聊 · 历史只读"
            } else {
                val online = members.count { fp ->
                    store.device(fp)?.let { isOnline(it) } == true
                }
                chatSubtitle.text = "${members.size} 位成员 · 在线 $online · 点标题管理"
            }
            chatSubtitle.setTextColor(getColor(R.color.textMuted))
            pairButton.visibility = View.GONE
            syncComposerState()
            return
        }
        val peer = currentPeer ?: return
        val fp = peer.optString("fingerprint")
        val fresh = store.devices().firstOrNull { it.optString("fingerprint") == fp } ?: peer
        currentPeer = fresh
        chatTitle.text = fresh.optString("alias").ifEmpty { fp.take(8) }
        val online = if (isOnline(fresh)) "在线" else seenText(fresh)
        chatSubtitle.text = "$online · ${fresh.optString("ip")}"
        chatSubtitle.setTextColor(if (isOnline(fresh)) getColor(R.color.online)
                                  else getColor(R.color.textMuted))
        pairButton.visibility = if (store.isPaired(fp)) View.GONE else View.VISIBLE
        syncComposerState()
    }

    /** A left group keeps its history but the composer goes read-only. */
    private fun syncComposerState() {
        val left = currentGroup?.let { store.isGroupLeft(it.optString("id")) } == true
        val enabled = !left
        inputEdit.isEnabled = enabled
        sendButton.isEnabled = enabled
        attachButton.isEnabled = enabled
        atButton.isEnabled = enabled
        inputEdit.hint = if (left) "已退出该群，不能发送" else "输入消息"
        sendButton.alpha = if (enabled) 1f else 0.4f
        attachButton.alpha = if (enabled) 1f else 0.35f
        if (left) atButton.visibility = View.GONE
    }

    private fun refreshMessages() {
        val query = messageQuery.trim()
        val group = currentGroup
        val items = if (group != null) {
            val gid = group.optString("id")
            if (query.isEmpty()) store.groupConversation(gid)
            else store.searchGroupConversation(gid, query)
        } else {
            val peer = currentPeer ?: return
            val fp = peer.optString("fingerprint")
            if (query.isEmpty()) store.conversation(fp)
            else store.searchConversation(fp, query)
        }
        messagesAdapter.reload(items)
        if (::searchRow.isInitialized && searchRow.visibility == View.VISIBLE) {
            searchCount.text = when {
                query.isEmpty() -> ""
                items.isEmpty() -> "无结果"
                else -> "${items.size} 条"
            }
        }
    }

    /** ⌕ toggles an inline search bar; closing it restores the full history. */
    private fun toggleSearch() {
        val opening = searchRow.visibility != View.VISIBLE
        searchRow.visibility = if (opening) View.VISIBLE else View.GONE
        if (opening) {
            searchEdit.requestFocus()
            getSystemService(InputMethodManager::class.java)
                .showSoftInput(searchEdit, InputMethodManager.SHOW_IMPLICIT)
        } else {
            searchEdit.setText("")
            messageQuery = ""
            getSystemService(InputMethodManager::class.java)
                .hideSoftInputFromWindow(searchEdit.windowToken, 0)
            refreshMessages()
        }
    }

    /** Switching conversations starts from a clean, unfiltered history. */
    private fun resetSearch() {
        messageQuery = ""
        if (::searchRow.isInitialized) {
            searchRow.visibility = View.GONE
            searchEdit.setText("")
        }
    }

    private fun markRead(peer: String) {
        prefs.edit().putLong("lastread_$peer", System.currentTimeMillis()).apply()
        Runtime.service?.clearPeerNotifications(peer)
    }

    private fun markGroupRead(gid: String) {
        prefs.edit().putLong("lastread_group_$gid", System.currentTimeMillis()).apply()
        Runtime.service?.clearGroupNotifications(gid)
    }

    private fun showTransfer(p: JSONObject) {
        val total = p.optLong("total", 0L)
        val got = p.optLong("got", 0L)
        val pct = if (total > 0) (got * 100 / total).toInt() else 0
        transferState.text = if (p.optBoolean("done"))
            "✓ ${p.optString("name")} 已完成" else
            "接收 ${p.optString("name")} · $pct% (${formatSize(got)} / ${formatSize(total)})"
        transferState.visibility = View.VISIBLE
    }

    private fun hideTransfer(rid: String) {
        val p = live[rid] ?: return
        if (p.optBoolean("done")) transferState.visibility = View.GONE
    }

    /** One decoded thumbnail per message; the list is small, so 24 MB is plenty. */
    private val thumbs = android.util.LruCache<String, Bitmap>(24 * 1024 * 1024)
    private val thumbLoading = HashSet<String>()

    private fun bindImageBubble(wrap: FrameLayout, msg: JSONObject, outgoing: Boolean) {
        val id = msg.optString("id")
        val holder = FrameLayout(this).apply {
            background = roundRect(getColor(R.color.bgBubbleIn), dp(10))
            clipToOutline = true
            setOnClickListener { openImagePreview(msg) }
            setOnLongClickListener { showMessageMenu(msg); true }
        }
        holder.addView(ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            tag = id
            layoutParams = FrameLayout.LayoutParams(dp(180), dp(120))
        })
        holder.addView(TextView(this).apply {
            text = timeFormat.format(Date(msg.optLong("time")))
            setTextColor(0xFFFFFFFF.toInt()); textSize = 10f
            setPadding(dp(6), dp(2), dp(6), dp(2))
            background = roundRect(0x66000000, dp(6))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.END or Gravity.BOTTOM).apply { setMargins(0, 0, dp(4), dp(4)) }
        })
        val params = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            if (outgoing) Gravity.END else Gravity.START)
        params.setMargins(dp(24), dp(3), dp(24), dp(3))
        wrap.addView(holder, params)
        val picture = holder.getChildAt(0) as ImageView
        val cached = thumbs.get(id)
        if (cached != null) showThumb(picture, cached) else loadThumbnail(msg, picture)
    }

    /** Thumbnails follow the picture's aspect instead of a square crop. */
    private fun showThumb(target: ImageView, bmp: Bitmap) {
        target.setImageBitmap(bmp)
        val width = dp(180)
        val height = (width * bmp.height.toFloat() / bmp.width)
            .toInt().coerceIn(dp(80), dp(260))
        target.layoutParams = FrameLayout.LayoutParams(width, height)
    }

    private fun loadThumbnail(msg: JSONObject, target: ImageView) {
        val id = msg.optString("id")
        val source = Media.thumbSource(this, msg) ?: return
        if (!thumbLoading.add(id)) return
        Thread {
            val bmp = Media.loadBitmap(this, source, 480)
            mainHandler.post {
                thumbLoading.remove(id)
                if (bmp == null) return@post
                thumbs.put(id, bmp)
                if (target.tag == id) showThumb(target, bmp)
            }
        }.start()
    }

    private inner class MessageAdapter : BaseAdapter() {
        private var items: List<JSONObject> = emptyList()
        fun reload(list: List<JSONObject>) { items = list; notifyDataSetChanged() }
        fun snapshot(): List<JSONObject> = items
        override fun getCount() = items.size
        override fun getItem(position: Int) = items[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val msg = items[position]
            val outgoing = msg.optString("from") == fingerprint
            val wrap = convertView as? FrameLayout ?: FrameLayout(this@MainActivity)
            wrap.removeAllViews()
            val failed = msg.optInt("status") == 2
            if (msg.optInt("type") != Wire.TYPE_TEXT &&
                Media.isImage(msg.optString("fileName")) &&
                msg.optString("filePath").isNotEmpty()) {
                bindImageBubble(wrap, msg, outgoing)
                return wrap
            }
            val bubble = TextView(this@MainActivity).apply {
                textSize = 14f
                setTextColor(if (outgoing) getColor(R.color.textOnAccent) else getColor(R.color.textPrimary))
                setPadding(dp(12), dp(8), dp(12), dp(8))
                background = roundRect(
                    if (outgoing) getColor(R.color.accent) else getColor(R.color.bgBubbleIn),
                    dp(10))
                text = bubbleDisplayText(msg, outgoing)
                maxWidth = dp(280)
            }
            if (failed) bubble.setTextColor(getColor(R.color.bubbleTextFailed))
            val params = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                if (outgoing) Gravity.END else Gravity.START)
            params.setMargins(dp(24), dp(3), dp(24), dp(3))
            bubble.layoutParams = params
            bubble.setOnClickListener {
                when {
                    msg.optInt("type") == Wire.TYPE_FILE &&
                        msg.optString("filePath").isNotEmpty() ->
                        openFile(msg.optString("filePath"))
                    outgoing && failed && msg.optInt("type") == Wire.TYPE_TEXT ->
                        retryText(msg)
                }
            }
            bubble.setOnLongClickListener {
                showMessageMenu(msg)
                true
            }
            wrap.addView(bubble)
            return wrap
        }
    }

    /**
     * Group bubbles carry the sender on the first line; `@alias` mentions are
     * painted in amber so the addressee is visible at a glance.
     */
    private fun bubbleDisplayText(msg: JSONObject, outgoing: Boolean): CharSequence {
        val body = bubbleText(msg, outgoing)
        val sender = if (currentGroup == null || outgoing) {
            null
        } else {
            val alias = store.device(msg.optString("from"))?.optString("alias")
            alias?.takeIf { it.isNotEmpty() } ?: msg.optString("from").take(6)
        }
        val shown = if (sender.isNullOrEmpty()) body else "$sender\n$body"
        val spannable = SpannableString(shown)
        if (!sender.isNullOrEmpty()) {
            spannable.setSpan(StyleSpan(Typeface.BOLD), 0, sender.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            spannable.setSpan(ForegroundColorSpan(getColor(R.color.senderName)), 0, sender.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
        if (msg.optInt("type") == Wire.TYPE_TEXT) {
            val mention = Regex("@[\\w\\u4e00-\\u9fa5.\\-]+")
            for (m in mention.findAll(shown)) {
                spannable.setSpan(ForegroundColorSpan(getColor(R.color.mention)),
                    m.range.first, m.range.last + 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                spannable.setSpan(StyleSpan(Typeface.BOLD),
                    m.range.first, m.range.last + 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
        highlightMatches(spannable, shown)
        return spannable
    }

    /** Paints every occurrence of the active search term (v2D). */
    private fun highlightMatches(spannable: SpannableString, text: String) {
        val query = messageQuery.trim()
        if (query.isEmpty()) return
        var from = 0
        while (true) {
            val at = text.indexOf(query, from, ignoreCase = true)
            if (at < 0) return
            spannable.setSpan(BackgroundColorSpan(getColor(R.color.searchHighlight)), at, at + query.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            from = at + query.length
        }
    }

    private fun bubbleText(msg: JSONObject, outgoing: Boolean): String {
        val type = msg.optInt("type")
        val status = msg.optInt("status")
        val time = timeFormat.format(Date(msg.optLong("time")))
        return if (type == Wire.TYPE_FILE) {
            val prog = live[msg.optString("id")]
            val pct = prog?.let {
                val total = it.optLong("total", 0L)
                if (total > 0) (it.optLong("got", 0L) * 100 / total).toInt() else 0
            } ?: 0
            val state = when {
                status == 1 -> " · 发送中 $pct%"
                status == 2 -> " · 发送失败"
                outgoing -> " · 已发送"
                msg.optString("filePath").isNotEmpty() -> " · 已保存，点按打开"
                else -> " · 已接收"
            }
            "▤  ${msg.optString("fileName")}\n${formatSize(msg.optLong("fileSize"))}$state\n$time"
        } else {
            msg.optString("text") +
                (if (failed(outgoing, status)) "\n⚠ 发送失败 · 点按重试" else "") + "\n$time"
        }
    }

    private fun failed(outgoing: Boolean, status: Int) = outgoing && status == 2

    /** Every image of this conversation, so the viewer can swipe between them. */
    private fun openImagePreview(tapped: JSONObject) {
        val images = messagesAdapter.snapshot().filter {
            it.optInt("type") != Wire.TYPE_TEXT &&
                Media.isImage(it.optString("fileName")) &&
                it.optString("filePath").isNotEmpty()
        }
        if (images.isEmpty()) return
        val sources = ArrayList<String>()
        val names = ArrayList<String>()
        for (m in images) {
            sources += m.optString("filePath")
            names += m.optString("fileName")
        }
        val at = images.indexOfFirst { it.optString("id") == tapped.optString("id") }
            .coerceAtLeast(0)
        startActivity(Intent(this, ImagePreviewActivity::class.java)
            .putStringArrayListExtra(ImagePreviewActivity.EXTRA_SOURCES, sources)
            .putStringArrayListExtra(ImagePreviewActivity.EXTRA_NAMES, names)
            .putExtra(ImagePreviewActivity.EXTRA_INDEX, at))
    }

    private fun saveImageToDownloads(msg: JSONObject) {
        val path = msg.optString("filePath")
        val name = msg.optString("fileName").ifEmpty { "relaydesk.jpg" }
        Thread {
            val saved = Media.saveToDownloads(this, path, name)
            mainHandler.post {
                toast(if (saved != null) "已保存到 Download/RelayDesk/$saved" else "保存失败")
            }
        }.start()
    }

    /** SAF "save as": the user picks the destination, we stream the bytes. */
    private fun saveAsDialog(msg: JSONObject) {
        saveAsMessage = msg
        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = contentResolver.getType(Uri.parse(msg.optString("filePath"))) ?: "*/*"
            putExtra(Intent.EXTRA_TITLE, msg.optString("fileName").ifEmpty { "file" })
        }
        startActivityForResult(intent, SAVE_AS_REQUEST)
    }

    private fun copyToUri(msg: JSONObject, target: Uri) {
        val source = msg.optString("filePath")
        Thread {
            val ok = runCatching {
                Media.open(this, source)?.use { input ->
                    contentResolver.openOutputStream(target)?.use { out -> input.copyTo(out) }
                } ?: throw IllegalStateException("source unreadable")
            }.isSuccess
            mainHandler.post { toast(if (ok) "已另存" else "另存失败") }
        }.start()
    }

    private fun openFile(path: String) {
        try {
            val uri = Uri.parse(path)
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, contentResolver.getType(uri) ?: "*/*")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(intent)
        } catch (_: Exception) {
            toast("没有可打开该文件的应用")
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

    /** True when the composer has a conversation target (peer or group). */
    private fun hasChatTarget(): Boolean {
        if (currentGroup != null || currentPeer != null) return true
        toast("未选择会话")
        return false
    }

    private fun resolve(peer: JSONObject): Pair<String, Int>? {
        val ip = store.deviceIp(peer.optString("fingerprint"))
            ?: peer.optString("ip").takeIf { it.isNotEmpty() }
            ?: return null
        return ip to (peer.optInt("port", 0).takeIf { it > 0 } ?: Wire.DEVICE_PORT)
    }

    /** Reachable members of a group: (fingerprint, ip, port), self excluded. */
    private fun groupTargets(gid: String): List<Triple<String, String, Int>> =
        store.groupMembers(gid).mapNotNull { fp ->
            if (fp == fingerprint) return@mapNotNull null
            val dev = store.device(fp) ?: return@mapNotNull null
            val ip = dev.optString("ip").takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            val port = dev.optInt("port", 0).takeIf { it > 0 } ?: Wire.DEVICE_PORT
            if (!isOnline(dev)) return@mapNotNull null
            Triple(fp, ip, port)
        }

    private fun showMentionPicker() {
        val group = currentGroup ?: return
        val members = store.groupMembers(group.optString("id")).mapNotNull { fp ->
            val alias = store.device(fp)?.optString("alias")?.takeIf { it.isNotEmpty() }
            if (fp == fingerprint) null else alias?.let { it to fp }
        }.distinctBy { it.first }
        if (members.isEmpty()) {
            toast("群里还没有其他成员")
            return
        }
        val names = members.map { it.first }.toTypedArray()
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("选择提醒谁")
            .setItems(names) { _, which ->
                insertMention(members[which].first)
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun insertMention(alias: String) {
        val start = inputEdit.selectionStart.coerceAtLeast(0)
        inputEdit.text.insert(start, "@$alias ")
        inputEdit.setSelection(start + alias.length + 2)
    }

    private fun showPairDialog(prompt: Runtime.PairPrompt?) {
        if (prompt == null || isFinishing) return
        val name = store.device(prompt.peer)?.optString("alias")
            ?.takeIf { it.isNotEmpty() } ?: prompt.peer.take(8)
        var answered = false
        fun answer(ok: Boolean) {
            if (answered) return
            answered = true
            Runtime.answerPair(ok)
        }
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("配对请求")
            .setMessage("「$name」请求与这台设备配对。\n验证码：${prompt.code.ifEmpty { "—" }}\n" +
                "请与对方核对后再接受。")
            .setPositiveButton("接受") { _, _ -> answer(true) }
            .setNegativeButton("拒绝") { _, _ -> answer(false) }
            .setOnCancelListener { answer(false) }
            .create()
            .apply {
                setOnDismissListener {
                    answer(false)
                    if (pairDialog === this) pairDialog = null
                }
                pairDialog = this
            }
            .show()
    }

    private fun pairCurrent() {
        val peer = requirePeer() ?: return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val myKey = store.ownPairKey(prefs)
        Thread {
            try {
                val (code, msg) = ChannelClient.postIntent(target.first, target.second,
                    Wire.pairIntent(fingerprint, peer.optString("fingerprint"), myKey))
                mainHandler.post {
                    if (code == 0) {
                        store.addPair(peer.optString("fingerprint"), myKey)
                        pairButton.visibility = View.GONE
                        toast("已配对 $msg")
                    } else toast("配对被拒：$msg")
                }
            } catch (e: Exception) {
                mainHandler.post { toast("配对失败：${e.message}") }
            }
        }.start()
    }

    private fun sendText() {
        val text = inputEdit.text.toString().trim()
        if (text.isEmpty()) return
        val group = currentGroup
        if (group != null) {
            sendGroupText(group.optString("id"), text)
            return
        }
        val peer = requirePeer() ?: return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val fp = peer.optString("fingerprint")
        val bubble = Wire.textBubble(fingerprint, fp, text)
        val id = bubble.optString("id")
        inputEdit.setText("")
        store.addMessage(id, fingerprint, fp, Wire.TYPE_TEXT,
            bubble.optLong("time"), "", text = text, status = 1)
        refreshMessages()
        Thread {
            val ok = postTextWithRetry(target.first, target.second, bubble)
            store.setMessageStatus(id, if (ok) 0 else 2)
            mainHandler.post {
                refreshMessages()
                refreshDevices()
                if (!ok) toast("发送失败，点按红色气泡可重试")
            }
        }.start()
    }

    /** One bubble id fanned out to every online member (they dedupe by id). */
    private fun sendGroupText(gid: String, text: String) {
        if (store.isGroupLeft(gid)) {
            toast("已退出该群，不能再发送")
            return
        }
        val id = Wire.newId()
        val time = System.currentTimeMillis()
        inputEdit.setText("")
        store.addMessage(id, fingerprint, "", Wire.TYPE_TEXT, time, gid,
            text = text, status = 1)
        refreshMessages()
        val targets = groupTargets(gid)
        if (targets.isEmpty()) {
            store.setMessageStatus(id, 2)
            refreshMessages()
            toast("没有在线成员，点按红色气泡可重试")
            return
        }
        Thread {
            var delivered = 0
            for ((fp, ip, port) in targets) {
                val bubble = Wire.textBubble(fingerprint, fp, text, gid, id = id, time = time)
                if (postTextWithRetry(ip, port, bubble)) delivered++
            }
            val ok = delivered > 0
            store.setMessageStatus(id, if (ok) 0 else 2)
            mainHandler.post {
                refreshMessages()
                refreshGroups()
                if (!ok) toast("发送失败，点按红色气泡可重试")
                else if (delivered < targets.size) toast("已送达 $delivered/${targets.size} 位成员")
            }
        }.start()
    }

    /** Initial attempt plus 3 retries at 0.5s/1.5s/4s backoff (v2A brief §3-7). */
    private fun postTextWithRetry(ip: String, port: Int, bubble: JSONObject): Boolean {
        val backoffs = longArrayOf(0, 500, 1500, 4000)
        for ((attempt, delay) in backoffs.withIndex()) {
            if (delay > 0) Thread.sleep(delay)
            try {
                ChannelClient.postBubble(ip, port, bubble,
                    timeoutMs = if (attempt == 0) 8000 else 5000)
                return true
            } catch (_: Exception) {
                // fall through to the next attempt
            }
        }
        return false
    }

    private fun retryText(msg: JSONObject) {
        val id = msg.optString("id")
        val text = msg.optString("text")
        val group = currentGroup
        if (group != null) {
            val gid = group.optString("id")
            if (store.isGroupLeft(gid)) {
                toast("已退出该群，不能再发送")
                return
            }
            val targets = groupTargets(gid)
            store.setMessageStatus(id, 1)
            refreshMessages()
            if (targets.isEmpty()) {
                store.setMessageStatus(id, 2)
                refreshMessages()
                toast("仍没有在线成员")
                return
            }
            Thread {
                var delivered = 0
                for ((fp, ip, port) in targets) {
                    val bubble = Wire.textBubble(fingerprint, fp, text, gid,
                        id = id, time = msg.optLong("time"))
                    if (postTextWithRetry(ip, port, bubble)) delivered++
                }
                store.setMessageStatus(id, if (delivered > 0) 0 else 2)
                mainHandler.post { refreshMessages(); refreshGroups() }
            }.start()
            return
        }
        val peer = requirePeer() ?: return
        val target = resolve(peer) ?: run { toast("对端无地址"); return }
        val bubble = Wire.textBubble(fingerprint, peer.optString("fingerprint"), text,
            id = id, time = msg.optLong("time"))
        store.setMessageStatus(id, 1)
        refreshMessages()
        Thread {
            val ok = postTextWithRetry(target.first, target.second, bubble)
            store.setMessageStatus(id, if (ok) 0 else 2)
            mainHandler.post { refreshMessages(); refreshDevices() }
        }.start()
    }

    // ---------- message & conversation operations (v2D) ----------

    /** Long-press menu on a bubble: copy / open / resend / delete one. */
    private fun showMessageMenu(msg: JSONObject) {
        val outgoing = msg.optString("from") == fingerprint
        val isText = msg.optInt("type") == Wire.TYPE_TEXT
        val hasPath = msg.optString("filePath").isNotEmpty()
        val imageMsg = !isText && Media.isImage(msg.optString("fileName"))
        val labels = mutableListOf<String>()
        val actions = mutableListOf<() -> Unit>()
        if (isText) {
            labels += "复制文字"
            actions += { copyText(msg.optString("text")) }
        } else if (imageMsg && hasPath) {
            labels += "查看大图"
            actions += { openImagePreview(msg) }
            labels += "保存到相册"
            actions += { saveImageToDownloads(msg) }
        } else if (hasPath) {
            labels += "打开文件"
            actions += { openFile(msg.optString("filePath")) }
            labels += "另存为…"
            actions += { saveAsDialog(msg) }
        }
        if (outgoing && failed(outgoing, msg.optInt("status"))) {
            labels += "重新发送"
            actions += { retryText(msg) }
        }
        labels += "删除这条"
        actions += { confirmDeleteMessage(msg) }
        AlertDialog.Builder(this, dlgTheme())
            .setTitle(if (isText) "消息" else msg.optString("fileName"))
            .setItems(labels.toTypedArray()) { _, which -> actions[which]() }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun confirmDeleteMessage(msg: JSONObject) {
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("删除这条消息？")
            .setMessage("只删除本机记录，对方不会受影响，且不可恢复。")
            .setPositiveButton("删除") { _, _ ->
                store.deleteMessage(msg.optString("id"))
                refreshMessages()
                refreshDevices()
                refreshGroups()
                toast("已删除")
            }
            .setNegativeButton("取消", null)
            .show()
            .danger()
    }

    /** Long-press on a conversation row: pin / mark read / delete. */
    private fun showConversationMenu(position: Int) {
        if (listMode == MODE_GROUPS) {
            val group = groupsAdapter.item(position) ?: return
            val gid = group.optString("id")
            val pinned = group.optInt("pinned") > 0
            val labels = arrayOf(if (pinned) "取消置顶" else "置顶", "标为已读", "删除会话")
            AlertDialog.Builder(this, dlgTheme())
                .setTitle(group.optString("name").ifEmpty { "群聊" })
                .setItems(labels) { _, which ->
                    when (which) {
                        0 -> {
                            store.setGroupPinned(gid, !pinned)
                            refreshGroups()
                            toast(if (pinned) "已取消置顶" else "已置顶")
                        }
                        1 -> {
                            markGroupRead(gid)
                            refreshGroups()
                            toast("已标为已读")
                        }
                        else -> confirmDeleteGroup(group)
                    }
                }
                .setNegativeButton("取消", null)
                .show()
            return
        }
        val dev = devicesAdapter.item(position) ?: return
        val fp = dev.optString("fingerprint")
        val pinned = dev.optInt("pinned") > 0
        val labels = arrayOf(if (pinned) "取消置顶" else "置顶", "标为已读", "删除聊天记录")
        AlertDialog.Builder(this, dlgTheme())
            .setTitle(dev.optString("alias").ifEmpty { fp.take(8) })
            .setItems(labels) { _, which ->
                when (which) {
                    0 -> {
                        setPinnedForMachine(dev, !pinned)
                        refreshDevices()
                        toast(if (pinned) "已取消置顶" else "已置顶")
                    }
                    1 -> {
                        markRead(fp)
                        refreshDevices()
                        toast("已标为已读")
                    }
                    else -> confirmDeletePeer(dev)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun confirmDeletePeer(dev: JSONObject) {
        val fp = dev.optString("fingerprint")
        // One machine may hold history under several fingerprints; drop them all
        // so the conversation cannot reappear under a stale identity.
        val machines = machineRows(dev, store.devices()).map { it.optString("fingerprint") }
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("删除聊天记录？")
            .setMessage("只删除本机记录，对端不受影响，且不可恢复。")
            .setPositiveButton("删除") { _, _ ->
                val edit = prefs.edit()
                for (peer in machines.ifEmpty { listOf(fp) }) {
                    store.deleteConversation(peer)
                    edit.remove("lastread_$peer")
                }
                edit.apply()
                refreshDevices()
                syncEmptyState()
                toast("已删除")
            }
            .setNegativeButton("取消", null)
            .show()
            .danger()
    }

    private fun confirmDeleteGroup(group: JSONObject) {
        val gid = group.optString("id")
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("删除群会话？")
            .setMessage("本机记录会全部删除，并从本机移除这个群；其他成员不受影响。")
            .setPositiveButton("删除") { _, _ ->
                store.deleteGroup(gid)
                prefs.edit().remove("lastread_group_$gid").apply()
                if (currentGroup?.optString("id") == gid) closeChat()
                refreshGroups()
                syncEmptyState()
                toast("已删除")
            }
            .setNegativeButton("取消", null)
            .show()
            .danger()
    }

    /** After an identity merge the open chat has to follow the new fingerprint. */
    private fun remapCurrentPeer() {
        val peer = currentPeer ?: return
        val fp = peer.optString("fingerprint")
        val all = store.devices()
        if (all.any { it.optString("fingerprint") == fp }) return
        val moved = all.firstOrNull {
            it.optString("alias") == peer.optString("alias") &&
                it.optString("ip") == peer.optString("ip")
        } ?: return
        currentPeer = moved
        Runtime.foregroundPeer = moved.optString("fingerprint")
        refreshMessages()
    }

    /** ✓ in the list header: every conversation becomes read at once. */
    private fun markAllRead() {
        val all = store.devices()
        // Old fingerprints of a re-created identity carry history too.
        val rows = dedupeDevices(all).flatMap { machineRows(it, all) }
            .map { it.optString("fingerprint") }.toSet()
        for (fp in rows) markRead(fp)
        for (g in store.groups()) markGroupRead(g.optString("id"))
        refreshDevices()
        refreshGroups()
        toast("已全部标为已读")
    }

    private fun isPinnedPeer(fp: String): Boolean = prefs.getBoolean("pinned_$fp", false)

    /** Pin/unpin every fingerprint row of the same machine (alias + IP). */
    private fun setPinnedForMachine(dev: JSONObject, pinned: Boolean) {
        val edit = prefs.edit()
        for (member in machineRows(dev, store.devices())) {
            edit.putBoolean("pinned_${member.optString("fingerprint")}", pinned)
        }
        edit.apply()
    }

    private fun copyText(text: String) {
        getSystemService(ClipboardManager::class.java)
            .setPrimaryClip(ClipData.newPlainText("relaydesk", text))
        toast("已复制")
    }

    // ---------- settings (v2D) ----------

    private fun openSettings() {
        refreshSettings()
        settingsScreen.visibility = View.VISIBLE
        listScreen.visibility = View.GONE
    }

    private fun closeSettings() {
        settingsScreen.visibility = View.GONE
        listScreen.visibility = View.VISIBLE
        refreshServiceState()
    }

    private fun addSection(title: String) {
        settingsBody.addView(TextView(this).apply {
            text = title
            setTextColor(getColor(R.color.textMuted)); textSize = 11f
            setPadding(dp(6), dp(16), dp(6), dp(6))
        })
    }

    private fun addRow(title: String, value: String, enabled: Boolean = true,
                       valueColor: Int = getColor(R.color.textMuted),
                       onClick: (() -> Unit)? = null) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = roundRect(getColor(R.color.bgPanel), dp(12))
            setPadding(dp(14), dp(14), dp(14), dp(14))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { bottomMargin = dp(8) }
        }
        row.addView(TextView(this).apply {
            text = title
            setTextColor(if (enabled) getColor(R.color.textPrimary) else getColor(R.color.textMuted))
            textSize = 14f
            layoutParams = LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        })
        row.addView(TextView(this).apply {
            text = value
            setTextColor(if (enabled) valueColor else getColor(R.color.textMuted))
            textSize = 12f; gravity = Gravity.END
            maxWidth = dp(200); maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        })
        if (onClick != null && enabled) {
            row.isClickable = true
            row.setOnClickListener { onClick() }
        }
        settingsBody.addView(row)
    }

    /** Rebuilt on every open so port / IME / alias are never stale. */
    private fun refreshSettings() {
        settingsBody.removeAllViews()

        addSection("本机")
        addRow("名称", alias) { editAliasDialog() }
        addRow("设备号", fingerprint.take(8)) { copyText(fingerprint) }
        addRow("配对二维码", "查看 ›") { showQrDialog() }

        addSection("通道")
        addRow("后台接收", if (Runtime.serviceRunning) "运行中" else "未运行")
        addRow("监听端口", if (Runtime.servicePort > 0) Runtime.servicePort.toString() else "—")
        addRow("本机地址", localIpv4().ifEmpty { "未连接 Wi-Fi" })

        addSection("文件")
        addRow("保存目录", "Download/RelayDesk") { copyText(fileSaveDir().absolutePath) }

        addSection("提醒")
        val notifyOn = prefs.getBoolean("notify_enabled", true)
        addRow("消息通知", if (notifyOn) "已开启" else "已关闭",
            valueColor = if (notifyOn) getColor(R.color.accent)
                         else getColor(R.color.textMuted)) {
            prefs.edit().putBoolean("notify_enabled", !notifyOn).apply()
            refreshSettings()
            toast(if (notifyOn) "通知已关闭" else "通知已开启")
        }

        addSection("输入法与显示")
        addRow("当前输入法", imeLabel())
        addRow("实体键盘", if (hasHardwareKeyboard()) "已连接" else "未连接")
        addRow("切换输入法", "系统设置 ›") { openImeSettings() }
        val lightTheme = wantsLightTheme()
        addRow("主题", if (lightTheme) "浅色" else "深色") { switchTheme(!lightTheme) }

        addSection("关于")
        addRow("版本", versionName())
        addRow("连接方式", "局域网直连 · 无需账号")
    }

    private fun editAliasDialog() {
        val input = EditText(this).apply {
            setText(alias); setSingleLine()
            setTextColor(getColor(R.color.textPrimary)); setHintTextColor(getColor(R.color.textMuted))
            hint = "本机名称"
            setPadding(dp(20), dp(10), dp(20), dp(10))
        }
        AlertDialog.Builder(this, dlgTheme())
            .setTitle("修改本机名称")
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isEmpty()) {
                    toast("名称不能为空")
                } else {
                    Identity.setAlias(this, name)
                    alias = name
                    Runtime.service?.announceNow()
                    refreshSettings()
                    refreshServiceState()
                    toast("已更新，对端重新发现后显示新名称")
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun fileSaveDir(): java.io.File = java.io.File(
        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
        "RelayDesk")

    private fun versionName(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "—"
    } catch (_: Exception) {
        "—"
    }

    private fun imeLabel(): String {
        val id = Settings.Secure.getString(contentResolver,
            Settings.Secure.DEFAULT_INPUT_METHOD).orEmpty()
        if (id.isEmpty()) return "—"
        // The system's own list is authoritative and needs no <queries>.
        val fromSystem = getSystemService(InputMethodManager::class.java)
            ?.enabledInputMethodList?.firstOrNull { it.id == id }
        val label = runCatching { fromSystem?.loadLabel(packageManager)?.toString() }
            .getOrNull()
        if (!label.isNullOrEmpty()) return label
        return runCatching {
            packageManager.getApplicationLabel(
                packageManager.getApplicationInfo(id.substringBefore('/'), 0)).toString()
        }.getOrNull() ?: id.substringBefore('/')
    }

    /** The magnetic keyboard reports as a hardware keyboard — worth showing. */
    private fun hasHardwareKeyboard(): Boolean =
        resources.configuration.keyboard != Configuration.KEYBOARD_NOKEYS

    private fun openImeSettings() {
        val opened = runCatching {
            startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS))
            true
        }.getOrDefault(false)
        if (!opened) {
            runCatching { startActivity(Intent(Settings.ACTION_SETTINGS)) }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            ::settingsScreen.isInitialized && settingsScreen.visibility == View.VISIBLE ->
                closeSettings()
            chatScreen.visibility == View.VISIBLE -> closeChat()
            else -> super.onBackPressed()
        }
    }

    private fun attachFile() {
        if (!hasChatTarget()) return
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
        }
        startActivityForResult(Intent.createChooser(intent, "选择要发送的文件"), ATTACH_REQUEST)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == QR_SCAN_REQUEST) {
            val payload = data?.getStringExtra(QrScanActivity.EXTRA_RESULT)
            if (resultCode == RESULT_OK && !payload.isNullOrEmpty()) handleScannedCode(payload)
            return
        }
        if (requestCode == SAVE_AS_REQUEST) {
            val target = data?.data
            val msg = saveAsMessage
            saveAsMessage = null
            if (resultCode == RESULT_OK && target != null && msg != null) copyToUri(msg, target)
            return
        }
        if (requestCode != ATTACH_REQUEST || resultCode != RESULT_OK ||
            data?.data == null) return
        val group = currentGroup
        if (group != null && store.isGroupLeft(group.optString("id"))) {
            toast("已退出该群，不能再发送")
            return
        }
        val peer = if (group == null) requirePeer() ?: return else null
        val uri = data.data!!
        Thread {
            var name = "file"
            var size = 0L
            var rid: String? = null
            try {
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
                val newRid = Wire.newId()
                rid = newRid
                val mime = contentResolver.getType(uri) ?: "application/octet-stream"
                // v2E: photos are compressed and thumbnailed before upload, and
                // every outgoing file gets a cache copy so a retry does not depend
                // on how long the picker's URI grant lasts.
                val prepared = if (mime.startsWith("image/"))
                    Media.prepareImage(this@MainActivity, uri, newRid, name, size) else null
                val localFile = prepared?.file
                    ?: Media.copyToCache(this@MainActivity, uri, newRid, name)
                val sendName = prepared?.name ?: name
                val sendSize = localFile.length()
                if (sendSize <= 0) throw IllegalStateException("文件为空")
                if (group != null) {
                    val gid = group.optString("id")
                    store.addMessage(newRid, fingerprint, "", Wire.TYPE_FILE,
                        System.currentTimeMillis(), gid, fileName = sendName,
                        fileSize = sendSize, status = 1,
                        filePath = localFile.absolutePath)
                    mainHandler.post { refreshMessages() }
                    val targets = groupTargets(gid)
                    var delivered = 0
                    for ((fp, ip, port) in targets) {
                        try {
                            ChannelClient.postBubble(ip, port,
                                Wire.fileBubble(fingerprint, fp, newRid, sendName, mime,
                                    sendSize, gid))
                            java.io.FileInputStream(localFile).use { input ->
                                ChannelClient.uploadFile(ip, port, newRid, sendName,
                                    sendSize, input) { sent ->
                                    live[newRid] = JSONObject().put("rid", newRid)
                                        .put("got", sent).put("total", sendSize)
                                        .put("done", sent >= sendSize)
                                    mainHandler.post { refreshMessages() }
                                }
                            }
                            delivered++
                        } catch (_: Exception) {
                            // one unreachable member must not abort the rest
                        }
                    }
                    val ok = delivered > 0
                    store.setMessageStatus(newRid, if (ok) 0 else 2)
                    mainHandler.post {
                        refreshMessages(); refreshGroups()
                        toast(if (ok) "文件已发送给 $delivered 位成员" else "没有在线成员，文件未送达")
                    }
                    return@Thread
                }
                val target = resolve(peer!!) ?: throw IllegalStateException("对端无地址")
                val fp = peer.optString("fingerprint")
                val bubble = Wire.fileBubble(fingerprint, fp, newRid, sendName, mime, sendSize)
                store.addMessage(newRid, fingerprint, fp, Wire.TYPE_FILE,
                    bubble.optLong("time"), "", fileName = sendName, fileSize = sendSize,
                    filePath = localFile.absolutePath, status = 1)
                mainHandler.post { refreshMessages() }
                ChannelClient.postBubble(target.first, target.second, bubble)
                java.io.FileInputStream(localFile).use { input ->
                    ChannelClient.uploadFile(target.first, target.second, newRid, sendName,
                        sendSize, input) { sent ->
                        live[newRid] = JSONObject().put("rid", newRid)
                            .put("got", sent).put("total", sendSize)
                            .put("done", sent >= sendSize)
                        mainHandler.post { refreshMessages() }
                    }
                }
                store.setMessageStatus(newRid, 0)
                mainHandler.post {
                    refreshMessages(); refreshDevices(); toast("文件已发送")
                }
            } catch (e: Exception) {
                android.util.Log.e("RD", "file send failed", e)
                rid?.let { store.setMessageStatus(it, 2) }
                mainHandler.post {
                    refreshMessages()
                    toast("文件发送失败：${e.message}")
                }
            }
        }.start()
    }
}
