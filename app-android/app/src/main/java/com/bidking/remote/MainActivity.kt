package com.bidking.remote

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.graphics.Color
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.ImageView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlin.concurrent.thread
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {

    private data class ServerTab(val name: String, val url: String, val pwd: String = "")
    // 取当前 active 服务器保存的密码（多服务器各自独立）
    private fun activeTabPwd(): String = tabs.find { it.url == activeUrl }?.pwd ?: ""
    // 当前连接的服务器显示名（通知里标明消息来自哪台解析工具，2026-08-30）
    private fun activeTabName(): String = tabs.find { it.url == activeUrl }?.name ?: ""

    /* ================= 令牌：按服务器隔离 + 加密持久化（2026-08-31 P1）=================
       目标：① 切后台 / 进程被回收后回来不用重新登录；② 多服务器各自持有令牌互不覆盖。
       存储：与服务器密码同级，写进 EncryptedSharedPreferences（initEncryptedPrefs）。
       ⚠️ 生命周期：令牌是 exe 的进程内存态，exe 一重启就全部失效（网6），所以持久化
       只用于「先拿旧令牌轻轻探一次，401 再重新登录」，绝不假定它一定有效。 */
    private fun tokenKeyOf(server: String): String {
        // 去掉协议头、把 : 和 . 换成安全字符，避开特殊字符进 key（参照 devKeyOf 的思路）
        val n = normalizeServerUrl(server)
        return "tok_" + n.removePrefix("http://").removePrefix("https://")
            .replace(":", "_").replace(".", "-")
    }
    private fun tokenFor(url: String): String = tokenMap[normalizeServerUrl(url)] ?: ""

    private fun setToken(url: String, tk: String, scope: String = "", paired: Boolean = false) {
        val k = normalizeServerUrl(url)
        if (tk.isEmpty()) {
            tokenMap.remove(k); tokenScope.remove(k); tokenPaired.remove(k)
            try { prefs.edit().remove(tokenKeyOf(k)).apply() } catch (_: Exception) {}
            return
        }
        tokenMap[k] = tk
        if (scope.isNotEmpty()) tokenScope[k] = scope
        // 已配对状态只增不减：后续用密码续期拿不到 paired 标记时不要把它抹掉
        tokenPaired[k] = paired || (tokenPaired[k] == true)
        try {
            prefs.edit().putString(tokenKeyOf(k), JSONObject()
                .put("t", tk)
                .put("scope", tokenScope[k] ?: "")
                .put("paired", tokenPaired[k] == true)
                .put("ts", System.currentTimeMillis()).toString()).apply()
        } catch (_: Exception) {}
        writeTokenCookie(k, tk)     // 令牌与 WebView Cookie 永远同步（Cookie 按 origin 天然隔离）
    }

    /** 把令牌写进 WebView Cookie：页面发出的第一个请求就带令牌，消灭「页面先请求、
     *  令牌后注入」的 401 竞态（安1）。Cookie 是应用级存储，WebView 被 LRU 销毁 /
     *  进程重启后依然在，所以这里是幂等的补写。 */
    private fun writeTokenCookie(url: String, tk: String) {
        val k = normalizeServerUrl(url)
        if (k.isEmpty() || tk.isEmpty()) return
        try {
            val cm = android.webkit.CookieManager.getInstance()
            cm.setAcceptCookie(true)
            cm.setCookie(k, "bk_token=$tk; Path=/")
            cm.flush()
        } catch (_: Exception) {}
    }

    /** 冷启动：把落盘的令牌读回内存并补写 Cookie（onCreate 里 loadTabs() 之后调用） */
    private fun loadTokens() {
        tabs.forEach { t ->
            val k = normalizeServerUrl(t.url)
            val raw = try { prefs.getString(tokenKeyOf(k), "") ?: "" } catch (_: Exception) { "" }
            if (raw.isEmpty()) return@forEach
            val o = try { JSONObject(raw) } catch (_: Exception) { return@forEach }
            val tk = o.optString("t", "")
            if (tk.isEmpty()) return@forEach
            tokenMap[k] = tk
            tokenScope[k] = o.optString("scope", "")
            tokenPaired[k] = o.optBoolean("paired", false)
            writeTokenCookie(k, tk)
        }
    }

    private fun scopeSuffix(url: String): String {
        val k = normalizeServerUrl(url)
        return if (tokenScope[k] == "legacy-readonly") " · 只读（公网未配对）"
               else if (tokenPaired[k] == true) " · 已配对"
               else ""
    }

    private lateinit var prefs: SharedPreferences
    private val tabs = mutableListOf<ServerTab>()
    // WebView 池：accessOrder=true，超过上限时淘汰最久未访问的（内存优化 P1）
    private val webViews = object : LinkedHashMap<String, WebView>(16, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, WebView>): Boolean {
            if (size <= MAX_WEBVIEWS) return false
            // 不销毁当前正在显示的 WebView；用显式引用判断，避免 showWebHostHint 占位时误判
            if (eldest.value !== currentWv) {
                try { eldest.value.destroy() } catch (_: Exception) {}
                return true
            }
            return false
        }
    }
    private val MAX_WEBVIEWS = 4
    private var currentWv: WebView? = null   // 当前正在显示的 WebView，LRU 淘汰时保护它
    // P2：电脑端重新解析过、但当时没在看的服务器。切回去时才刷新，
    // 避免给后台页面做无谓的整页重载（用户看不到，还丢滚动位置）。
    private val dirtyServers = mutableSetOf<String>()

    @Volatile private var activeUrl = ""
    /* 令牌按服务器隔离后的只读视图（2026-08-31 P1）：
       全文十几处「读令牌」的地方一行都不用改，写入一律改走 setToken(url, token)。
       原来是单个内存变量 authToken —— 进程被系统回收后必丢（切后台回来只能重新登录），
       而且多服务器共用一个变量，切 tab 会互相覆盖（安16）。 */
    private val authToken: String get() = tokenFor(activeUrl)
    private val tokenMap = java.util.concurrent.ConcurrentHashMap<String, String>()      // 归一URL -> 令牌
    private val tokenScope = java.util.concurrent.ConcurrentHashMap<String, String>()    // 归一URL -> scope
    private val tokenPaired = java.util.concurrent.ConcurrentHashMap<String, Boolean>()  // 归一URL -> 是否已配对
    private var passEnabled = false
    @Volatile private var isConnecting = false   // 防重入：避免 onCreate/onResume/网络回调并发重复连接
    private val notifying = AtomicBoolean(false)
    @Volatile private var lowStockRunning = false
    @Volatile private var autoSyncRunning = false       // exe 端刷新后 App 自动同步（问题 1）
    private var lastSyncState = ""            // 上次 /api/status 的 state 摘要
    private var lastBackPressed = 0L          // 上次按返回键时间（I1：再按一次退出防误关）
    private var netCallback: android.net.ConnectivityManager.NetworkCallback? = null  // 网络变化监听（C2 自动重连）

    /* ================= 状态栏分层（2026-08-31 P0）=================
       背景（安15）：启动估价器的看门狗曾把状态栏永久占成「启动中：idle」——
       checkLaunchStatus 在令牌为空 / JSON 解析失败时直接 return@Thread，既不停表
       也不复位文字，于是「已连接 / 连接失败：xxx」全被盖住，用户以为连不上，
       只能彻底退出 App 才恢复。
       修法：连接层与启动层分开存，启动层带 90 秒 TTL 自动作废（最后一道保险），
       两层同时有内容时拼接显示。所有状态写入一律走这两个 setter，
       不再直接写 statusView.text。 */
    @Volatile private var connStatusText = ""     // 连接层：已连接 / 连接失败：xxx / 正在恢复连接…
    @Volatile private var launchStatusText = ""   // 启动层：启动中：launching · 正在确认卡密
    private var launchStatusAt = 0L               // 启动层最后更新时间（TTL 兜底用）

    private fun setConnStatus(s: String) { connStatusText = s; renderStatus() }
    private fun setLaunchStatus(s: String) { launchStatusText = s; launchStatusAt = System.currentTimeMillis(); renderStatus() }
    /** 启动层收工：传 "" 表示把状态栏完整交还连接层 */
    private fun clearLaunchStatus() { launchStatusText = ""; renderStatus() }
    private fun renderStatus() {
        // 启动层超过 90 秒没更新就自动作废：哪怕某条代码路径漏了收工，也能自己恢复
        if (launchStatusText.isNotEmpty() && System.currentTimeMillis() - launchStatusAt > 90_000L) launchStatusText = ""
        val txt = when {
            launchStatusText.isEmpty() -> connStatusText
            connStatusText.isEmpty() -> launchStatusText
            else -> "$connStatusText ｜ $launchStatusText"
        }
        if (Looper.myLooper() == Looper.getMainLooper()) statusView.text = txt
        else runOnUiThread { statusView.text = txt }
    }

    // UI
    private lateinit var bottomBar: LinearLayout          // 底部导航（含功能条 + 服务器 tab）
    private lateinit var funcBar: HorizontalScrollView    // 底部功能条（报表页功能入口）
    private lateinit var webHost: FrameLayout             // WebView 宿主
    private lateinit var webProgress: ProgressBar         // 加载进度条
    private lateinit var statusView: TextView
    private lateinit var refreshBtn: TextView  // 顶栏「⟳ 重新连接」按钮（2026-08-30 用户要求）
    private lateinit var notifyBtn: TextView  // 顶栏「🔔 通知」按钮（TextView 替代 Button 以精确控制尺寸）
    private lateinit var themeBtn: TextView   // 顶栏「☀/🌙」深色切换按钮
    private var darkMode = false            // 是否深色模式（App 原生 UI + 网页全局）
    private lateinit var rootView: FrameLayout        // 根布局（深色统一着色）
    private lateinit var titleLabel: TextView         // 标题文字（深色改 ink）
    private var profitInverted = false        // 盈亏配色是否反转（页面 CSS 语义：inverted=红盈绿亏，normal=绿盈红亏）
    private lateinit var profitBtn: TextView     // 顶栏「红盈/绿盈」盈亏配色切换按钮

    // 底部弹层
    private lateinit var sheet: LinearLayout
    private lateinit var sheetTitle: TextView
    private lateinit var sheetNameInput: EditText
    private lateinit var sheetUrlInput: EditText
    private lateinit var sheetPwdInput: EditText
    private lateinit var sheetPrimaryBtn: Button
    private lateinit var sheetDangerBtn: Button
    private lateinit var sheetCancelBtn: Button
    private var sheetMode = "conf"   // conf=配置 | add=添加

    companion object {
        private const val PREFS = "bidking_prefs"
        private const val KEY_TABS = "servers"
        private const val KEY_PROFIT_INVERTED = "profit_inverted"
        // 设备配对（2026-08-29 新增）：device_id 全局唯一；每台电脑的密钥分开存
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_DEVKEY_PREFIX = "devkey_"
        private const val CHANNEL_ID = "bidking_lowstock"
        private const val LAUNCH_CHANNEL_ID = "bidking_launcher"   // 远程启动估价器的结果提醒
        private const val LOW = 10
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = initEncryptedPrefs()
        createNotificationChannel()
        loadTabs()
        loadTokens()     // P1：把上次登录的令牌从加密存储读回内存（按服务器各自独立）
        buildUi()
        loadDarkModePref()   // 读取并应用全局深色（含 WebView 注入）
        loadProfitPref()     // 读取并应用盈亏配色（红盈绿亏 / 绿盈红亏）
        if (tabs.isNotEmpty()) {
            activeUrl = tabs.first().url
            // P1：冷启动优先用落盘令牌轻探（/api/status 200 即免登录），
            // 令牌失效（如电脑端 exe 重启过）才回退完整登录。旧实现无条件
            // connect() —— 强杀/被系统回收后重开必走一整轮密码登录。
            resumeOrLogin(activeUrl, tabs.first().pwd)
        } else {
            setConnStatus("点击下方「＋」添加第一个解析工具地址")
        }
        setupNetworkMonitor()   // 监听网络变化，断网恢复自动重连（C2）
        // 电池优化引导（N2）：延迟弹出，避免打断初始加载
        window.decorView.postDelayed({ maybeRequestBatteryOptim() }, 2000)
    }

    /** 从后台返回 / 冷启动：原则是「默认什么都不做」。
     *  2026-08-31 P1：令牌已按服务器加密落盘，只有三种情况才需要网络动作：
     *    ① 这台服务器没有令牌（首次 / 令牌被清）→ 用落盘令牌轻探，不行再完整登录；
     *    ② 页面被 LRU 淘汰了（要重建 WebView）→ 同上；
     *    ③ 其余（最常见）→ 只做一次后台静默探测，200 就连状态栏都不改。
     *  轮询线程无论哪种情况都要确保还活着（后台可能被系统停掉）。 */
    override fun onResume() {
        super.onResume()
        if (activeUrl.isEmpty()) return
        val key = normalizeServerUrl(activeUrl)
        when {
            tokenFor(activeUrl).isEmpty() -> resumeOrLogin(activeUrl, activeTabPwd())
            !webViews.containsKey(key)    -> resumeOrLogin(activeUrl, activeTabPwd())
            else                          -> silentProbe(activeUrl)
        }
        if (!autoSyncRunning) startAutoSyncMonitor()
        if (!lowStockRunning) startLowStockMonitor()
    }

    /* ================= 界面（exe 报表风格：米色纸感 #F4F1EA + 墨绿 #1F7667 + 暖橙 #A55B2A） ================= */
    private fun buildUi() {
        rootView = FrameLayout(this).apply { setBackgroundColor(if (darkMode) 0xFF12171F.toInt() else 0xFFF4F1EA.toInt()) }

        // 中部容器：标题栏 + WebView 占满
        val mid = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(10), dp(14), 0)
        }
        // 标题栏：左侧标题 + 右侧「🔔 通知」按钮（系统消息提示：低库存通知权限引导）
        val titleRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        titleLabel = TextView(this).apply {
            // 版本署名（2026-08-30）：标题带版本号（读自打包配置）；完整署名见提示卡片
            val ver = try {
                packageManager.getPackageInfo(packageName, 0).versionName ?: ""
            } catch (_: Exception) { "" }
            text = "BidKing 远程助手" + (if (ver.isNotEmpty()) " v$ver" else "")
            textSize = 15f
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            setTypeface(null, android.graphics.Typeface.BOLD)
            setTextColor(0xFF1B2430.toInt())
        }
        titleRow.addView(titleLabel, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        // 「⟳ 重新连接」按钮（2026-08-30）：一键重连当前服务器，免去切底部服务器名重连
        refreshBtn = TextView(this).apply {
            text = "⟳"
            textSize = 15f
            gravity = Gravity.CENTER
            setTextColor(0xFF1B2430.toInt())
            background = roundRectDrawable(0xFFE7E0D2.toInt(), 8f)
            addPressFeedback(this, 0.88f)
            setOnClickListener {
                if (isConnecting) { toast("正在连接中，请稍候"); return@setOnClickListener }
                if (activeUrl.isEmpty()) { toast("还没有可连接的服务器"); return@setOnClickListener }
                toast("正在重新连接…")
                setToken(activeUrl, "")     // 手动重连：主动丢弃旧令牌，走完整登录
                connect(activeUrl, activeTabPwd())
            }
        }
        titleRow.addView(refreshBtn, LinearLayout.LayoutParams(dp(34), dp(30)).apply {
            rightMargin = dp(3)
        })
        themeBtn = TextView(this).apply {
            text = "☀"
            textSize = 13f
            gravity = Gravity.CENTER
            setTextColor(0xFF1B2430.toInt())
            background = roundRectDrawable(0xFFE7E0D2.toInt(), 8f)
            addPressFeedback(this, 0.88f)
            setOnClickListener { toggleDarkMode() }
        }
        titleRow.addView(themeBtn, LinearLayout.LayoutParams(dp(34), dp(30)).apply {
            rightMargin = dp(3)
        })
        // 盈亏配色切换按钮：红盈绿亏 ⇄ 绿盈红亏（默认绿盈红亏，与 exe 网页默认一致）
        profitBtn = TextView(this).apply {
            text = "红盈"
            textSize = 10f
            gravity = Gravity.CENTER
            setTextColor(0xFF1B2430.toInt())
            background = roundRectDrawable(0xFFE7E0D2.toInt(), 8f)
            addPressFeedback(this, 0.88f)
            setOnClickListener { toggleProfitColors() }
        }
        titleRow.addView(profitBtn, LinearLayout.LayoutParams(dp(34), dp(30)).apply {
            rightMargin = dp(3)
        })
        notifyBtn = TextView(this).apply {
            text = "🔔"
            textSize = 14f
            gravity = Gravity.CENTER
            setTextColor(0xFFFFFFFF.toInt())
            background = roundRectDrawable(0xFF1F7667.toInt(), 8f)
            addPressFeedback(this, 0.88f)
            setOnClickListener { requestNotifyPermission() }
        }
        titleRow.addView(notifyBtn, LinearLayout.LayoutParams(dp(34), dp(30)))
        mid.addView(titleRow)
        statusView = TextView(this).apply {
            text = "未连接"
            textSize = 11f
            setTextColor(0xFF69717D.toInt())
            setPadding(0, dp(2), 0, dp(6))
        }
        mid.addView(statusView)
        webHost = FrameLayout(this).apply {
            setPadding(0, 0, 0, 0)
        }
        // 加载进度条（悬在 WebView 顶部，慢速网络可见进度）
        webProgress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            visibility = View.GONE
            setBackgroundColor(0x00000000)
            progressDrawable = android.graphics.drawable.GradientDrawable().apply {
                shape = android.graphics.drawable.GradientDrawable.RECTANGLE
                setColor(0xFF1F7667.toInt())
                cornerRadius = 0f
            }
        }
        webHost.addView(webProgress, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, dp(4), Gravity.TOP))
        // 进度条压在内容上面需要提升层级
        webProgress.z = 10f
        mid.addView(webHost, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))

        rootView.addView(mid)

        // 底部功能条（报表页功能入口：高价值/统计图表/生涯统查/显示后三位/显示赢家/道具价格配置）
        funcBar = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            visibility = View.GONE   // 默认隐藏，仅在报表页显示
        }
        funcBar.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(6), dp(6), dp(6), dp(4))
        })

        // 底部导航栏（改为垂直：上方功能条 + 下方服务器 tab）
        bottomBar = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xFFFFFFFF.toInt())
            setPadding(dp(8), dp(4), dp(8), dp(8))
            elevation = dp(6).toFloat()
        }
        rootView.addView(bottomBar, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))

        // 底部弹层
        buildSheet(rootView)

        setContentView(rootView)
        renderBottomBar()
        syncWebHostBottomPadding()
    }

    // 让 WebView 内容底部让出底部导航栏高度，避免最底数据被遮住
    // 双 post：第一次触发 measure/layout，第二次读 height 才是最新值
    // （单 post 在 funcBar 刚切可见时可能读到旧高度，造成 padding 不足）
    private fun syncWebHostBottomPadding() {
        bottomBar.post {
            bottomBar.post {
                val h = bottomBar.height
                if (h > 0) webHost.setPadding(0, 0, 0, h)
            }
        }
    }

    // 统一控制功能条可见性，并在改变后刷新 webHost 底部 padding
    // （报告页功能条在 WebView 加载完成后才显示，会把 bottomBar 撑高，
    //  若不重算 padding，会出现"无对局数据"提示被底部栏遮挡）
    private fun updateFuncBarVisibility(visible: Boolean) {
        val target = if (visible) View.VISIBLE else View.GONE
        if (funcBar.visibility == target) return
        funcBar.visibility = target
        syncWebHostBottomPadding()
    }

    /* -------- 底部弹层 -------- */
    private fun buildSheet(root: FrameLayout) {
        sheet = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xFFFFFFFF.toInt())
            setPadding(dp(18), dp(14), dp(18), dp(18))
            visibility = View.GONE
            elevation = dp(14).toFloat()
        }
        sheetTitle = TextView(this).apply {
            textSize = 15f
            setTypeface(null, android.graphics.Typeface.BOLD)
            setTextColor(0xFF1B2430.toInt())
            setPadding(0, 0, 0, dp(8))
        }
        sheetTitle.layoutParams = FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT)
        sheet.addView(sheetTitle)

        sheetNameInput = EditText(this).apply {
            hint = "名称（如：家电脑 / ngrok公网）"
            setSingleLine(true)
            setTextColor(Color.BLACK)   // 需求：填写内容黑色（避免主题色看不清）
            minHeight = dp(48)
        }
        sheetUrlInput = EditText(this).apply {
            hint = "地址 http://192.168.x.x:8766 或 ngrok 域名"
            setSingleLine(true)
            setTextColor(Color.BLACK)
            minHeight = dp(48)
        }
        sheet.addView(labeled("名称", sheetNameInput))
        sheet.addView(labeled("地址", sheetUrlInput))
        sheet.addView(pwdLabeled())

        // 按钮竖排（手机上更易点按，避免三个按钮挤一行显示不全）
        val btnCol = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        sheetPrimaryBtn = Button(this).apply {
            text = "连接"
            isAllCaps = false
            minHeight = dp(46)
            textSize = 15f
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFF1F7667.toInt())
            setTextColor(0xFFFFFFFF.toInt())
            setOnClickListener { onPrimary() }
        }
        sheetDangerBtn = Button(this).apply {
            text = "删除"
            isAllCaps = false
            minHeight = dp(44)
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFA33D2A.toInt())
            setTextColor(0xFFFFFFFF.toInt())
            setOnClickListener { onDelete() }
        }
        sheetCancelBtn = Button(this).apply {
            text = "收起"
            isAllCaps = false
            minHeight = dp(44)
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFE7E0D2.toInt())
            setTextColor(0xFF1B2430.toInt())
            setOnClickListener { hideSheet() }
        }
        btnCol.addView(sheetPrimaryBtn, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        btnCol.addView(sheetDangerBtn, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(8)
        })
        btnCol.addView(sheetCancelBtn, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(8)
        })
        sheet.addView(btnCol)

        root.addView(sheet, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
    }

    private fun labeled(text: String, input: EditText): LinearLayout {
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(4), 0, dp(2)) }
        box.addView(TextView(this).apply {
            this.text = text; textSize = 11f; setTextColor(0xFF69717D.toInt())
        })
        // 输入框占满宽度，避免显示不全
        box.addView(input, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        return box
    }

    // 密码输入框：带「显示/隐藏」切换（输错了不用删光重输）
    private fun pwdLabeled(): LinearLayout {
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(4), 0, dp(2)) }
        box.addView(TextView(this).apply {
            text = "访问密码"; textSize = 11f; setTextColor(0xFF69717D.toInt())
        })
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        sheetPwdInput = EditText(this).apply {
            hint = "访问密码（电脑端设的，未设留空）"
            setSingleLine(true)
            setTextColor(Color.BLACK)   // 需求：填写内容黑色
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            minHeight = dp(48)
        }
        val toggle = Button(this).apply {
            text = "👁"
            isAllCaps = false
            textSize = 14f
            minHeight = dp(48)
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFE7E0D2.toInt())
            setTextColor(0xFF1B2430.toInt())
            setOnClickListener {
                val hidden = sheetPwdInput.inputType and InputType.TYPE_TEXT_VARIATION_PASSWORD != 0
                sheetPwdInput.inputType = if (hidden)
                    InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
                else
                    InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                sheetPwdInput.setSelection(sheetPwdInput.text?.length ?: 0)
            }
        }
        row.addView(sheetPwdInput, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(toggle, LinearLayout.LayoutParams(dp(50), LinearLayout.LayoutParams.WRAP_CONTENT).apply { leftMargin = dp(6) })
        box.addView(row, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        return box
    }

    private fun showConfSheet(tab: ServerTab) {
        sheetTitle.text = "⚙ ${tab.name}"
        sheetNameInput.setText(tab.name)
        sheetUrlInput.setText(tab.url)
        sheetPwdInput.setText(tab.pwd)
        sheetPwdInput.visibility = View.VISIBLE
        sheetDangerBtn.visibility = View.VISIBLE
        sheetPrimaryBtn.text = "保存并连接"
        sheetMode = "conf"
        sheet.visibility = View.VISIBLE
    }

    private fun showAddSheet() {
        sheetTitle.text = "＋ 添加解析工具"
        sheetNameInput.setText("")
        sheetUrlInput.setText("")
        sheetPwdInput.setText("")
        // 添加时也显示密码框（用户反馈看不到）：电脑端设过密码就在这里填好一起保存
        sheetPwdInput.visibility = View.VISIBLE
        sheetDangerBtn.visibility = View.GONE
        sheetPrimaryBtn.text = "添加"
        sheetMode = "add"
        sheet.visibility = View.VISIBLE
    }

    private fun hideSheet() { sheet.visibility = View.GONE }

    private fun onPrimary() {
        if (sheetMode == "add") {
            val name = sheetNameInput.text.toString().trim()
            val url = normalizeServerUrl(sheetUrlInput.text.toString())
            if (url.isEmpty()) { toast("请输入地址"); return }
            val cleanName = name.ifEmpty { url.removePrefix("http://").removePrefix("https://") }
            addTab(cleanName, url)
            hideSheet()
        } else {
            // 配置模式：允许修改名称/地址/密码并保存（用户反馈"已填写的配置打不开/改不了"）
            val newName = sheetNameInput.text.toString().trim()
            val newUrl = normalizeServerUrl(sheetUrlInput.text.toString())
            val pwd = sheetPwdInput.text.toString()
            val idx = tabs.indexOfFirst { it.url == activeUrl }
            if (idx >= 0) {
                val old = tabs[idx]
                val updated = old.copy(
                    name = newName.ifEmpty { old.name },
                    url = newUrl.ifEmpty { old.url },
                    pwd = pwd
                )
                // 地址变更时检查是否与其它 tab 重复
                if (updated.url != old.url && tabs.any { it.url == updated.url }) {
                    toast("该地址已在列表中")
                    return
                }
                tabs[idx] = updated
                // 地址变了：同步 activeUrl 和 WebView 缓存 key，旧 WebView 销毁
                if (updated.url != old.url) {
                    activeUrl = updated.url
                    webViews.remove(old.url)?.destroy()
                }
                saveTabs()
                renderBottomBar()
                connect(updated.url, pwd)
            }
            hideSheet()
        }
    }

    private fun onDelete() {
        if (sheetMode != "conf") return
        val url = activeUrl
        tabs.removeAll { it.url == url }
        webViews.remove(url)?.destroy()
        saveTabs()
        activeUrl = tabs.firstOrNull()?.url ?: ""
        renderBottomBar()
        hideSheet()
        if (activeUrl.isEmpty()) {
            webHost.removeAllViews()
            setConnStatus("没有服务器了，点击「＋」添加")
        } else {
            connect(activeUrl, activeTabPwd())
        }
        toast("已删除")
    }

    /* ================= 底部导航 ================= */
    private fun renderBottomBar() {
        // 用可横向滚动的 tab 条：服务器多时名字不被挤没，滑动即可看到全部
        bottomBar.removeAllViews()

        // 功能条：报表页功能入口（由 showOrCreateWebView 控制显示/隐藏）
        val funcInner = funcBar.getChildAt(0) as? LinearLayout
        funcInner?.removeAllViews()
        // 底部功能条：跳转类入口。"赢家"和"后三位"是开关，移到网页顶部筛选区。
        val funcs = listOf(
            Triple("高价值", "document.getElementById('highValueBtn').click();", false),
            Triple("统计", "document.getElementById('statsBtn').click();", false),
            Triple("生涯", "location.href='bidking_career.html?app=1';", true),
            Triple("道具", "location.href='bidking_item_prices.html?app=1';", true)
        )
        funcs.forEach { (label, js, primary) ->
            funcInner?.addView(TextView(this).apply {
                text = label
                textSize = if (primary) 12f else 11f
                gravity = Gravity.CENTER
                setTypeface(null, if (primary) android.graphics.Typeface.BOLD else android.graphics.Typeface.NORMAL)
                setPadding(if (primary) dp(12) else dp(10), dp(5), if (primary) dp(12) else dp(10), dp(5))
                val bg = if (primary) (if (darkMode) 0xFF1F7667.toInt() else 0xFF40B99F.toInt())
                        else (if (darkMode) 0xFF2A3646.toInt() else 0xFFE7E0D2.toInt())
                background = roundRectDrawable(bg, if (primary) 10f else 6f)
                setTextColor(if (primary) 0xFFFFFFFF.toInt() else (if (darkMode) 0xFFE8EEF4.toInt() else 0xFF1B2430.toInt()))
                addPressFeedback(this)
                setOnClickListener { v ->
                    currentWv?.evaluateJavascript(
                        "(function(){try{$js}catch(e){console.log('func click error',e);}})();", null)
                }
            }, LinearLayout.LayoutParams(
                if (primary) dp(58) else LinearLayout.LayoutParams.WRAP_CONTENT,
                if (primary) dp(34) else dp(30)
            ).apply { rightMargin = dp(6) })
        }
        // 红色「启动估价器」：走原生 HTTP 调 exe 端 /api/launch，不像上面那样执行网页 JS
        funcInner?.addView(TextView(this).apply {
            text = "启动估价器"
            textSize = 12f
            gravity = Gravity.CENTER
            setTypeface(null, android.graphics.Typeface.BOLD)
            setPadding(dp(10), dp(5), dp(10), dp(5))
            background = roundRectDrawable(0xFFD32F2F.toInt(), 10f)   // 红色，与其他入口区分
            setTextColor(0xFFFFFFFF.toInt())
            addPressFeedback(this)
            setOnClickListener { launchEstimator() }
        }, LinearLayout.LayoutParams(dp(84), dp(34)).apply { rightMargin = dp(6) })
        bottomBar.addView(funcBar, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        val tabRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        if (tabs.isEmpty()) {
            tabRow.addView(Button(this).apply {
                text = "＋ 添加第一个工具"
                isAllCaps = false
                textSize = 12f
                backgroundTintList = android.content.res.ColorStateList.valueOf(0xFF1F7667.toInt())
                setTextColor(0xFFFFFFFF.toInt())
                setOnClickListener { showAddSheet() }
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            bottomBar.addView(tabRow)
            syncWebHostBottomPadding()
            return
        }
        val tabStrip = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
        }
        val stripInner = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        tabs.forEach { tab ->
            val selected = tab.url == activeUrl
            stripInner.addView(Button(this).apply {
                text = tab.name
                textSize = 12f
                isAllCaps = false
                setPadding(dp(14), dp(6), dp(14), dp(6))
                backgroundTintList = android.content.res.ColorStateList.valueOf(
                    if (selected) (if (darkMode) 0xFF40B99F.toInt() else 0xFF1F7667.toInt())
                    else (if (darkMode) 0xFF2A3646.toInt() else 0xFFE7E0D2.toInt()))
                setTextColor(if (selected) 0xFFFFFFFF.toInt() else (if (darkMode) 0xFFE8EEF4.toInt() else 0xFF1B2430.toInt()))
                setOnClickListener {
                    // 点已选中的服务器名 = 打开配置（更直观地改地址/密码）；点其它服务器 = 直切
                    if (tab.url == activeUrl) {
                        showConfSheet(tab)
                        return@setOnClickListener
                    }
                    activeUrl = tab.url
                    renderBottomBar()
                    // P2 秒切：这台服务器的页面和令牌都还在 → 先把旧页面贴回来（零网络等待），
                    // 校验挪到后台；只有首次 / 页面被 LRU 淘汰时才走完整登录。
                    // 旧实现无条件 connect()，切一次就要等一整轮登录（用户体感"卡一下"）。
                    val key = normalizeServerUrl(tab.url)
                    val cached = webViews[key]
                    if (cached != null && tokenFor(key).isNotEmpty()) {
                        showOrCreateWebView(key)
                        setConnStatus("已连接" + scopeSuffix(key))
                        // 期间电脑端重新解析过 → 补一次刷新（软刷新优先，见 softRefresh）
                        if (dirtyServers.remove(key)) softRefresh(cached)
                        silentProbe(key)        // 后台校验：200 完全不动 UI，401 才补登录
                    } else {
                        connect(key, tab.pwd)
                    }
                }
                setOnLongClickListener { showConfSheet(tab); true }
            }, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        tabStrip.addView(stripInner)
        tabRow.addView(tabStrip, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        // ＋ 添加
        tabRow.addView(Button(this).apply {
            text = "＋"
            textSize = 18f
            setTypeface(null, android.graphics.Typeface.BOLD)
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFFA55B2A.toInt())
            setTextColor(0xFFFFFFFF.toInt())
            setOnClickListener { showAddSheet() }
        }, LinearLayout.LayoutParams(dp(44), LinearLayout.LayoutParams.WRAP_CONTENT))
        bottomBar.addView(tabRow)
        syncWebHostBottomPadding()
    }

    /* ================= 添加/切换服务器 ================= */
    private fun addTab(name: String, url: String) {
        if (tabs.any { it.url == url }) { toast("该地址已在列表中"); return }
        val pwd = sheetPwdInput.text.toString()
        tabs.add(ServerTab(name, url, pwd))
        saveTabs()
        activeUrl = url
        renderBottomBar()
        connect(url, pwd)
        toast("已添加「$name」")
    }

    /* ================= 轻量续连（2026-08-31 P1）=================
       令牌已经按服务器加密落盘，所以切后台 / 进程被回收回来时，绝大多数情况下
       不必再走一遍完整的密码登录或设备配对挑战-响应 —— 先拿旧令牌探一次就行。*/

    /** 先拿落盘令牌探一次 /api/status：有效就直接用，无效再走完整登录。
     *  ⚠️ 为什么用 /api/status 而不是 /api/server-info：server-info 在服务端的 GET
     *  免鉴权白名单里（serve.py:2388），不带令牌也返回 200，拿它校验等于没校验；
     *  /api/status 走令牌拦截器（serve.py:2404），响应体只有三个字段，最便宜。 */
    private fun resumeOrLogin(url: String, pwd: String) {
        val key = normalizeServerUrl(url)
        val tk = tokenFor(key)
        if (tk.isEmpty()) { connect(url, pwd); return }
        if (isConnecting) return          // 已有连接在跑，别插队
        setConnStatus("正在恢复连接…")
        thread {
            val (code, _, _) = httpGetEx("$key/api/status", tk)
            runOnUiThread {
                when {
                    code == 200 -> {
                        writeTokenCookie(key, tk)
                        setConnStatus("已连接" + scopeSuffix(key))
                        showOrCreateWebView(key)
                        startLowStockMonitor()
                        startAutoSyncMonitor()
                    }
                    code == 401 || code == 403 -> {
                        // 令牌过期（多半是电脑端的 exe 重启过，令牌是它的内存态）
                        setConnStatus("连接已过期，正在重新登录…")
                        setToken(key, "")
                        connect(key, pwd)
                    }
                    else -> {
                        // 网络不通：不清令牌（网络抖动 ≠ 令牌失效），有缓存页面就先看着
                        setConnStatus("网络不稳，页面显示的是上次数据")
                        if (webViews.containsKey(key)) showOrCreateWebView(key)
                        else connect(key, pwd)
                    }
                }
            }
        }
    }

    /** 后台静默探测：令牌还有效就【什么 UI 都不动】，只有 401 才补一次登录。
     *  用于「页面和令牌都还在」的切后台返回场景 —— 这是最常见的一条路径。 */
    private fun silentProbe(url: String) {
        val key = normalizeServerUrl(url)
        val tk = tokenFor(key)
        if (tk.isEmpty()) return
        thread {
            val (code, _, _) = httpGetEx("$key/api/status", tk)
            if (code == 401 || code == 403) {
                runOnUiThread {
                    setConnStatus("连接已过期，正在重新登录…")
                    setToken(key, "")
                    connect(key, activeTabPwd())
                }
            }
        }
    }

    /** 401 惰性自愈（2026-08-31 P1）：轮询类请求拿到 401 时自动重新登录。
     *  解决「电脑端 exe 重启后令牌全部作废」—— 这时状态栏还写着「已连接」，
     *  页面却 401 拿不到数据，用户会以为 App 坏了。同一服务器 60 秒内最多
     *  重连一次，避免网络抖动或 exe 反复重启把登录请求刷爆。 */
    private val lastReconnectAt = java.util.concurrent.ConcurrentHashMap<String, Long>()
    private fun reconnectSilently(url: String) {
        val key = normalizeServerUrl(url)
        val now = System.currentTimeMillis()
        if (now - (lastReconnectAt[key] ?: 0L) < 60_000L) return
        lastReconnectAt[key] = now
        setToken(key, "")
        runOnUiThread { setConnStatus("连接已过期，正在重新登录…") }
        val pwd = tabs.find { normalizeServerUrl(it.url) == key }?.pwd ?: ""
        connect(key, pwd)
    }

    /* ================= 连接 / 鉴权 ================= */
    // 2026-08-29：密码登录优先，不再一上来就要求配对。
    // 新 exe 规则：本机/局域网 = 密码对上即全权（scope=local-ui，无需配对）；
    // 只有 ngrok 公网未配对时才会拿到 legacy-readonly → 这时才走配对等电脑同意。
    // 失败提示具体化：密码错误 / 未填密码 / 连不上分开说清楚。
    private fun connect(server0: String, pwd: String) {
        if (server0.isEmpty() || isConnecting) return
        val server = normalizeServerUrl(server0)   // 历史保存的地址可能带路径尾巴，统一清洗
        if (server.isEmpty()) return
        isConnecting = true
        setConnStatus("正在连接…")
        thread {
            var ok = false
            var paired = false
            var readonly = false
            var errMsg = ""
            try {
            // 1) 已配对 → 挑战-响应登录（密钥失效多半是电脑端撤销/重置了这部手机）
            if (getDeviceKey(server).isNotEmpty()) {
                ok = deviceLogin(server)
                paired = ok
                if (!ok) clearDeviceKey(server)
            }
            // 2) 密码登录。局域网/本机：对上密码即全权；公网未配对：只读令牌。
            var needPair = false
            if (!ok) {
                val (code, body, errKind) = httpPostEx("$server/api/auth", JSONObject().put("pwd", pwd))
                val json = body?.let { b -> runCatching { JSONObject(b) }.getOrNull() }
                if (json != null && json.optBoolean("ok")) {
                    // P1：令牌按「本次连接的 server」存，不用 activeUrl ——
                    // 快速切 tab 时 activeUrl 可能已经指向另一台，会把令牌存错地方
                    val tk = json.optString("token", "")
                    val sc = json.optString("scope", "")
                    setToken(server, tk, sc)
                    passEnabled = json.optBoolean("pass_enabled", false)
                    ok = tk.isNotEmpty()
                    readonly = sc == "legacy-readonly"
                } else if (json != null && code == 403 && json.optString("error", "").contains("尚未配对")) {
                    // 电脑端要求先配对（公网+旧版只读被关）：直接走配对流程，不判失败
                    needPair = true
                } else if (json != null && code >= 400) {
                    // 服务器明确拒绝：配对也过不了密码关，直接报具体原因，不再盲试配对
                    errMsg = json.optString("error", "").ifEmpty { "电脑拒绝了连接（HTTP $code）" }
                    if (errMsg == "unauthorized") {
                        // 请求落进了服务器的令牌拦截器：多半是地址带了多余后缀
                        errMsg = "地址格式不对：地址只填到域名为止（如 https://xxx.ngrok-free.dev 或 http://192.168.x.x:8766），不要带 /api 等后缀；点底部服务器名可修改"
                    } else if (errMsg.contains("密码")) {
                        errMsg = if (pwd.isEmpty()) "电脑端已设置访问密码，点底部当前服务器名即可填写密码"
                                 else "密码错误，点底部当前服务器名可修改密码"
                    }
                    ok = false
                } else if (code == -1) {
                    // 网络层失败：区分「域名不存在」和「连不上」
                    errMsg = when (errKind) {
                        "dns" -> "该地址不存在（域名可能已过期）：ngrok 每次重启地址都会变，请到电脑端报表页复制最新的「公网访问」地址"
                        "conn" -> "连不上电脑：请确认电脑端解析器在运行、ngrok 隧道已开启、手机能上网"
                        else -> "网络错误，请重试"
                    }
                    ok = false
                }
            }
            // 3) 需要配对（公网只读，或电脑端明确要求）+ 有密码 → 发起配对
            if ((ok && readonly || needPair) && pwd.isNotEmpty()) {
                val (rid, auto, pairErr) = requestPair(server, pwd)
                if (auto != null) {
                    // 该设备此前已同意过配对：服务端凭密码直接重发了密钥+令牌（同一设备只需同意一次）
                    val k = auto.optString("device_key", "")
                    if (k.isNotEmpty()) saveDeviceKey(server, k)
                    val tk = auto.optString("token", "")
                    if (tk.isNotEmpty()) {
                        setToken(server, tk, "", paired = true)
                        paired = true; readonly = false; ok = true
                    } else if (deviceLogin(server)) {
                        paired = true; readonly = false; ok = true
                    } else if (needPair) { ok = false; errMsg = "重新绑定成功但登录失败，请重试" }
                } else if (rid != null) {
                    runOnUiThread {
                        setConnStatus("等待电脑端同意配对…")
                        showWebHostHint("🔐 等待电脑端同意\n\n这是通过公网(ngrok)访问，需要在电脑上批准：\n电脑报表页 →「🚀 远程启动电脑程序」弹窗 →「安全与配对」→ 点「同意」\n\n（同意后本窗口自动继续）\n\n若你在同一 Wi-Fi，请改用电脑网页顶部显示的局域网地址，无需配对即可连接")
                    }
                    val (k, waitErr) = pollPairKey(server, rid)
                    if (k != null) {
                        saveDeviceKey(server, k)
                        if (deviceLogin(server)) { paired = true; readonly = false; ok = true }
                        else if (needPair) { ok = false; errMsg = "配对成功但登录失败，请重试" }
                    } else if (needPair) {
                        ok = false
                        errMsg = waitErr.ifEmpty { "配对未完成，请重试" }
                    }
                    // 公网只读连接配对没成 → 保持只读连接可用，不再整体判失败
                } else if (needPair) {
                    ok = false
                    errMsg = when {
                        pairErr.contains("密码") ->
                            if (pwd.isEmpty()) "电脑端已设置访问密码，点底部当前服务器名即可填写密码"
                            else "密码错误，点底部当前服务器名可修改密码"
                        pairErr.isNotEmpty() -> "$pairErr（点底部当前服务器名可修改密码）"
                        else -> "配对请求失败，请重试"
                    }
                }
            } else if (needPair && pwd.isEmpty()) {
                ok = false
                errMsg = "电脑端要求配对且需要访问密码，点底部当前服务器名填写密码后重试"
            }
            if (ok) {
                // 令牌写入 WebView Cookie（2026-08-30）：页面发出的第一个请求就带令牌，
                // 消灭"页面先请求、令牌后注入"的 401 竞态（局域网+已设密码时
                // 生涯/道具/价格库会全没数据）。
                // P1：setToken() 内部已同步写 Cookie，这里再补一次确保与当前 server 一致
                writeTokenCookie(server, tokenFor(server))
                runOnUiThread {
                    // 状态栏不回显服务器地址（2026-08-30）：截图外发时不暴露所连域名
                    setConnStatus("已连接" + when {
                        paired -> " · 已配对"
                        readonly -> " · 只读（公网未配对）"
                        else -> ""
                    })
                    showOrCreateWebView(server)
                    startLowStockMonitor()   // 连接成功即开始库存轮询
                    startAutoSyncMonitor()   // 连接成功即开始自动同步检测（exe 刷新后 App 同步）
                    if (readonly) toast("公网访问为只读；在电脑端配对后可远程启动估价器")
                }
            } else {
                setToken(server, "")
                runOnUiThread {
                    if (errMsg.isEmpty()) {
                        errMsg = "连不上电脑：确认电脑上解析器正在运行、地址正确、防火墙放行 8766"
                    }
                    setConnStatus("连接失败：$errMsg")
                    showWebHostHint("⚠️ 连接失败\n\n$errMsg\n\n检查清单：\n• 电脑上 BidKing解析器.exe 是否在运行\n• 地址是否正确（电脑网页顶部会显示「另一台电脑访问」的局域网地址）\n• 手机和电脑是否在同一 Wi-Fi；Windows 防火墙是否放行 8766\n• 密码是否与电脑端设置的一致\n\n点底部服务器名可重新连接")
                    toast("无法获取凭证，请检查地址与密码")
                }
            }
            } catch (e: Exception) {
                setToken(server, "")
                val msg = e.message ?: "未知错误"
                runOnUiThread {
                    setConnStatus("连接异常")
                    showWebHostHint("⚠️ 连接异常\n\n$msg\n\n点底部服务器名可重新连接")
                }
            } finally {
                isConnecting = false
            }
        }
    }

    // 连接失败/未连接时：WebView 区域显示引导占位（避免空白）+ 重试按钮
    private fun showWebHostHint(text: String) {
        // 版本署名：附在提示卡片尾部
        val ver = try {
            packageManager.getPackageInfo(packageName, 0).versionName ?: ""
        } catch (_: Exception) { "" }
        val signed = text + "\n\n—— BidKing 远程助手 v$ver"
        webHost.removeAllViews()
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(dp(24), dp(16), dp(24), dp(16))
            setBackgroundColor(if (darkMode) 0xFF171F29.toInt() else 0xFFF4F1EA.toInt())
        }
        wrap.addView(TextView(this).apply {
            this.text = signed
            textSize = 14f
            setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else 0xFF1B2430.toInt())
            gravity = Gravity.CENTER
        }, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        // 重试按钮：一键重新连接当前服务器
        wrap.addView(Button(this).apply {
            setText("🔄 重试连接")
            setAllCaps(false)
            minHeight = dp(44)
            textSize = 14f
            backgroundTintList = android.content.res.ColorStateList.valueOf(0xFF1F7667.toInt())
            setTextColor(0xFFFFFFFF.toInt())
            setOnClickListener {
                showWebHostHint("正在重连…")
                connect(activeUrl, activeTabPwd())
            }
        }, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(16)
        })
        webHost.addView(wrap, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
    }

    /* ================= WebView（?app=1 裁剪） ================= */
    private fun showOrCreateWebView(url: String) {
        // P2：池的 key 必须归一。connect() 传进来的是 normalize 后的 server，
        // 而 tab.url 可能带尾巴（/api、结尾 /），不归一就会给同一台服务器建出两个
        // WebView，白占一个 LRU 名额，还会让「切回来」变成「重新加载」。
        val key = normalizeServerUrl(url)
        val wv = webViews.getOrPut(key) {
            WebView(this).apply {
                layoutParams = FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT)
                settings.apply {
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                    mediaPlaybackRequiresUserGesture = false
                    // 整页缩放一屏显示（用户要求连接后无需滑动看全部）
                    useWideViewPort = true
                    loadWithOverviewMode = true
                }
                // 深色下消除 WebView 初始白闪
                setBackgroundColor(if (darkMode) 0xFF12171F.toInt() else 0xFFF4F1EA.toInt())
                webChromeClient = object : android.webkit.WebChromeClient() {
                    override fun onProgressChanged(view: WebView?, newProgress: Int) {
                        webProgress.progress = newProgress
                        webProgress.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
                    }
                }
                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                        var u = request?.url?.toString() ?: return false
                        // 内嵌页面切换自动带 ?app=1（生涯/道具价格页也裁剪为 App 视图）
                        if (u.contains("bidking_career.html") || u.contains("bidking_item_prices.html") ||
                            u.contains("bidking_standalone.html")) {
                            u = u + (if (u.contains("?")) "&app=1" else "?app=1")
                        }
                        updateFuncBarVisibility(!(u.contains("bidking_career.html") || u.contains("bidking_item_prices.html")))
                        // 主帧导航带 ngrok 跳过头，防免费版警告页（WebView UA 是浏览器形态）
                        view?.loadUrl(u, mapOf("ngrok-skip-browser-warning" to "1")); return true
                    }
                    override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                        super.onPageStarted(view, url, favicon)
                        // 提前注入 fetch/XHR 包装器（ngrok 跳过头 + 令牌，2026-08-30）。
                        // 原来只在 onPageFinished 注入，而页面在 DOMContentLoaded 就发首个
                        // 数据请求（道具页读价格库/生涯页读数据库）——请求赶在注入前出去，
                        // 经 ngrok 免费版拿到警告页 HTML → JSON 解析失败 →
                        // 道具页显示「无法连接价格数据库」。onPageStarted 阶段页面脚本
                        // 尚未执行，包装器先落地；包装器只是包一层 window.fetch/XHR，幂等，
                        // onPageFinished 的兜底注入再包一层也无害（头重复设置无副作用）。
                        // P1：传本页 URL，按它取令牌（不能用 activeUrl，多服务器会串号）
                        injectTokenIntoPage(view, url)
                        // P3：主报表页本地缓存层必须排在令牌包装器【之后】注入，
                        // 它捕获的 origFetch 才已带令牌头（安17）；只在报表页注入
                        if (url != null && url.contains("bidking_report.html")) {
                            injectReportCacheLayer(view)
                        }
                    }
                    override fun onPageFinished(view: WebView?, pageUrl: String?) {
                        super.onPageFinished(view, pageUrl)
                        val pu = pageUrl ?: ""
                        updateFuncBarVisibility(!(pu.contains("bidking_career.html") || pu.contains("bidking_item_prices.html")))
                        injectGlobalTheme(view)
                        injectTouchFeel(view)
                        injectTokenIntoPage(view, pageUrl)
                        injectEnhancements(view, pageUrl)
                        // P3 兜底：onPageStarted 注入万一丢失，这里补一次缓存层
                        // （同文档幂等守卫，重复注入无副作用；迟到时首个请求已走网络，
                        //  但 __bkSoftRefresh / 缓存写库仍生效）
                        if (pageUrl != null && pageUrl.contains("bidking_report.html")) {
                            injectReportCacheLayer(view)
                        }
                        // E1：生涯页数据经 fetch 后异步渲染，延迟补注入一次，
                        // 确保 data-label/拿仓背景/三合一切换等生效（页面 DOM 就绪兜底）
                        view?.postDelayed({
                            injectGlobalTheme(view)
                            injectTouchFeel(view)
                            injectEnhancements(view, pageUrl)
                        }, 800)
                    }
                    @Suppress("DEPRECATION")
                    override fun onReceivedError(view: WebView?, errorCode: Int, description: String?, failingUrl: String?) {
                        super.onReceivedError(view, errorCode, description, failingUrl)
                        // 加载失败不白屏：显示错误信息
                        runOnUiThread {
                            setConnStatus("页面加载失败：$description")
                            showWebHostHint("⚠️ 报表加载失败\n\n$description\n\n检查：\n• 电脑工具是否运行\n• 地址/密码是否正确\n• 点底部服务器名重试")
                        }
                    }
                }
                // ?app=1：页面裁剪（隐藏局域网/数据库/密码/自动刷新）+ 不抢控制锁
                // 注意：loadUrl 移到 attach 窗口之后再执行（见下方），避免首次 onPageFinished
                // 在 WebView 未挂载时触发导致注入脚本丢失，页面停留在 exe 样式。
            }
        }
        webHost.removeAllViews()
        webHost.addView(wv, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT))
        currentWv = wv   // LRU 淘汰时保护当前可见页
        // 底部功能条只在报表页显示（生涯/道具页不需要这些入口）
        val curUrl = wv.url ?: ""
        updateFuncBarVisibility(!(curUrl.contains("bidking_career.html") || curUrl.contains("bidking_item_prices.html")))
        // removeAllViews 会把进度条也清掉，重新加回来并置顶
        webHost.addView(webProgress, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, dp(4), Gravity.TOP))
        webProgress.z = 10f
        // 重新显示缓存的 WebView 时恢复主题/token/增强脚本（切 tab 回来不变回 exe 样式）
        // P1：令牌按这张 WebView 自己的 URL 注入（切 tab 秒显时页面还属于旧服务器）
        injectGlobalTheme(wv)
        injectTouchFeel(wv)
        injectTokenIntoPage(wv, wv.url ?: key)
        wv.url?.let { injectEnhancements(wv, it) }
        // 首次创建并在挂载窗口后再 loadUrl，确保 onPageFinished/evaluateJavascript 稳定生效
        if (wv.tag == null) {
            wv.tag = true
            // P2：LRU 淘汰后重建的 WebView，页面 DOM 状态丢了，但令牌 Cookie 是应用级
            // 存储、还在。这里无条件补写一次（幂等、零成本），确保首请求就带令牌。
            writeTokenCookie(key, tokenFor(key))
            // 主帧导航带 ngrok 跳过头，防免费版警告页（WebView UA 是浏览器形态）
            wv.loadUrl("$key/bidking_report.html?app=1",
                mapOf("ngrok-skip-browser-warning" to "1"))
        }
    }

    /** 软刷新：优先让页面自己重新拉数据（保住滚动位置和已渲染的内容），
     *  页面还没注入刷新接口时回落到整页 reload。
     *  P3 会把页面侧的 window.__bkSoftRefresh 接上；这里先保证调用点可用。 */
    private fun softRefresh(wv: WebView) {
        wv.evaluateJavascript(
            "(function(){try{if(window.__bkSoftRefresh){window.__bkSoftRefresh();return '1';}}catch(e){}return '0';})()"
        ) { r -> if ((r ?: "").trim('"') != "1") wv.reload() }
    }

    /** P3：主报表页本地缓存层（App 端注入，不动 exe 内嵌网页）。
     *  - IndexedDB 按服务器 origin 天然隔离（多服务器各自独立缓存）；
     *  - 首个 result.json 请求先投喂缓存（秒开、零网络），后台再用 ETag 对账：
     *    result.json 是「每次解析整体重写的会话快照」，不是只增库，所以
     *    不用 since_ts，只用 ETag（serve.py 静态文件 mtime-size）判「有没有变」；
     *  - 对账一致 → 什么都不做（滚动位置纹丝不动）；有变 → softRender 重渲染
     *    并恢复 window.scrollY + .table-wrap 滚动位置；
     *  - 自愈（用户选择不加手动清缓存入口）：缓存先过页面自己的 validateReport
     *    校验，不过就丢弃走网络；整层 try/catch 降级；连续 3 次渲染异常
     *    自动置 __bkReportCacheOff 关闭缓存层（本次文档内生效）。
     *  ⚠️ 安17：必须排在 injectTokenIntoPage 之后注入（见 onPageStarted 调用点），
     *  这里的 origFetch 已带 X-Auth-Token 与 ngrok 跳过头。
     *  本段 JS 入库前曾以独立 .js 文件过 node --check（语法零错），改这里时
     *  建议先复制出去再过一遍语法检查（改 html/注入 JS 必过 JS 语法检查——页1）。 */
    private fun injectReportCacheLayer(wv: WebView?) {
        val js = """(function () {
  if (window.__bkReportCacheReady || window.__bkReportCacheOff) return;
  window.__bkReportCacheReady = true;
  var origFetch = window.fetch;
  var DB = "bidking_report", ST = "kv", KEY = "report_cache";
  var cache = null, served = false, renderFails = 0;

  function idbOpen() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(ST); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function idbGet(key) {
    return idbOpen().then(function (db) {
      return new Promise(function (resolve, reject) {
        var rq = db.transaction(ST, "readonly").objectStore(ST).get(key);
        rq.onsuccess = function () { resolve(rq.result); };
        rq.onerror = function () { reject(rq.error); };
      }).then(function (v) { db.close(); return v; }, function (e) { db.close(); throw e; });
    });
  }
  function idbSet(key, val) {
    return idbOpen().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(ST, "readwrite");
        tx.objectStore(ST).put(val, key);
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { reject(tx.error); };
      }).then(function (v) { db.close(); return v; }, function (e) { db.close(); throw e; });
    });
  }

  function cacheReady() {
    return idbGet(KEY).then(function (c) {
      if (c && typeof c.text === "string" && typeof c.etag === "string") cache = c;
    }).catch(function () {});
  }
  var readyP = cacheReady();

  function validCache() {
    try { return !!(cache && cache.text && window.validateReport(JSON.parse(cache.text))); }
    catch (e) { return false; }
  }
  function isResultJson(u) { var i = u.indexOf("result.json"); return i >= 0 && (i + 11 === u.length || u.charAt(i + 11) === "?"); }
  function note(msg) { try { window.showStatus(msg); } catch (e) {} }
  function saveCache(etag, text) {
    cache = { etag: etag || "", text: text, savedAt: Date.now() };
    idbSet(KEY, cache).catch(function () {});
  }

  window.fetch = function (input, init) {
    try {
      var u = String((input && input.url) || input || "");
      if (isResultJson(u) && !(init && init.__bkBypass)) {
        if (!served) {
          served = true;
          var args = arguments, self = this;
          setTimeout(bkReconcile, 0);
          return readyP.catch(function () {}).then(function () {
            if (window.__bkReportCacheOff || !validCache()) return origFetch.apply(self, args);
            var mins = cache.savedAt ? Math.max(1, Math.round((Date.now() - cache.savedAt) / 60000)) : null;
            note(mins != null ? ("显示的是缓存数据（约 " + mins + " 分钟前），正在检查更新…") : "正在检查更新…");
            return new Response(cache.text, {
              status: 200,
              headers: { "Content-Type": "application/json" }
            });
          });
        }
        var p = origFetch.apply(this, arguments);
        return p.then(function (r) {
          if (r && r.ok) {
            try {
              r.clone().text().then(function (t) { saveCache(r.headers.get("ETag") || "", t); }).catch(function () {});
            } catch (e) {}
          }
          return r;
        });
      }
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  async function bkReconcile() {
    try {
      var r = await origFetch("result.json", { __bkBypass: true });
      if (r.status === 401 || r.status === 403) { note("当前没有访问权限，显示的是缓存数据"); return; }
      if (!r.ok) { note("暂时连不上电脑，显示的是缓存数据"); return; }
      var etag = r.headers.get("ETag") || "";
      var text = await r.text();
      if (cache && etag && etag === cache.etag) { note("已是最新"); return; }
      if (cache && !etag && text === cache.text) { note("已是最新"); return; }
      var data;
      try { data = window.validateReport(JSON.parse(text)); }
      catch (e) { note("新数据校验失败，暂时保留缓存显示"); return; }
      saveCache(etag, text);
      softRender(data);
      note("已更新到最新");
    } catch (e) { note("暂时连不上电脑，显示的是缓存数据"); }
  }

  function softRender(data) {
    try {
      var y = window.scrollY;
      var wraps = [];
      document.querySelectorAll(".table-wrap").forEach(function (el) {
        wraps.push([el, el.scrollTop, el.scrollLeft]);
      });
      report = data;
      window.__hasGames = !!(data && data.games && data.games.length);
      try {
        if (typeof getSelectedUid === "function" && !getSelectedUid() && data.meta && data.meta.uid) setSelectedUid(data.meta.uid);
      } catch (e) {}
      window.render("result.json");
      requestAnimationFrame(function () {
        window.scrollTo(0, y);
        wraps.forEach(function (p) { p[0].scrollTop = p[1]; p[0].scrollLeft = p[2]; });
      });
    } catch (e) {
      if (++renderFails >= 3) window.__bkReportCacheOff = 1;
    }
  }

  window.__bkSoftRefresh = function () {
    try { bkReconcile(); return true; } catch (e) { return false; }
  };
})();"""
        wv?.evaluateJavascript(js, null)
    }

    /** 注入 fetch/XHR 包装器：ngrok 跳过头 + 令牌。
     *  ⚠️ 安16（P1）：必须按【这个 WebView 自己的 URL】取令牌，不能用 authToken
     *  （authToken = 当前 activeUrl 的令牌）。多服务器各自持有 WebView，切 tab 后
     *  若给旧页面注入新服务器的令牌，那个页面的请求会全部 401、数据全空。 */
    private fun injectTokenIntoPage(wv: WebView?, viewUrl: String? = null) {
        // ngrok 免费版会对"浏览器 UA"的请求插警告页，页面内 fetch/XHR 也要带跳过头
        // （令牌可能还没拿到，所以这层包装与令牌解耦，始终注入）
        val tk = tokenFor(viewUrl ?: wv?.url ?: activeUrl)
        val safeToken = tk.replace("\\", "\\\\").replace("'", "\\'")
        val tokenPart = if (tk.isNotEmpty()) "opt.headers['X-Auth-Token']='$safeToken';" else ""
        val tokenXhr = if (tk.isNotEmpty()) "try{this.setRequestHeader('X-Auth-Token','$safeToken');}catch(e){}" else ""
        val js = "(function(){" +
                "const H={'ngrok-skip-browser-warning':'1'};" +
                "const o=window.fetch;window.fetch=function(u,opt){opt=opt||{};opt.headers=opt.headers||{};opt.headers['ngrok-skip-browser-warning']='1';$tokenPart return o(u,opt);};" +
                "const o2=XMLHttpRequest.prototype.open;const o3=XMLHttpRequest.prototype.send;" +
                "XMLHttpRequest.prototype.open=function(m,url){this.__bkMethod=m;this.__bkUrl=url;return o2.apply(this,arguments);};" +
                "XMLHttpRequest.prototype.send=function(body){try{this.setRequestHeader('ngrok-skip-browser-warning','1');}catch(e){}$tokenXhr return o3.apply(this,arguments);};})();"
        wv?.evaluateJavascript(js, null)
    }

    // 全局深色主题注入（localStorage + data-theme，三页 JS 读全局 key 应用深/浅色）
    private fun injectGlobalTheme(wv: WebView?) {
        val t = if (darkMode) "dark" else "light"
        val pc = if (profitInverted) "inverted" else "normal"
        // 配色也要持久化到全局 key（2026-08-30 修）：此前只设当前文档的
        // data-profit-colors 属性，页面自身启动时读全局 key（为空→绿盈红亏）
        // 先跑一遍，拿仓行背景/盈亏数字先按错的上色；持久化后页面启动即正确。
        wv?.evaluateJavascript(
            "(function(){localStorage.setItem('bidking-global-theme','$t');" +
            "localStorage.setItem('bidking-global-profit-colors','$pc');" +
            "document.documentElement.dataset.theme='$t';" +
            "document.documentElement.setAttribute('data-profit-colors','$pc');})();", null)
    }

    // 全局触控手感注入（2026-08-30）：去掉 WebView 默认的蓝色方块点按高亮
    //（用户反馈「披着 app 的网页」的主要来源），换成跟手的按压态：
    // 按钮按下变暗、行/可点元素按下变淡、UI 元素禁用长按选中。
    // 同时承载拿仓行底色规则（安12）——必须放这个【无条件注入、无守卫】的样式表：
    // 生涯明细在 App 端有两种渲染（页面自带卡片化 CSS / 增强注入的表格化 CSS），
    // 而增强注入有时序守卫可能整块不生效；规则放在这里才能保证两种模式下都在。
    // 特异性带 #careerTableScroll ID + .win-* 类，压过页面卡片规则 var(--panel)
    // 和增强表格规则的 transparent（都是 ID+!important 一级）。
    private fun injectTouchFeel(wv: WebView?) {
        val js = "(function(){" +
                "if(document.getElementById('__bk_touch_feel'))return;" +
                "var s=document.createElement('style');s.id='__bk_touch_feel';" +
                "s.textContent=[" +
                "'*{-webkit-tap-highlight-color:transparent!important;}'," +
                "'button,select,.rate-toggle,tr[onclick],[role=button]{user-select:none;-webkit-user-select:none;}'," +
                "'button:active,select:active{filter:brightness(.85);}'," +
                "'tbody tr:active{filter:brightness(.92);}'," +
                "'[onclick]:active,.rate-toggle:active{opacity:.65;}'," +
                /* 拿仓行底色：跟随红盈绿亏/绿盈红亏（红盈=盈利拿仓淡红/亏损拿仓淡绿；绿盈相反） */
                "'html.app-mode #careerTableScroll table tbody tr.win-positive{background:var(--profit-good-bg,rgba(17,98,72,.2))!important;}'," +
                "'html.app-mode #careerTableScroll table tbody tr.win-negative{background:var(--profit-bad-bg,rgba(163,61,42,.2))!important;}'," +
                "'html.app-mode:not([data-theme=dark]) #careerTableScroll table tbody tr.win-positive{background:rgba(17,98,72,.2)!important;}'," +
                "'html.app-mode:not([data-theme=dark]) #careerTableScroll table tbody tr.win-negative{background:rgba(163,61,42,.2)!important;}'," +
                "'html.app-mode:not([data-theme=dark])[data-profit-colors=inverted] #careerTableScroll table tbody tr.win-positive{background:rgba(163,61,42,.2)!important;}'," +
                "'html.app-mode:not([data-theme=dark])[data-profit-colors=inverted] #careerTableScroll table tbody tr.win-negative{background:rgba(17,98,72,.2)!important;}'," +
                "'html.app-mode[data-theme=dark] #careerTableScroll table tbody tr.win-positive{background:rgba(39,255,154,.15)!important;}'," +
                "'html.app-mode[data-theme=dark] #careerTableScroll table tbody tr.win-negative{background:rgba(255,91,97,.15)!important;}'," +
                "'html.app-mode[data-theme=dark][data-profit-colors=inverted] #careerTableScroll table tbody tr.win-positive{background:rgba(255,91,97,.15)!important;}'," +
                "'html.app-mode[data-theme=dark][data-profit-colors=inverted] #careerTableScroll table tbody tr.win-negative{background:rgba(39,255,154,.15)!important;}'" +
                "].join('');" +
                "(document.head||document.documentElement).appendChild(s);" +
                "})();"
        wv?.evaluateJavascript(js, null)
    }

    /* ================= App 端增强注入（图表/生涯三合一 ⇌ + 统计卡紧凑）
       只依赖页面 DOM 结构，兼容 exe 新旧版本（无论电脑端网页有没有内置切换，App 加载后都生效） ================= */
    private fun injectEnhancements(wv: WebView?, pageUrl: String?) {
        val url = pageUrl ?: return
        // 通用：给所有动态生成的表格补列名标签，避免卡片化后只剩裸数字
        wv?.evaluateJavascript(AppEnhance.TABLE_LABEL_JS, null)
        // 报表页：收益/拍品价值/银币 三合一切换 + 统计卡紧凑 + 回到顶部悬浮按钮
        if (url.contains("bidking_report.html")) {
            wv?.evaluateJavascript(AppEnhance.REPORT_ENHANCE_JS, null)
            wv?.evaluateJavascript(AppEnhance.REPORT_CARDS_JS, null)
            wv?.evaluateJavascript(AppEnhance.FAB_JS, null)
        }
        // 生涯页：各地图大类/24h/高价值 三合一切换 + 「前往该局」卡死修复 + 回到顶部悬浮按钮
        else if (url.contains("bidking_career.html")) {
            wv?.evaluateJavascript(AppEnhance.CAREER_ENHANCE_JS, null)
            wv?.evaluateJavascript(AppEnhance.CAREER_JUMP_PATCH_JS, null)
            wv?.evaluateJavascript(AppEnhance.FAB_JS, null)
        }
        // 道具价格页：3 列响应式网格 + 自动保存 + 回到顶部悬浮按钮
        else if (url.contains("bidking_item_prices.html")) {
            wv?.evaluateJavascript(AppEnhance.ITEM_ENHANCE_JS, null)
            wv?.evaluateJavascript(AppEnhance.FAB_JS, null)
        }
    }

    private object AppEnhance {
        /* 主页详细对局卡片化（2026-08-30 用户要求）：3×2 字段卡
           时间/地图/赢家 | 拍下物品/展示盈亏/我的盈亏（赢家列回归）。
           ⚠️ 安12 教训：卡片底色不带 !important，拿仓 win-positive/win-negative
           的配色底色用更高优先级 !important 规则显式保留（页面 var 为 v1.4.2 的 20%）。 */
        const val REPORT_CARDS_JS = """
            (function(){
              try {
                if (document.getElementById('__bk-report-cards')) return;
                const wrap = document.querySelector('section.table-wrap');
                if (!wrap || !wrap.querySelector('#tableBody')) return;   // 页面未就绪：留给 800ms 补注入
                const st = document.createElement('style');
                st.id = '__bk-report-cards';
                st.textContent = [
                  'html.app-mode .table-wrap table { min-width:0 !important; display:block !important; }',
                  'html.app-mode .table-wrap thead { display:none !important; }',
                  'html.app-mode .table-wrap tbody { display:block !important; }',
                  'html.app-mode .table-wrap tbody tr { display:grid !important; grid-template-columns:repeat(3,1fr) !important; gap:1px 10px !important; padding:9px 12px 7px !important; margin:0 0 8px 0 !important; border:1px solid var(--line) !important; border-radius:10px !important; background:var(--panel); overflow:hidden !important; }',
                  'html.app-mode .table-wrap tbody tr.win-positive { background:var(--profit-good-bg) !important; }',
                  'html.app-mode .table-wrap tbody tr.win-negative { background:var(--profit-bad-bg) !important; }',
                  'html.app-mode .table-wrap tbody td { display:block !important; padding:0 !important; border:0 !important; height:auto !important; vertical-align:top !important; text-align:left !important; white-space:nowrap !important; overflow:hidden !important; text-overflow:ellipsis !important; }',
                  'html.app-mode .table-wrap tbody td.won-items { white-space:normal !important; }',
                  'html.app-mode .table-wrap tbody td::before { display:block !important; color:var(--muted) !important; font-size:9px !important; line-height:1.15 !important; content:attr(data-label); }',
                  'html.app-mode .table-wrap tbody td:nth-child(2),',
                  'html.app-mode .table-wrap tbody td:nth-child(5),',
                  'html.app-mode .table-wrap tbody td:nth-child(7),',
                  'html.app-mode .table-wrap tbody td:nth-child(8),',
                  'html.app-mode .table-wrap tbody td:nth-child(9),',
                  'html.app-mode .table-wrap tbody td:nth-child(10) { display:none !important; }'
                ].join('');
                document.head.appendChild(st);
              } catch (e) {}
            })();
        """

        // 报表页注入：三合一⇌（不依赖 exe 是否已有 data-trend-group，找不到三图表则跳过）+ 统计卡紧凑
        const val REPORT_ENHANCE_JS = """
            (function(){
              try {
                const MARK = "__bkAppEnh1";
                const isReport = !!document.querySelector('canvas#profitTrendCanvas');
                if (!isReport) return;                 // 页面未就绪：不置 MARK，让 800ms 后补注入有机会执行
                if (window[MARK]) return;             // 幂等：已注入过不再重复
                window[MARK] = 1;
                // === 统计卡紧凑（App 下 6 卡 3×2 全可见，小字号小间距） ===
                const sum = document.querySelector('#summarySection, section.summary');
                if (sum) {
                  sum.classList.add('__bk-compact');
                  const style = document.createElement('style');
                  style.textContent = `
                    section.summary.__bk-compact { grid-template-columns: repeat(3, minmax(0,1fr)) !important; gap:4px !important; margin:6px 0 !important; }
                    section.summary.__bk-compact .card { padding:4px 6px !important; }
                    section.summary.__bk-compact .card .label { font-size:9px !important; }
                    section.summary.__bk-compact .card .value { font-size:12px !important; margin-top:1px !important; }
                    section.summary.__bk-compact .card .delta { font-size:8px !important; }
                    section.summary.__bk-compact .card .mini-action { font-size:8px !important; padding:0 4px !important; min-height:16px !important; }
                  `;
                  style.id = '__bk-compact-style';
                  if (!document.getElementById('__bk-compact-style')) document.head.appendChild(style);
                }
                // === Canvas 字体放大 1.5 倍（修走势图文字挤压）===
                // ⚠️ 关键：必须带 get 陷阱，把方法 bind 回真正的 context。
                // 浏览器的 canvas 方法校验 this 的内部槽位，Proxy 不是平台对象，
                // 只写 set 陷阱时 ctx.setTransform() 会抛 Illegal invocation → 整张图画不出来（三张图就是这么没的）。
                if (!window.__bkCanvasFontBoost) {
                  window.__bkCanvasFontBoost = 1;
                  const origGetContext = HTMLCanvasElement.prototype.getContext;
                  HTMLCanvasElement.prototype.getContext = function(type, attrs) {
                    const ctx = origGetContext.call(this, type, attrs);
                    if (!ctx || type !== '2d') return ctx;
                    return new Proxy(ctx, {
                      set(target, prop, value) {
                        if (prop === 'font' && typeof value === 'string') {
                          value = value.replace(/(\d+)(?:\.\d+)?px/g, function(m, n){ return Math.round(parseFloat(n) * 1.5) + 'px'; });
                        }
                        target[prop] = value;
                        return true;
                      },
                      get(target, prop) {
                        const v = target[prop];
                        return (typeof v === 'function') ? v.bind(target) : v;
                      }
                    });
                  };
                }
                // === 绘图自检：确认 canvas 真的能画。画不了就完全不接管图表，宁可跟电脑端一样全列出来 ===
                let drawOK = true, drawErr = '';
                try {
                  const pc = document.createElement('canvas');
                  pc.width = 4; pc.height = 4;
                  const px = pc.getContext('2d');
                  px.fillStyle = '#000';
                  px.fillRect(0, 0, 4, 4);
                  const pd = px.getImageData(0, 0, 1, 1).data;
                  drawOK = !!pd && pd[3] !== 0;
                } catch (e) { drawOK = false; drawErr = String((e && e.message) || e); }
                window.__bkDrawOK = drawOK;
                window.__bkDrawErr = drawErr;
                const CHART_IDS = ['profitTrendCanvas', 'valueTrendCanvas', 'silverTrendCanvas'];
                function hasReportData() {
                  try { return !!(typeof report !== 'undefined' && report && report.games && report.games.length > 0); } catch (e) { return false; }
                }
                // 三张全显兜底（调试/极端用）：退出兜底模式并回到当前走势
                window.__bkShowAllCharts = function() {
                  window.__bkShowAll = false;
                  if (typeof showChart === 'function') showChart(window.__bkChartIdx || 0);
                };
                // === 图表三合一：收益 / 拍品价值 / 银币（每个标题都带 ⇌，切换后按钮不消失；固定显示位置） ===
                const charts = CHART_IDS.map(function(id){ return document.getElementById(id); });
                if (charts[0] && charts[1] && charts[2] && drawOK) {
                  const secs = charts.map(c => c.closest('section.chart-panel, .chart-panel'));
                  if (secs.every(Boolean)) {
                    // 锚点：『道具当前数量』panel（含 #inventoryBox 的 section）——三个图表都固定显示在它之前
                    let anchor = null;
                    const inventorySec = document.querySelector('#inventoryBox')?.closest('section.chart-panel, .chart-panel');
                    if (inventorySec) {
                      anchor = inventorySec;
                    } else {
                      const all = document.querySelectorAll('section.chart-panel, .chart-panel');
                      for (const s of all) {
                        const t = s.querySelector('.chart-title');
                        if (t && /道具当前数量/.test(t.textContent || '')) { anchor = s; break; }
                      }
                    }
                    const names = ['收益走势', '拍品价值走势', '银币数量走势'];
                    let idx = 0;
                    // ★ 关键修复：被 display:none 隐藏的 canvas，getBoundingClientRect() 宽高为 0，
                    // 页面 render() 时 canvas.width 会被设成 0 → 画不出来，且不重绘就永远空白。
                    // 所以每次把某个图表显示出来后，必须按当前真实尺寸立刻重绘一次。
                    // 注意：report 是 let 声明（不在 window 上），但注入脚本在全局作用域执行，可用裸标识符访问；
                    // renderProfitTrend / filteredGames / renderValueTrend / renderSilverTrend 都是顶层函数声明。
                    function redraw(i) {
                      try {
                        if (typeof report === 'undefined' || !report) return false;
                        if (i === 0 && typeof renderProfitTrend === 'function') { renderProfitTrend(filteredGames()); return true; }
                        if (i === 1 && typeof renderValueTrend === 'function') { renderValueTrend(filteredGames()); return true; }
                        if (i === 2 && typeof renderSilverTrend === 'function') { renderSilverTrend(); return true; }
                      } catch (e) {}
                      return false;
                    }
                    function showChart(i) {
                      idx = ((i % 3) + 3) % 3;
                      window.__bkChartIdx = idx;
                      window.__bkShowAll = false;           // 主动切换即退出三张全显模式
                      // 先把当前要显示的 section 移到锚点之前（固定位置）
                      if (anchor && anchor.parentNode && secs[idx] !== anchor) {
                        anchor.before(secs[idx]);
                      }
                      secs.forEach((s, j) => { s.style.display = (j === idx) ? '' : 'none'; });
                      // 2026-08-30 用户反馈（文字挤压）：移动 DOM 节点会清空 canvas 位图，
                      // 显示后遗留的旧位图会被拉伸/挤压。显示后强制把位图重设为容器
                      // 真实宽度再绘制——空态文字与真实数据图统一走这一条路径。
                      requestAnimationFrame(function() {
                        try {
                          const cv = charts[idx];
                          if (!cv) return;
                          const w = Math.floor(cv.clientWidth || 0);
                          if (w > 50) {
                            const ratio = window.devicePixelRatio || 1;
                            cv.width = Math.round(w * ratio);
                            const h = Math.floor(cv.getBoundingClientRect().height || 170);
                            cv.height = Math.max(1, Math.round(h * ratio));
                          }
                        } catch (e) {}
                        redraw(idx);
                        scheduleEnsure(8, 400);             // 兜底自愈（空白检测→按真实宽度重画）
                      });
                    }
                    window.__bkShowChart = showChart;
                    // 数据晚到时从「三张全显」自动恢复三合一（2026-08-30）
                    if (!window.__bkLrsPatched && typeof loadReportFromServer === 'function') {
                      window.__bkLrsPatched = 1;
                      const _lrs = loadReportFromServer;
                      loadReportFromServer = function() {
                        const p = _lrs.apply(this, arguments);
                        try { if (p && p.then) p.then(function() {
                          if (window.__bkShowAll && hasReportData()) {
                            window.__bkShowAll = false;
                            showChart(window.__bkChartIdx || 0);
                          }
                        }); } catch (e) {}
                        return p;
                      };
                    }
                    // ★ 走势图自愈：症状是「框在、里面全白」。
                    // 只要检测到当前这张图是空白（位图宽高为 0 或像素全透明），
                    // 就按容器的真实宽度重设 canvas 位图并立刻重绘——不论根因是
                    // 布局未就绪、切页返回位图丢失，还是数据晚到，都能自愈。
                    function isBlankCanvas(cv) {
                      try {
                        if (!cv || !cv.width || !cv.height) return true;
                        const r = cv.getBoundingClientRect();
                        if ((r.width || 0) < 50) return true;
                        const c2 = cv.getContext('2d');
                        if (!c2) return true;
                        const d = c2.getImageData(0, 0, cv.width, cv.height).data;
                        for (let p = 3; p < d.length; p += 400) { if (d[p] !== 0) return false; }
                        return true;
                      } catch (e) { return true; }
                    }
                    function ensureChart() {
                      try {
                        const i = window.__bkChartIdx || 0;
                        const cv = charts[i];
                        if (!cv) return false;
                        if (!isBlankCanvas(cv)) return true;          // 已经有内容，不用管
                        const sec = secs[i];
                        const w = Math.floor(cv.clientWidth || (sec && sec.clientWidth) || cv.getBoundingClientRect().width || 0);
                        if (w < 50) return false;                      // 容器还没宽度，等下一轮
                        const ratio = window.devicePixelRatio || 1;
                        const h = Math.floor(cv.getBoundingClientRect().height || 170) || 170;
                        cv.width = Math.floor(w * ratio);
                        cv.height = Math.max(1, Math.floor(h * ratio));
                        return redraw(i);
                      } catch (e) { return false; }
                    }
                    window.__bkEnsureChart = ensureChart;
                    function scheduleEnsure(times, gap) {
                      let n = 0;
                      const limit = times || 8;
                      const t = setInterval(function() {
                        n++;
                        if (ensureChart() || n >= limit) {
                          clearInterval(t);
                          // 反复补画还是空白：多半是报表数据没到，触发页面自己重新拉一次
                          if (n >= limit && typeof loadReportFromServer === 'function') {
                            try { loadReportFromServer(); } catch (e) {}
                          }
                        }
                      }, gap || 400);
                    }
                    function safeShowChart(i) { showChart(i); scheduleEnsure(10, 400); }
                    // 兜底：①数据比注入晚到 ②返回键 goBack 从历史栈恢复导致 canvas 位图丢失
                    var __bkRedrawTimer = null;
                    function scheduleRedraw() {
                      if (__bkRedrawTimer) { clearInterval(__bkRedrawTimer); __bkRedrawTimer = null; }
                      var n = 0;
                      __bkRedrawTimer = setInterval(function() {
                        n++;
                        if (redraw(window.__bkChartIdx || 0) || n > 10) {
                          clearInterval(__bkRedrawTimer); __bkRedrawTimer = null;
                        }
                      }, 300);
                    }
                    window.addEventListener('pageshow', function(){ scheduleRedraw(); });
                    document.addEventListener('visibilitychange', function() {
                      if (!document.hidden) scheduleRedraw();
                    });
                    secs.forEach((s, j) => {
                      const title = s.querySelector('.chart-title');
                      if (!title || title.querySelector('[data-bk-switch]')) return;
                      const btn = document.createElement('span');
                      btn.setAttribute('data-bk-switch', '1');
                      btn.textContent = '\u21CC';
                      btn.title = '切换走势图（收益/拍品价值/银币）';
                      btn.style.cssText = 'cursor:pointer;margin-left:8px;font-size:14px;user-select:none;';
                      btn.onclick = () => safeShowChart(j + 1);
                      title.appendChild(btn);
                    });
                    safeShowChart(0);   // 默认收益走势 + 自愈补画
                    scheduleRedraw();   // result.json 可能晚于注入到达，轮询补画（最多约 3 秒）
                    // 页面尺寸变化 / 转屏后画布要重画，否则会留着旧位图或空白
                    window.addEventListener('resize', function() { scheduleEnsure(6, 300); });
                    window.addEventListener('orientationchange', function() { setTimeout(function(){ scheduleEnsure(8, 300); }, 500); });
                  }
                }
                // 把"显示赢家"/"显示后三位"两个开关移到筛选区红框位置（btnWins 右侧）
                (function(){
                  if (window.__bkToolbarMoved) return;
                  window.__bkToolbarMoved = 1;
                  const seg = document.querySelector('section.toolbar .seg');
                  const btnWins = document.getElementById('btnWins');
                  const mask = document.getElementById('maskDigitsBtn');
                  const winner = document.getElementById('maskWinnerBtn');
                  if (!seg || !btnWins || !mask || !winner) return;
                  seg.insertBefore(winner, btnWins.nextSibling);
                  seg.insertBefore(mask, btnWins.nextSibling);
                  // 监听按钮文字，同步 active 视觉状态
                  function syncState(){
                    winner.classList.toggle('bk-active', (winner.textContent || '').includes('隐藏'));
                    mask.classList.toggle('bk-active', (mask.textContent || '').includes('隐藏'));
                  }
                  syncState();
                  winner.addEventListener('click', () => setTimeout(syncState, 0));
                  mask.addEventListener('click', () => setTimeout(syncState, 0));
                })();
                // === App 排版兜底：隐藏网页版深色/盈亏色按钮 + 各地图大类表格卡片化 + 图表压高 + 深色 CSS 兜底 ===
                const s2 = document.createElement('style');
                s2.textContent = `
                  html.app-mode #themeToggle, html.app-mode #profitColorToggle { display:none !important; }
                  /* 需求：App 里不要「远程控制」入口（启动估价器走底部红色按钮） */
                  html.app-mode #remotePanelBtn, html.app-mode #remoteModal { display:none !important; }
                  /* 报表页 toolbar 精简：功能按钮已移到 App 原生底部功能条，网页只留筛选控件 */
                  html.app-mode #highValueBtn, html.app-mode #statsBtn, html.app-mode #careerBtn,
                  html.app-mode #itemPriceConfigBtn { display:none !important; }
                  /* 赢家/后三位按钮已移到筛选区，做成开关样式 */
                  html.app-mode #maskDigitsBtn, html.app-mode #maskWinnerBtn { min-height:28px !important; padding:3px 10px !important; font-size:11px !important; border-radius:6px !important; background:var(--panel) !important; border:1px solid var(--line) !important; color:var(--text) !important; margin-left:4px !important; }
                  html.app-mode #maskDigitsBtn.bk-active, html.app-mode #maskWinnerBtn.bk-active { background:var(--accent) !important; color:#fff !important; border-color:var(--accent) !important; }
                  html.app-mode section.toolbar { padding:6px !important; margin-bottom:6px !important; }
                  html.app-mode section.toolbar .seg { display:flex !important; flex-wrap:wrap !important; gap:6px !important; align-items:center !important; margin:0 !important; }
                  html.app-mode section.toolbar .seg > .sub { font-size:11px !important; }
                  html.app-mode section.toolbar .limit { width:64px !important; min-width:0 !important; }
                  html.app-mode section.toolbar .time-input { min-width:0 !important; flex:1 1 120px !important; }
                  html.app-mode section.toolbar .chips { display:flex !important; flex-wrap:nowrap !important; overflow-x:auto !important; gap:5px !important; margin-top:6px !important; }
                  html.app-mode section.toolbar .chips .chip { flex:0 0 auto !important; padding:5px 10px !important; font-size:11px !important; }
                  html.app-mode .trend-canvas { height: 170px !important; }
                  html.app-mode #mapValueStats table { min-width:0 !important; display:block !important; }
                  html.app-mode #mapValueStats table thead { display:none !important; }
                  html.app-mode #mapValueStats table tbody { display:grid !important; grid-template-columns:1fr !important; gap:6px !important; }
                  html.app-mode #mapValueStats table tbody tr { display:grid; grid-template-columns:1fr 1fr; gap:5px 10px; padding:8px 10px !important; margin:0 !important; border:1px solid var(--line) !important; border-radius:8px !important; background:var(--panel) !important; }
                  html.app-mode #mapValueStats table td { padding:0 !important; border:0 !important; text-align:left !important; font-size:12px !important; }
                  html.app-mode #mapValueStats table td::before { display:block !important; color:var(--ink) !important; opacity:0.75 !important; font-size:12px !important; margin-bottom:1px !important; content:attr(data-label); }
                  html.app-mode .inv-count small { font-size:15px !important; color:var(--ink) !important; font-weight:800 !important; margin-left:4px !important; }
                  /* 深色兜底：即使网页无暗色变量，也强制全套深色（与 App 全局 ☀/🌙 联动） */
                  html[data-theme="dark"] {
                    --bg:#12171f !important; --panel:#171f29 !important; --ink:#e8eef4 !important;
                    --muted:#8fa0b3 !important; --line:#2a3646 !important; --accent:#40b99f !important;
                    --accent-2:#e2b93b !important; --good:#27ff9a !important; --bad:#ff5b61 !important;
                    --table-head:#182231 !important; --table-head-ink:#d6e1ef !important; --table-line:#263344 !important;
                    --button-bg:#e2b93b !important; --button-ink:#111722 !important; --button-secondary-bg:#111923 !important;
                    --good-bg:rgba(39,255,154,.10) !important; --bad-bg:rgba(255,91,97,.11) !important;
                    --value-line:#60a5fa !important; --value-line-2:#fbbf24 !important;
                  }
                  html[data-theme="dark"] body, html[data-theme="dark"] .app { background:var(--bg) !important; color:var(--ink) !important; }
                  html[data-theme="dark"] .panel, html[data-theme="dark"] .ctrl-row, html[data-theme="dark"] .toolbar { background:var(--panel) !important; border-color:var(--line) !important; color:var(--ink) !important; }
                  html[data-theme="dark"] .sub, html[data-theme="dark"] .meta, html[data-theme="dark"] .muted { color:var(--muted) !important; }
                  html[data-theme="dark"] input, html[data-theme="dark"] select { background:#1c2633 !important; color:var(--ink) !important; border-color:var(--line) !important; }
                  html[data-theme="dark"] .card { background:var(--panel) !important; border-color:var(--line) !important; }
                  html[data-theme="dark"] table { color:var(--ink) !important; }
                  html[data-theme="dark"] th { background:var(--table-head) !important; color:var(--table-head-ink) !important; }
                  html[data-theme="dark"] td { border-color:var(--table-line) !important; }
                `;
                s2.id = '__bk-report-app-style';
                if (!document.getElementById('__bk-report-app-style')) document.head.appendChild(s2);
                // 各地图大类表格列名注入（供卡片化显示）
                const mvs = document.querySelector('#mapValueStats table');
                if (mvs) {
                  const heads = mvs.querySelectorAll('thead th');
                  mvs.querySelectorAll('tbody tr').forEach(tr => {
                    tr.querySelectorAll('td').forEach((td, k) => {
                      if (heads[k]) td.setAttribute('data-label', heads[k].textContent.trim());
                      if (k === 0) { td.style.fontWeight = '800'; }
                    });
                  });
                }
                // 关键修复：网页里的"生涯数据统查"/"道具价格配置"按钮用的是 window.open(..., "_blank")，
                // 而 App 的 WebView 没有启用多窗口支持，window.open 会被静默忽略 → 点击毫无反应。
                // 这里覆盖 window.open，改为在当前 WebView 内跳转（通用兜底，网页内所有 _blank 都生效）。
                window.open = function(url, target, features){
                  if (url) { location.href = url; }
                  return null;
                };
                // safeClick：点击后若 600ms 内未发生导航，则 fallback 直接跳转
                window.safeClick = function(id, fallbackUrl){
                  const el = document.getElementById(id);
                  if (!el) { if (fallbackUrl) location.href = fallbackUrl; return false; }
                  let navigated = false;
                  window.addEventListener('beforeunload', function(){ navigated = true; }, { once: true });
                  try { el.click(); } catch(e) {}
                  setTimeout(function(){ if (!navigated && fallbackUrl) location.href = fallbackUrl; }, 600);
                  return true;
                };
              } catch (e) {}
            })();
        """

        // 生涯页注入：各地点场次 / 各地图大类 / 24h均值 三合一⇌（每个标题都带 ⇌）；高价值物品独立常显
        const val CAREER_ENHANCE_JS = """
            (function(){
              try {
                const MARK = "__bkAppEnh2";
                // 参与切换的 3 个面板：各地点场次 / 各地图大类 / 24h均值（高价值物品不参与，保持常显）
                const secs = Array.from(document.querySelectorAll('section.panel')).filter(s =>
                  s.querySelector('#mapCounts') || s.querySelector('#careerMapValueStats') || s.querySelector('#hourlyAverages'));
                if (secs.length < 3) return;   // 页面未就绪：不置 MARK，让 800ms 后补注入有机会执行
                if (window[MARK]) return;
                window[MARK] = 1;
                let idx = 0;
                function showCareer(i) {
                  idx = ((i % 3) + 3) % 3;
                  secs.forEach((s, j) => { s.style.display = (j === idx) ? '' : 'none'; });
                }
                secs.forEach((s, j) => {
                  const h2 = s.querySelector('h2');
                  if (!h2 || h2.querySelector('[data-bk-switch]')) return;
                  const btn = document.createElement('span');
                  btn.setAttribute('data-bk-switch', '1');
                  btn.textContent = '\u21CC';
                  btn.title = '切换：各地点场次 / 各地图大类 / 24h拍品均价';
                  btn.style.cssText = 'cursor:pointer;margin-left:8px;font-size:14px;user-select:none;';
                  btn.onclick = () => showCareer(j + 1);
                  h2.appendChild(btn);
                });
                showCareer(0);   // 默认各地点场次

                // === 生涯页整体 App 化排版（消除右划，卡片化所有宽表格） ===
                const style = document.createElement('style');
                style.textContent = `
                  html.app-mode body { padding: 10px !important; }
                  html.app-mode header { flex-direction: column; align-items: stretch; gap: 6px !important; margin-bottom: 8px !important; }
                  html.app-mode h1 { font-size: 20px !important; }
                  html.app-mode h2 { font-size: 15px !important; margin-bottom: 6px !important; }
                  html.app-mode .sub { font-size: 11px !important; }
                  html.app-mode .panel, html.app-mode .toolbar { padding: 10px !important; margin-bottom: 8px !important; }
                  html.app-mode .grid { grid-template-columns: repeat(2, minmax(0,1fr)) !important; gap: 6px !important; }
                  html.app-mode .metric { padding: 8px 10px !important; }
                  html.app-mode .metric .value { font-size: 18px !important; margin-top: 2px !important; }
                  html.app-mode button, html.app-mode input, html.app-mode select { min-height: 32px !important; font-size: 12px !important; }
                  /* 生涯局内明细：卡片化（2026-08-30 用户要求）。
                     虚拟滚动按固定卡片高度 CARD_H=92 计算（渲染函数已同步整体替换，
                     页面 ROW_H=44 是顶层 const 改不了，只能换函数）。
                     每卡 3×2 字段：时间/地图/赢家 | 拍下物品/展示盈亏/我的盈亏（赢家列回归）。
                     ⚠️ 安12 教训：这里【不设 tr 背景】——拿仓红盈绿亏底色由 injectTouchFeel
                     的 !important 规则负责，谁都不会被这条规则盖掉。 */
                  html.app-mode #careerTableScroll { max-height: 62vh !important; overflow: auto !important; }
                  html.app-mode #careerTableScroll table { min-width: 0 !important; display: block !important; width: 100% !important; font-size: 12px !important; }
                  html.app-mode #careerTableScroll table thead { display: none !important; }
                  html.app-mode #careerTableScroll table tbody { display: block !important; }
                  html.app-mode #careerTableScroll table tbody tr:not(.vs-spacer) {
                    display: grid !important; grid-template-columns: repeat(3, 1fr) !important;
                    gap: 1px 10px !important; height: 92px !important; box-sizing: border-box !important;
                    padding: 8px 10px 2px !important; margin: 0 0 6px 0 !important;
                    border: 1px solid var(--line) !important; border-radius: 10px !important;
                    overflow: hidden !important;
                  }
                  html.app-mode #careerTableScroll table tbody tr.vs-spacer { display: block !important; padding: 0 !important; margin: 0 !important; border: 0 !important; background: transparent !important; }
                  html.app-mode #careerTableScroll table tbody tr.vs-spacer td { display: none !important; }
                  html.app-mode #careerTableScroll table td { display: block !important; padding: 0 !important; border: 0 !important; height: auto !important; min-height: 0 !important; vertical-align: top !important; text-align: left !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
                  html.app-mode #careerTableScroll table td::before { display: block !important; color: var(--muted) !important; font-size: 9px !important; line-height: 1.15 !important; content: attr(data-label); }
                  html.app-mode #careerTableScroll table td.won-items { overflow: visible !important; }
                  html.app-mode #careerTableScroll table .won-toggle { min-height: 0 !important; height: auto !important; padding: 2px 8px !important; margin: 0 !important; line-height: 1.3 !important; }
                  /* 主表列裁剪：隐藏 回合/赢家/道具/赢家出价/对手最高价，保留核心 7 列 */
                  html.app-mode #careerTableScroll table tbody td:nth-child(2),
                  html.app-mode #careerTableScroll table tbody td:nth-child(5),
                  html.app-mode #careerTableScroll table tbody td:nth-child(7),
                  html.app-mode #careerTableScroll table tbody td:nth-child(8),
                  html.app-mode #careerTableScroll table tbody td:nth-child(9),
                  html.app-mode #careerTableScroll table tbody td:nth-child(10) { display: none !important; }
                  /* 其它 4 个面板仍保持卡片化（横向滚动已经处理，这里只恢复主表） */
                  html.app-mode #mapCounts table,
                  html.app-mode #careerMapValueStats table,
                  html.app-mode #hourlyAverages table,
                  html.app-mode #careerHighValueItems table { min-width: 0 !important; display: block; font-size: 12px !important; }
                  html.app-mode #mapCounts table thead,
                  html.app-mode #careerMapValueStats table thead,
                  html.app-mode #hourlyAverages table thead,
                  html.app-mode #careerHighValueItems table thead { display: none !important; }
                  html.app-mode #mapCounts table tbody,
                  html.app-mode #careerMapValueStats table tbody,
                  html.app-mode #hourlyAverages table tbody,
                  html.app-mode #careerHighValueItems table tbody { display: block !important; }
                  html.app-mode #mapCounts table tbody tr,
                  html.app-mode #careerMapValueStats table tbody tr,
                  html.app-mode #hourlyAverages table tbody tr,
                  html.app-mode #careerHighValueItems table tbody tr {
                    display: grid; grid-template-columns: 1fr 1fr; gap: 5px 10px;
                    padding: 8px 10px !important; margin-bottom: 6px !important;
                    border: 1px solid var(--line) !important; border-radius: 8px !important;
                    background: var(--panel) !important;
                  }
                  html.app-mode #mapCounts table td,
                  html.app-mode #careerMapValueStats table td,
                  html.app-mode #hourlyAverages table td,
                  html.app-mode #careerHighValueItems table td { padding: 0 !important; border: 0 !important; height: auto !important; text-align: left !important; font-size: 12px !important; }
                  html.app-mode #mapCounts table td::before,
                  html.app-mode #careerMapValueStats table td::before,
                  html.app-mode #hourlyAverages table td::before,
                  html.app-mode #careerHighValueItems table td::before { display: block !important; color: var(--ink) !important; opacity: 0.75 !important; font-size: 11px !important; margin-bottom: 1px !important; content: attr(data-label); }
                  /* 高价值物品展开行的滚动容器恢复 */
                  html.app-mode .occ-scroll { max-height: 240px !important; overflow: auto !important; }
                  /* 拿仓行底色规则已上移到 injectTouchFeel（2026-08-30 安12）：
                     本块带 secs.length<3 时序守卫，可能整块不注入，靠不住；
                     底色必须放无条件注入的样式表才能保证卡片/表格两种渲染下都生效。 */
                `;
                /* 明细卡片化渲染（2026-08-30）：页面 ROW_H=44 是顶层 const 改不了，
                   顶层 function 声明即 window 属性 → 整体替换为按卡片高度 CARD_H=92
                   （卡高 86 + 卡间距 6）计算的版本，页面内所有调用同步生效。
                   拿仓行 win-positive/win-negative 类由页面 buildCareerRowsHtml 照常
                   生成，配色底色由 injectTouchFeel 的 !important 规则着色（安12）。 */
                try {
                  if (typeof currentFilteredRows !== 'undefined' && typeof buildCareerRowsHtml === 'function') {
                    const CARD_H = 98;   // 卡高 92 + 卡间距 6
                    window.__bkCardH = CARD_H;
                    window.renderVirtualRows = function() {
                      const total = currentFilteredRows.length;
                      if (!total) {
                        careerTableBody.innerHTML = '<tr><td colspan="12">暂无数据库记录</td></tr>';
                        if (typeof updateCareerRowStatus === 'function') updateCareerRowStatus();
                        return;
                      }
                      const scrollTop = careerTableScroll.scrollTop;
                      const viewport = careerTableScroll.clientHeight;
                      let start = Math.floor(scrollTop / CARD_H) - VBUFFER;
                      if (start < 0) start = 0;
                      let end = start + Math.ceil(viewport / CARD_H) + VBUFFER * 2;
                      if (end > total) end = total;
                      careerTableBody.innerHTML =
                        '<tr class="vs-spacer" style="height:' + (start * CARD_H) + 'px"><td colspan="12"></td></tr>' +
                        buildCareerRowsHtml(currentFilteredRows.slice(start, end)) +
                        '<tr class="vs-spacer" style="height:' + ((total - end) * CARD_H) + 'px"><td colspan="12"></td></tr>';
                      if (typeof updateCareerRowStatus === 'function') updateCareerRowStatus();
                      careerLoadMoreBtn.style.display = scrollTop > 300 ? '' : 'none';
                      careerLoadMoreBtn.textContent = '↑ 回到顶部';
                    };
                    window.updateCareerRowStatus = function() {
                      const total = currentFilteredRows.length;
                      if (!total) { careerRowStatus.textContent = '共 0 场'; return; }
                      const visible = Math.min(total, Math.ceil(careerTableScroll.clientHeight / CARD_H) + VBUFFER * 2);
                      careerRowStatus.textContent = '共 ' + fmt(total) + ' 场（卡片视图：仅渲染约 ' + visible + ' 张卡片，滚动流畅）';
                    };
                    renderVirtualRows();
                  }
                } catch (e3) {}
                style.id = '__bk-career-app-style';
                if (!document.getElementById('__bk-career-app-style')) document.head.appendChild(style);

                // 给所有表格单元格注入列名标签，供 ::before 显示（第一列保留标签并加粗）
                document.querySelectorAll('table').forEach(tbl => {
                  const headCells = tbl.querySelectorAll('thead th');
                  tbl.querySelectorAll('tbody tr').forEach(tr => {
                    const tds = tr.querySelectorAll('td');
                    tds.forEach((td, k) => {
                      const label = headCells[k] ? headCells[k].textContent.trim() : '';
                      if (label) td.setAttribute('data-label', label);
                      if (k === 0) { td.style.fontWeight = '800'; }
                    });
                  });
                });
                // 高价值物品：覆盖 renderOccList，分片渲染避免一次性渲染过多 DOM；同时强制 occ-row hidden 生效
                const occStyle = document.createElement('style');
                occStyle.textContent = 'html.app-mode #careerHighValueItems table tbody tr.occ-row { display:block !important; } html.app-mode #careerHighValueItems table tbody tr.occ-row[hidden] { display:none !important; } html.app-mode #careerHighValueItems table tbody tr.occ-row td { display:block !important; width:100% !important; padding:0 !important; }';
                document.head.appendChild(occStyle);
                window.renderOccList = function(occId) {
                  const listEl = document.getElementById('occlist-' + occId);
                  if (!listEl || listEl.dataset.filled) return;
                  const gids = (window.careerOccMap && window.careerOccMap[occId]) || [];
                  const sorted = gids.slice().sort(function(a,b){ return Number((window.games && window.games[b] && window.games[b].ts) || 0) - Number((window.games && window.games[a] && window.games[a].ts) || 0); });
                  const PAGE = 20;
                  let idx = 0;
                  function renderMore(){
                    const slice = sorted.slice(idx, idx + PAGE);
                    if (!slice.length) return;
                    const rows = slice.map(function(gid){
                      const g = window.games && window.games[gid];
                      const time = g ? (g.time || (window.formatTs && window.formatTs(g.ts)) || '') : '';
                      return '<div class="occ-item" style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--line);"><span class="occ-time">' + (window.escapeHtml ? window.escapeHtml(time) : time) + '</span><button class="won-toggle" type="button" data-jump-gid="' + gid + '">前往</button></div>';
                    }).join('');
                    listEl.insertAdjacentHTML('beforeend', rows);
                    idx += PAGE;
                    const remains = sorted.length - idx;
                    if (remains > 0) {
                      const more = document.createElement('div');
                      more.className = 'sub';
                      more.style.cssText = 'padding:8px 0;text-align:center;cursor:pointer;';
                      more.textContent = '已显示 ' + idx + '/' + sorted.length + ' 局，点击加载更多';
                      more.onclick = function(){ more.remove(); renderMore(); };
                      listEl.appendChild(more);
                    } else if (sorted.length > PAGE) {
                      const tip = document.createElement('div');
                      tip.className = 'sub';
                      tip.style.cssText = 'padding:4px 0;';
                      tip.textContent = '共 ' + sorted.length + ' 局';
                      listEl.appendChild(tip);
                    }
                  }
                  renderMore();
                  listEl.dataset.filled = '1';
                };
                // 拿仓背景/盈亏配色跟随 App 的「红盈绿亏/绿盈红亏」开关（2026-08-30）：
                // 页面启动读的是页面本地 key（App 模式下恒为空 → 恒为绿盈红亏，
                // 拿仓行背景不跟反转），这里用 App 注入的全局 key 重新应用纠正。
                try {
                  if (typeof applyProfitColorMode === 'function') {
                    const m = localStorage.getItem('bidking-global-profit-colors');
                    if (m) applyProfitColorMode(m);
                  }
                } catch (e3) {}
                // 默认显示全部场次（2026-08-30）：早年为万场性能加的「默认只看最近
                // 200 场」已移除——生涯页虚拟滚动只渲染可见行，全量数据不再卡顿；
                // 输入框留空即全部（placeholder 已提示）。
              } catch (e) {}
            })();
        """

        // 道具价格页注入：2 列（竖屏）/ 3 列（横屏/宽屏）响应式网格 + 占满宽度 + 显示保存按钮 + 输入即自动保存
        const val ITEM_ENHANCE_JS = """
            (function(){
              try {
                if (window.__bkItemEnh) return; window.__bkItemEnh = 1;
                const style = document.createElement('style');
                style.textContent = [
                  'html.app-mode body { padding:0 !important; }',
                  'html.app-mode .app { width:100% !important; max-width:none !important; margin:0 !important; padding:10px 8px !important; }',
                  'html.app-mode h1 { font-size:18px !important; margin:0 0 8px !important; }',
                  'html.app-mode .sub { margin:0 0 10px !important; }',
                  'html.app-mode .toolbar { padding:8px 0 !important; }',
                  'html.app-mode .table-wrap { padding:0 !important; overflow:visible !important; border:0 !important; background:transparent !important; box-shadow:none !important; }',
                  'html.app-mode .table-wrap table { min-width:0 !important; display:block !important; width:100% !important; }',
                  'html.app-mode .table-wrap table thead { display:none !important; }',
                  'html.app-mode .table-wrap table tbody { display:grid !important; grid-template-columns:repeat(3, minmax(0,1fr)) !important; gap:8px !important; width:100% !important; }',
                  'html.app-mode .table-wrap table tbody tr { display:flex !important; flex-direction:column !important; gap:3px !important; padding:8px !important; margin:0 !important; align-items:stretch !important; border:1px solid var(--line) !important; border-radius:10px !important; background:var(--panel) !important; }',
                  'html.app-mode .table-wrap table tbody tr > td { width:100% !important; padding:0 !important; border:0 !important; grid-area:auto !important; }',
                  'html.app-mode .table-wrap table tbody tr > td::before { display:block !important; color:var(--muted) !important; font-size:9px !important; margin-bottom:1px !important; content:attr(data-label); }',
                  'html.app-mode .table-wrap table tbody tr > td:nth-child(1) { font-size:13px !important; line-height:1.2 !important; font-weight:700 !important; }',
                  'html.app-mode .table-wrap table tbody tr > td:nth-child(1)::before { content:"道具"; }',
                  'html.app-mode .table-wrap table tbody tr > td:nth-child(2) { font-size:11px !important; }',
                  'html.app-mode .table-wrap table tbody tr > td:nth-child(2)::before { content:"基础价"; }',
                  'html.app-mode .price-input { width:100% !important; height:36px !important; }',
                  'html.app-mode #saveBtn, html.app-mode #clearAllBtn { display:inline-block !important; }'
                ].join('\n');
                document.head.appendChild(style);
                const saveBtn = document.getElementById('saveBtn'); if (saveBtn) saveBtn.textContent = '保存价格';
                const tb = document.getElementById('itemPriceTableBody');
                if (tb) {
                  // 给每个 td 注入列名标签
                  document.querySelectorAll('#itemPriceTableBody tr').forEach(function(tr){
                    const tds = tr.querySelectorAll('td');
                    if (tds.length >= 5) {
                      tds[0].setAttribute('data-label', '道具');
                      tds[1].setAttribute('data-label', '基础价');
                      tds[2].setAttribute('data-label', '自定义单价');
                      tds[3].setAttribute('data-label', '当前数量');
                      tds[4].setAttribute('data-label', '总价值');
                    }
                  });
                  let __t = null;
                  tb.addEventListener('input', function(e){
                    if (!e.target || !e.target.classList || !e.target.classList.contains('price-input')) return;
                    if (__t) clearTimeout(__t);
                    __t = setTimeout(function(){
                      const prices = {};
                      document.querySelectorAll('[data-item-price-cid]').forEach(function(i){
                        prices[i.getAttribute('data-item-price-cid')] = Math.max(0, Math.floor(Number(i.value || 0)));
                      });
                      fetch('/api/item-prices', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ prices }) })
                        .then(function(){ const s=document.getElementById('statusText'); if(s) s.textContent='已自动保存价格（返回统计页后按新价格重算）'; })
                        .catch(function(){});
                    }, 800);
                  });
                }
              } catch(e) {}
            })();
        """

        // 生涯页「前往该局」卡死修复：覆盖 jumpToCareerGame，把平滑滚动(smooth)改为即时(auto)，消除长列表假死。逻辑与原函数一致，零回归风险
        const val CAREER_JUMP_PATCH_JS = """
            (function(){
              try {
                if (window.__bkJumpPatched) return; window.__bkJumpPatched = 1;
                window.jumpToCareerGame = function(gid) {
                  gid = Number(gid);
                  if (typeof currentFilteredRows === 'undefined') return;
                  const pos = currentFilteredRows.findIndex(function(g){ return g && g._idx === gid; });
                  if (pos < 0) {
                    if (typeof alert === 'function') alert('该局当前被筛选条件隐藏（如「只看拍到」或取消了对应地图筛选），无法跳转。请调整筛选后重试。');
                    return;
                  }
                  careerTableScroll.scrollTop = Math.max(0, pos * (window.__bkCardH || ROW_H) - 4);
                  if (typeof renderVirtualRows === 'function') renderVirtualRows();
                  const el = document.getElementById('career-game-' + gid);
                  if (el) {
                    el.scrollIntoView({ behavior: 'auto', block: 'center' });
                    el.classList.remove('career-flash'); void el.offsetWidth; el.classList.add('career-flash');
                    setTimeout(function(){ el.classList.remove('career-flash'); }, 1700);
                  }
                };
              } catch(e) {}
            })();
        """

        // 通用悬浮「回到顶部」按钮（报表/生涯/道具三页共用，滚动超 280px 出现，点按即时回顶）
        const val FAB_JS = """
            (function(){
              try {
                if (window.__bkFab) return; window.__bkFab = 1;
                const fab = document.createElement('button');
                fab.textContent = '↑';
                fab.setAttribute('aria-label', '回到顶部');
                fab.type = 'button';
                fab.style.cssText = 'position:fixed;right:12px;bottom:118px;z-index:99999;width:36px;height:36px;border-radius:50%;border:none;background:rgba(47,127,114,.92);color:#fff;font-size:18px;line-height:36px;text-align:center;box-shadow:0 4px 14px rgba(0,0,0,.3);cursor:pointer;opacity:0;transform:translateY(8px);transition:opacity .18s,transform .18s;pointer-events:none;';
                document.body.appendChild(fab);
                function nearTop(){ const y = window.scrollY || document.documentElement.scrollTop || 0; const sc = document.querySelector('.table-wrap'); const sct = sc ? sc.scrollTop : 0; return (y < 280 && sct < 280); }
                function refresh(){ if(nearTop()){ fab.style.opacity='0'; fab.style.transform='translateY(8px)'; fab.style.pointerEvents='none'; } else { fab.style.opacity='1'; fab.style.transform='translateY(0)'; fab.style.pointerEvents='auto'; } }
                window.addEventListener('scroll', refresh, {passive:true});
                const sc = document.querySelector('.table-wrap'); if (sc) sc.addEventListener('scroll', refresh, {passive:true});
                fab.addEventListener('click', function(){ window.scrollTo({top:0, behavior:'auto'}); const s = document.querySelector('.table-wrap'); if (s) s.scrollTop = 0; });
                refresh();
              } catch(e) {}
            })();
        """

        // 通用表格列名自动补标：覆盖动态生成的表格，避免 App 卡片化后只剩裸数字
        // 原理：MutationObserver 监听 body，新表格/新行出现时自动补 data-label；列名优先来自 thead th，缺失时用兜底映射表
        const val TABLE_LABEL_JS = """
            (function(){
              try {
                if (window.__bkTableLabelObs) return;
                const FALLBACK = {
                  'mapValueStats': ['地图大类','场次','最高拍品','最低拍品','平均拍品'],
                  'mapCounts': ['地点','场次','地点盈亏'],
                  'careerMapValueStats': ['地图大类','场次','最高拍品','最低拍品','平均拍品'],
                  'hourlyAverages': ['时间段','场次','拍品均值'],
                  'careerHighValueItems': ['物品名','数量','单件价值','前往该局']
                };
                function nearestId(el){
                  while(el && el !== document.body){
                    if(el.id) return el.id;
                    el = el.parentElement;
                  }
                  return '';
                }
                function colsOf(tbl){
                  const heads = tbl.querySelectorAll('thead th');
                  if(heads.length) return Array.from(heads).map(function(th){ return th.textContent.trim(); });
                  return FALLBACK[nearestId(tbl)] || [];
                }
                function mark(tbl){
                  // 不做「行数没变就跳过」的省略（2026-08-30 修）：自动刷新重渲染后
                  // 行数往往相同，跳过会导致 data-label 不再补挂 → 卡片字段标签消失
                  const rows = Array.from(tbl.querySelectorAll('tbody tr'));
                  const cols = colsOf(tbl);
                  if(!cols.length) return;
                  rows.forEach(function(tr){
                    const tds = tr.querySelectorAll('td');
                    if(tds.length === 1 && tds[0].hasAttribute('colspan')) return;
                    tds.forEach(function(td,k){
                      if(cols[k]) td.setAttribute('data-label', cols[k]);
                      if(k === 0) td.style.fontWeight = '800';
                    });
                  });
                  tbl.dataset.bkLabeled = '1';
                  tbl.dataset.bkRows = String(rows.length);
                }
                function sweep(){ document.querySelectorAll('table').forEach(mark); }
                sweep();
                let t = null;
                const obs = new MutationObserver(function(){
                  if(t) clearTimeout(t);
                  t = setTimeout(sweep, 120);
                });
                obs.observe(document.body, {childList:true, subtree:true});
                window.__bkTableLabelObs = 1;
              } catch(e) {}
            })();
        """
    }

    /* ================= 系统消息提示（通知权限申请与引导） ================= */

    /* ================= App 全局深色模式（原生 UI + 网页全局主题联动） ================= */
    private fun toggleDarkMode() {
        darkMode = !darkMode
        prefs.edit().putBoolean("dark_mode", darkMode).apply()
        applyAppTheme()
        // 通知所有 WebView：切换全局主题（三页 JS 已实现读全局 key）
        webViews.values.forEach { wv ->
            val js = "(function(){localStorage.setItem('bidking-global-theme','${if (darkMode) "dark" else "light"}');" +
                "document.documentElement.dataset.theme='${if (darkMode) "dark" else "light"}';})();"
            wv.post { wv.evaluateJavascript(js, null) }
        }
    }

    /* 盈亏配色切换（红盈绿亏 ⇄ 绿盈红亏）：写 data-profit-colors 属性，三页 CSS 读它上色
       （与 exe 页面自带 #profitColorToggle 解耦——App 模式页面按钮已被隐藏，由本按钮驱动，且每次加载都强制设上，
        避免属性未设时 --profit-good/--profit-bad 未定义导致盈亏数字无颜色） */
    private fun toggleProfitColors() {
        profitInverted = !profitInverted
        prefs.edit().putBoolean(KEY_PROFIT_INVERTED, profitInverted).apply()
        applyProfitColors()
        val pc = if (profitInverted) "inverted" else "normal"
        webViews.values.forEach { wv ->
            val js = "(function(){document.documentElement.setAttribute('data-profit-colors','$pc');})();"
            wv.post { wv.evaluateJavascript(js, null) }
        }
    }

    // 顶栏小按钮的圆角背景：用 TextView 替代 Button 后需自己画背景，避免 Material3 默认 inset/elevation 撑大宽度
    private fun roundRectDrawable(color: Int, radiusDp: Float): android.graphics.drawable.Drawable {
        return android.graphics.drawable.GradientDrawable().apply {
            shape = android.graphics.drawable.GradientDrawable.RECTANGLE
            setColor(color)
            cornerRadius = dp(radiusDp.toInt()).toFloat()
        }
    }

    private fun applyAppTheme() {
        val bg = if (darkMode) 0xFF12171F.toInt() else 0xFFF4F1EA.toInt()
        val panel = if (darkMode) 0xFF171F29.toInt() else 0xFFFFFFFF.toInt()
        val ink = if (darkMode) 0xFFE8EEF4.toInt() else 0xFF1B2430.toInt()
        val muted = if (darkMode) 0xFF8FA0B3.toInt() else 0xFF69717D.toInt()
        val accent = if (darkMode) 0xFF40B99F.toInt() else 0xFF1F7667.toInt()
        val accentSoft = if (darkMode) 0xFF2A3646.toInt() else 0xFFE7E0D2.toInt()
        themeBtn.text = if (darkMode) "🌙" else "☀"
        themeBtn.background = roundRectDrawable(accentSoft, 8f)
        themeBtn.setTextColor(ink)
        if (::refreshBtn.isInitialized) {
            refreshBtn.background = roundRectDrawable(accentSoft, 8f)
            refreshBtn.setTextColor(ink)
        }

        // 根布局统一着色（替换脆弱的 decorView 遍历）
        rootView.setBackgroundColor(bg)
        titleLabel.setTextColor(ink)
        // 各 WebView 背景同步（消除深色下白闪）
        webViews.values.forEach { wv -> wv.setBackgroundColor(if (darkMode) 0xFF12171F.toInt() else 0xFFF4F1EA.toInt()) }
        // 状态栏文字 + 状态栏/导航栏深色同步
        statusView.setTextColor(muted)
        try {
            window.statusBarColor = bg
            window.navigationBarColor = panel
        } catch (_: Exception) {}

        // 弹层输入框文字深色适配（原来固定黑/灰，深色下看不清）
        sheetNameInput.setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else Color.BLACK)
        sheetUrlInput.setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else Color.BLACK)
        sheetPwdInput.setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else Color.BLACK)

        // 通知按钮深色适配
        notifyBtn.background = roundRectDrawable(
            if (darkMode) 0xFF2A3646.toInt() else 0xFF1F7667.toInt(), 8f)
        notifyBtn.setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else 0xFFFFFFFF.toInt())

        // 进度条深色
        webProgress.setBackgroundColor(0x00000000)

        // 底部栏 + 弹层（延迟一点等重建）
        bottomBar.post {
            bottomBar.setBackgroundColor(panel)
            renderBottomBar()
            sheet.setBackgroundColor(panel)
            sheetTitle.setTextColor(ink)
            sheetCancelBtn.backgroundTintList = android.content.res.ColorStateList.valueOf(accentSoft)
            sheetCancelBtn.setTextColor(ink)
            // 弹层主/危险按钮文字保持白（深色下也清晰）
            sheetPrimaryBtn.setTextColor(0xFFFFFFFF.toInt())
            sheetDangerBtn.setTextColor(0xFFFFFFFF.toInt())
        }
        // 盈亏配色按钮随深色同步底色
        profitBtn.background = roundRectDrawable(accentSoft, 8f)
        profitBtn.setTextColor(ink)
    }

    private fun applyProfitColors() {
        // 按钮显示当前模式。页面 CSS 语义（与 exe 报表页一致）：
        // inverted = 红盈绿亏、normal = 绿盈红亏——此前映射写反，显示「红盈」时
        // 页面实际渲染绿盈，拿仓行背景看起来「不跟配色走」。
        profitBtn.text = if (profitInverted) "红盈" else "绿盈"
        profitBtn.background = roundRectDrawable(
            if (darkMode) 0xFF2A3646.toInt() else 0xFFE7E0D2.toInt(), 8f)
        profitBtn.setTextColor(if (darkMode) 0xFFE8EEF4.toInt() else 0xFF1B2430.toInt())
    }

    private fun loadDarkModePref() {
        darkMode = prefs.getBoolean("dark_mode", false)
        applyAppTheme()
    }

    private fun loadProfitPref() {
        profitInverted = prefs.getBoolean(KEY_PROFIT_INVERTED, false)
        applyProfitColors()
    }

    /* ================= 系统消息提示（通知权限申请与引导） ================= */
    private fun requestNotifyPermission() {
        // Android 13+（API 33）需要运行时权限 POST_NOTIFICATIONS
        if (Build.VERSION.SDK_INT >= 33) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                // 用户之前拒绝过"不再询问"时，直接引导去系统设置
                if (!shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)) {
                    toast("请在系统设置中允许通知权限")
                    openNotificationSettings()
                    return
                }
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
                return
            }
            toast("通知权限已开启 ✅")
        } else {
            // Android 12 及以下：安装时已授权，无需运行时申请
            toast("系统已默认开启通知（Android 12 及以下无需设置）")
        }
    }

    private fun openNotificationSettings() {
        try {
            val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
            startActivity(intent)
        } catch (_: Exception) {
            try {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            } catch (_: Exception) {
                toast("请在系统「设置→应用→BidKing远程」中允许通知")
            }
        }
    }

    // 通知权限申请结果回调：授权成功提示；拒绝则提示低库存通知不会开启
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1001) {
            val granted = grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            toast(if (granted) "通知权限已开启，低库存提醒已就绪 ✅" else "未授权，低库存将不会系统提醒（可点🔔重新开启）")
        }
    }

    /* ================= exe 端刷新自动同步（问题 1：App 无需退出重进） ================= */
    private fun startAutoSyncMonitor() {
        if (autoSyncRunning) return
        autoSyncRunning = true
        thread {
            while (autoSyncRunning) {
                try {
                    if (activeUrl.isNotEmpty() && authToken.isNotEmpty()) {
                        val (httpCode, resp, _) = httpGetEx("$activeUrl/api/status", authToken)
                        if (httpCode == 401 || httpCode == 403) {
                            // P1：令牌作废（多半是电脑端 exe 重启过，令牌是它的内存态）→ 自动重新登录
                            reconnectSilently(activeUrl)
                        } else {
                            val json = try { JSONObject(resp ?: "") } catch (_: Exception) { null }
                            val state = json?.optString("status") ?: ""
                            val code = json?.optInt("code") ?: 0
                            val msg = json?.optString("msg") ?: ""
                            // 完整签名：state|code|msg（msg 含本次新增/跳过局数，连刷内容不同也能识别）
                            val sig = "$state|$code|$msg"
                            val prevState = if (lastSyncState.isEmpty()) "" else lastSyncState.substringBefore("|")
                            val prevDoneMsg = if (prevState == "done") lastSyncState.substringAfterLast("|") else ""
                            // 完成一次解析：① 由非 done 转 done（idle/running→done），或 ② 同态连刷但 msg 变化
                            val fresh = state == "done" && (prevState != "done" || msg != prevDoneMsg)
                            if (fresh) {
                                lastSyncState = sig
                                // P3：当前页软刷新（保滚动位置），后台页只打脏标记，
                                // 切回来时再刷（P2 的 showOrCreateWebView 会消费 dirtyServers）
                                val activeKey = normalizeServerUrl(activeUrl)
                                runOnUiThread {
                                    webViews.forEach { (key, wv) ->
                                        val u = wv.url ?: ""
                                        if (u.contains("bidking_report.html") || u.contains("bidking_career.html")
                                            || u.contains("bidking_item_prices.html")) {
                                            if (key == activeKey) softRefresh(wv)
                                            else dirtyServers.add(key)
                                        }
                                    }
                                }
                            } else {
                                // 其它态（idle/running/error）也更新签名，避免下次 done 误判成「无变化」
                                lastSyncState = sig
                            }
                        }
                    }
                } catch (_: Exception) {}
                Thread.sleep(10_000)   // 10s 轮询
            }
        }
    }

    /* ================= 原生库存通知（App 刚需，同道具只提醒一次，补货后重置） ================= */
    // 已提醒过的道具（补货后移除）。键 = "服务器url|cid"（2026-08-30）：
    // 多服务器时各自独立提醒，A 机的记录不抑制 B 机同道具的告警
    private val notifiedCids = mutableSetOf<String>()
    private fun startLowStockMonitor() {
        if (lowStockRunning) return
        lowStockRunning = true
        thread {
            while (lowStockRunning) {
                try {
                    if (activeUrl.isNotEmpty() && authToken.isNotEmpty()) {
                        val (httpCode, resp, _) = httpPostEx("$activeUrl/api/lowstock", JSONObject(), authToken)
                        if (httpCode == 401 || httpCode == 403) {
                            reconnectSilently(activeUrl)     // P1：令牌作废 → 自动重新登录
                        } else {
                            val json = try { JSONObject(resp ?: "") } catch (_: Exception) { null }
                            val items = json?.optJSONArray("items")
                            val nowLow = mutableListOf<Pair<String, String>>()  // (cid, name×count)
                            if (items != null) {
                                for (i in 0 until items.length()) {
                                    val it = items.getJSONObject(i)
                                    val cid = it.optString("cid")
                                    val name = it.optString("name")
                                    val count = it.optInt("count")
                                    nowLow.add(cid to "$name（${count}个）")
                                }
                            }
                            // 键前缀：按服务器隔离提醒状态（多服务器互不干扰）
                            val prefix = activeUrl + "|"
                            // 1) 补货重置：当前服务器已提醒但本次数量>low（不在低库存列表）的 cid，从已提醒集合移除
                            //    （只清理当前服务器的记录，其他服务器的保持不动）
                            val stillLow = nowLow.map { prefix + it.first }.toSet()
                            notifiedCids.removeAll { it.startsWith(prefix) && it !in stillLow }
                            // 2) 只提醒「新出现」的低库存道具（同道具同服务器不重复打扰）
                            val fresh = nowLow.filter { prefix + it.first !in notifiedCids }
                            if (fresh.isNotEmpty() && notifying.compareAndSet(false, true)) {
                                try {
                                    fresh.forEach { (cid, text) -> notifyLowStock(cid, text) }
                                    val names = fresh.joinToString("、") { it.second }
                                    statusRun("低库存：$names")
                                    fresh.forEach { notifiedCids.add(prefix + it.first) }
                                } finally {
                                    notifying.set(false)
                                }
                            }
                        }
                    }
                } catch (_: Exception) {}
                Thread.sleep(30_000)
            }
        }
    }

    private fun notifyLowStock(cid: String, text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            android.app.Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") android.app.Notification.Builder(this)
        }
        val tab = activeTabName()
        val src = if (tab.isNotEmpty()) " · $tab" else ""
        val n = builder
            .setContentTitle("BidKing 道具库存告急$src")
            .setContentText("道具数量≤$LOW，请及时补充：$text")
            .setSmallIcon(com.bidking.remote.R.drawable.ic_stat_crown)
            .setAutoCancel(true)
            .build()
        try { nm.notify(cid.hashCode() and 0x7fffffff, n) } catch (_: Exception) {}
    }

    /* ================= 远程启动估价器 =================
       手机只能"触发启动"，启动哪个程序完全由 exe 端 .bidking_launcher.json 白名单决定，
       App 不传任何路径，避免远程执行任意程序。 ================= */
    private var launchWatchdog: Handler? = null
    @Volatile private var launchWatchdogRunning = false
    private var launchAttemptId = 0        // 每次点「启动估价器」自增：用于"同一次失败只提醒一次"
    private var lastNotifiedAttempt = -1
    private var launchNotifySeq = 1000     // 通知 id 自增：每次失败都是通知栏里独立的一条（不再互相覆盖）
    private var launchProbeMiss = 0        // 2026-08-31 P0：连续拿不到启动状态的次数（≥3 收工，别无限空转）
    private var launchIdleCount = 0        // 2026-08-31 P0：连续读到 idle 的次数（≥2 收工，不再空转 4 分钟）

    private fun launchEstimator() {
        if (activeUrl.isEmpty()) { toast("还没连接服务器"); return }
        if (authToken.isEmpty()) { toast("连接已失效，请重新连接"); return }
        launchAttemptId++
        toast("正在让电脑启动估价器…")
        Thread {
            val resp = httpPost("$activeUrl/api/launch", JSONObject(), authToken)
            val json = try { JSONObject(resp ?: "") } catch (_: Exception) { null }
            if (json == null) {
                runOnUiThread { toast("启动请求失败：连不上电脑") }
                return@Thread
            }
            val state = json.optString("state", "")
            val error = json.optString("error", "")
            val message = json.optString("message", "")
            runOnUiThread {
                when (state) {
                    "running" -> toast("估价器已在运行，已置顶")
                    "confirming" -> { toast("已启动，正在自动确认卡密"); startLaunchWatchdog() }
                    "launching" -> { toast("启动指令已发送"); startLaunchWatchdog() }
                    "error" -> toast("启动失败：" + (if (error.isNotEmpty()) error else "未知错误"))
                    else -> toast("状态：$state")
                }
                if (message.isNotEmpty()) setLaunchStatus("启动：$message")
            }
        }.start()
    }

    /** 启动后每 5 秒查一次状态，第 4 分钟做最终判定；没活下来就发系统通知。
     *  为什么是 4 分钟：exe 端现在会在整个 3 分钟窗口里持续处理卡密弹窗，
     *  App 要给足时间，否则正当程序还在确认弹窗时就被判成"启动失败"。 */
    private fun startLaunchWatchdog() {
        launchWatchdogRunning = true
        launchProbeMiss = 0      // 每轮启动重置计数（P0）
        launchIdleCount = 0
        launchWatchdog?.removeCallbacksAndMessages(null)
        launchWatchdog = Handler(Looper.getMainLooper())
        val poll = object : Runnable {
            var count = 0
            override fun run() {
                if (!launchWatchdogRunning) return
                checkLaunchStatus(count >= 48)      // 48 × 5 秒 = 4 分钟
                count++
                if (count <= 48) launchWatchdog?.postDelayed(this, 5000)
                else stopLaunchWatchdog("")         // 到点收工，状态栏交还连接层（P0）
            }
        }
        launchWatchdog?.post(poll)
    }

    /** 统一停表：停轮询 + 清消息队列 + 复位启动层状态栏。
     *  传 "" = 把状态栏完整交还连接层。
     *  ⚠️ 安15：任何提前 return 的分支都必须走这里 —— 原实现在「令牌为空 /
     *  JSON 解析失败」时直接 return@Thread，既不停表也不复位文字，状态栏被
     *  「启动中：idle」永久占住，用户只能彻底退出 App 才恢复。 */
    private fun stopLaunchWatchdog(finalText: String) {
        launchWatchdogRunning = false
        launchWatchdog?.removeCallbacksAndMessages(null)
        if (finalText.isEmpty()) clearLaunchStatus() else setLaunchStatus(finalText)
    }

    private fun checkLaunchStatus(finalCheck: Boolean) {
        Thread {
            // P0 修复：连接都没了，启动监控就没有意义，立刻收工并交还状态栏。
            // （原实现这里直接 return，状态栏从此卡死在「启动中：idle」）
            if (activeUrl.isEmpty() || authToken.isEmpty()) {
                stopLaunchWatchdog("")
                return@Thread
            }
            val resp = httpGet("$activeUrl/api/launch/status", authToken)
            val json = try { JSONObject(resp ?: "") } catch (_: Exception) { null }
            if (json == null) {
                // 连续 3 次拿不到状态（电脑关了 / 网络断了）→ 收工，别无限空转
                launchProbeMiss++
                if (launchProbeMiss >= 3) stopLaunchWatchdog("启动状态查不到（先确认电脑是否还开着）")
                return@Thread
            }
            launchProbeMiss = 0
            val state = json.optString("state", "")
            val error = json.optString("error", "")
            val pending = json.optBoolean("confirm_pending", false)
            val detail = json.optString("confirm_detail", "")
            // idle = 这次启动压根没进入流程。宽限 2 轮（10 秒）给 exe 写状态的时间，
            // 然后立刻收工 —— 原实现会为此白白空转满 4 分钟，且每 5 秒刷一次状态栏。
            if (state == "idle") {
                launchIdleCount++
                if (launchIdleCount >= 2) {
                    stopLaunchWatchdog("")
                    toast("估价器没有进入启动流程，请在电脑上确认")
                }
                return@Thread
            }
            launchIdleCount = 0
            val hardFail = (state == "exited") || (state == "error")
            val failed = hardFail || (finalCheck && state != "running")
            if (failed) {
                stopLaunchWatchdog("")
                val reason = when {
                    error.isNotEmpty() -> error
                    pending -> "卡密验证窗口没关掉，需要到电脑上手动点「确定」"
                    finalCheck -> "估价器 4 分钟内没有正常运行"
                    else -> "启动失败"
                }
                val shown = if (detail.isNotEmpty()) "$reason（$detail）" else reason
                runOnUiThread { setLaunchStatus("启动失败：$reason") }
                notifyLaunchFailed(shown, launchAttemptId)
            } else if (finalCheck) {
                stopLaunchWatchdog("估价器已启动并正常运行")
            } else {
                val tail = if (detail.isNotEmpty()) " · $detail" else ""
                val newTxt = "启动中：$state$tail"
                // 防闪烁：文案没变就不动 UI（省掉每 5 秒一次的无谓重绘）
                runOnUiThread { if (newTxt != launchStatusText) setLaunchStatus(newTxt) }
            }
        }.start()
    }

    /** 失败通知：每次失败都是通知栏里独立的一条（id 自增，不再被上一条覆盖）；
     *  但同一次启动尝试只发一次，避免 5 秒一轮把通知刷爆。 */
    private fun notifyLaunchFailed(msg: String, attempt: Int) {
        if (attempt == lastNotifiedAttempt) return
        lastNotifiedAttempt = attempt
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            android.app.Notification.Builder(this, LAUNCH_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION") android.app.Notification.Builder(this)
        }
        val tab = activeTabName()
        val src = if (tab.isNotEmpty()) " · $tab" else ""
        val n = builder
            .setContentTitle("估价器启动失败$src")
            .setContentText(msg)
            .setStyle(android.app.Notification.BigTextStyle().bigText(msg))
            .setSmallIcon(com.bidking.remote.R.drawable.ic_stat_crown)
            .setAutoCancel(true)
            .build()
        val id = launchNotifySeq++
        try { nm.notify(id, n) } catch (_: Exception) {}
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val ch = NotificationChannel(CHANNEL_ID, "库存告急", NotificationManager.IMPORTANCE_HIGH)
            nm.createNotificationChannel(ch)
            val ch2 = NotificationChannel(LAUNCH_CHANNEL_ID, "程序启动提醒", NotificationManager.IMPORTANCE_HIGH)
            nm.createNotificationChannel(ch2)
        }
    }

    // 低库存提醒 / 网络恢复提示都归「连接层」（setConnStatus 内部已处理线程切换）
    private fun statusRun(text: String) {
        setConnStatus(text)
    }

    /* ================= 网络监听（C2：断网恢复自动重连） ================= */
    private fun setupNetworkMonitor() {
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager
        netCallback = object : android.net.ConnectivityManager.NetworkCallback() {
            private var lastReconnect = 0L
            override fun onAvailable(network: android.net.Network) {
                super.onAvailable(network)
                if (activeUrl.isEmpty()) return
                val now = System.currentTimeMillis()
                if (now - lastReconnect < 5000) return   // 节流：避免网络抖动反复重连
                lastReconnect = now
                runOnUiThread {
                    if (authToken.isEmpty()) {
                        statusRun("网络已恢复，自动重连…")
                        connect(activeUrl, activeTabPwd())
                    } else {
                        webViews[activeUrl]?.reload()
                        statusRun("网络已恢复，已刷新页面")
                    }
                }
            }
        }
        val req = android.net.NetworkRequest.Builder()
            .addCapability(android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET).build()
        cm.registerNetworkCallback(req, netCallback!!)
    }

    /* ================= 持久化 ================= */
    // 服务器密码等敏感信息加密存储（SE1）：使用 EncryptedSharedPreferences；
    // 首次启动从旧明文偏好迁移（旧全局密码注入到每个服务器），随后清空明文，避免回退崩溃时也能用。
    private fun initEncryptedPrefs(): SharedPreferences {
        return try {
            val masterKey = MasterKey.Builder(this).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
            val enc = EncryptedSharedPreferences.create(
                this, PREFS + "_enc", masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            )
            val legacy = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            if (legacy.all.isNotEmpty()) {
                val edit = enc.edit()
                val oldTabs = legacy.getString(KEY_TABS, null)
                if (oldTabs != null) {
                    val oldPwd = legacy.getString("pwd", "") ?: ""  // 旧全局密码（KEY_PWD 已废弃）
                    try {
                        val arr = JSONArray(oldTabs)
                        val out = JSONArray()
                        for (i in 0 until arr.length()) {
                            val o = arr.optJSONObject(i) ?: continue
                            if (!o.has("pwd")) o.put("pwd", oldPwd)
                            out.put(o)
                        }
                        edit.putString(KEY_TABS, out.toString())
                    } catch (_: Exception) { edit.putString(KEY_TABS, oldTabs) }
                }
                if (legacy.contains("dark_mode")) edit.putBoolean("dark_mode", legacy.getBoolean("dark_mode", false))
                if (legacy.contains(KEY_PROFIT_INVERTED)) edit.putBoolean(KEY_PROFIT_INVERTED, legacy.getBoolean(KEY_PROFIT_INVERTED, false))
                edit.apply()
                legacy.edit().clear().apply()
            }
            enc
        } catch (e: Exception) {
            // 加密不可用（极少数机型）则退回明文，保证不崩溃
            getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        }
    }

    // 电池优化白名单引导（N2）：仅首次弹系统授权框，避免后台状态轮询/通知被系统限频
    private fun maybeRequestBatteryOptim() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
        val pm = getSystemService(Context.POWER_SERVICE) as? android.os.PowerManager ?: return
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        if (prefs.getBoolean("battery_prompted", false)) return
        prefs.edit().putBoolean("battery_prompted", true).apply()
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            })
        } catch (_: Exception) {}
    }

    private fun loadTabs() {
        val raw = prefs.getString(KEY_TABS, "[]") ?: "[]"
        try {
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                tabs += ServerTab(o.optString("name"), normalizeServerUrl(o.optString("url")), o.optString("pwd"))
            }
        } catch (_: Exception) { tabs.clear() }
    }

    // 地址规范化（2026-08-29）：只保留 协议+主机+端口，丢弃多余路径/查询/结尾斜杠。
    // 用户粘贴的地址常带尾巴（/api、/bidking_report.html、结尾 / 等），拼接口时变成
    // /xxx/api/auth 之类落进服务器的令牌拦截器 → 返回 unauthorized，App 只能显示
    // 一句看不懂的 "连接失败：unauthorized"。
    private fun normalizeServerUrl(raw: String): String {
        var u = raw.trim().trimEnd('/')
        if (u.isEmpty()) return u
        if (!u.contains("://")) u = if (u.lowercase().contains("ngrok")) "https://$u" else "http://$u"
        return try {
            val url = URL(u)
            val port = if (url.port > 0) ":${url.port}" else ""
            "${url.protocol}://${url.host}$port"
        } catch (_: Exception) { u }
    }

    private fun saveTabs() {
        val arr = JSONArray()
        tabs.forEach { arr.put(JSONObject().put("name", it.name).put("url", it.url).put("pwd", it.pwd)) }
        prefs.edit().putString(KEY_TABS, arr.toString()).apply()
    }

    /* ================= HTTP ================= */
    // GET 请求（带 token）：用于 /api/status 轮询等。失败返回 null，与空响应体区分
    private fun httpGet(url: String, token: String = ""): String? {
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(url).openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.setRequestProperty("ngrok-skip-browser-warning", "1")
            if (token.isNotEmpty()) conn.setRequestProperty("X-Auth-Token", token)
            val code = conn.responseCode
            if (code < 200 || code >= 300) return null
            val stream: InputStream? = conn.inputStream
            val out = ByteArrayOutputStream()
            stream?.copyTo(out)
            out.toString(Charsets.UTF_8.name())
        } catch (e: Exception) { null }
        finally { conn?.disconnect() }
    }

    // POST 请求（带 token）：失败返回 null
    private fun httpPost(url: String, body: JSONObject, token: String = ""): String? {
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(url).openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("ngrok-skip-browser-warning", "1")
            if (token.isNotEmpty()) conn.setRequestProperty("X-Auth-Token", token)
            conn.outputStream.write(body.toString().toByteArray(Charsets.UTF_8))
            val code = conn.responseCode
            if (code < 200 || code >= 300) return null
            val stream: InputStream? = conn.inputStream
            val out = ByteArrayOutputStream()
            stream?.copyTo(out)
            out.toString(Charsets.UTF_8.name())
        } catch (e: Exception) { null }
        finally { conn?.disconnect() }
    }

    // 带 HTTP 状态码的 GET：返回 (状态码, 响应体, 失败类别)。
    // 4xx/5xx 读 errorStream；网络失败返回 (-1, null, 类别)：
    //   dns=域名不存在（ngrok 重启后地址会变）、conn=连不上/超时、net=其他网络错误
    private fun httpGetEx(url: String, token: String = ""): Triple<Int, String?, String> {
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(url).openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.setRequestProperty("ngrok-skip-browser-warning", "1")
            if (token.isNotEmpty()) conn.setRequestProperty("X-Auth-Token", token)
            val code = conn.responseCode
            val stream: InputStream? = if (code in 200..299) conn.inputStream else conn.errorStream
            val out = ByteArrayOutputStream()
            stream?.copyTo(out)
            Triple(code, out.toString(Charsets.UTF_8.name()), "")
        } catch (e: java.net.UnknownHostException) { Triple(-1, null, "dns") }
        catch (e: java.net.ConnectException) { Triple(-1, null, "conn") }
        catch (e: java.net.SocketTimeoutException) { Triple(-1, null, "conn") }
        catch (e: java.io.IOException) { Triple(-1, null, "conn") }
        catch (e: Exception) { Triple(-1, null, "net") }
        finally { conn?.disconnect() }
    }

    // 带 HTTP 状态码的 POST：返回 (状态码, 响应体, 失败类别)，同 httpGetEx
    private fun httpPostEx(url: String, body: JSONObject, token: String = ""): Triple<Int, String?, String> {
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(url).openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("ngrok-skip-browser-warning", "1")
            if (token.isNotEmpty()) conn.setRequestProperty("X-Auth-Token", token)
            conn.outputStream.write(body.toString().toByteArray(Charsets.UTF_8))
            val code = conn.responseCode
            val stream: InputStream? = if (code in 200..299) conn.inputStream else conn.errorStream
            val out = ByteArrayOutputStream()
            stream?.copyTo(out)
            Triple(code, out.toString(Charsets.UTF_8.name()), "")
        } catch (e: java.net.UnknownHostException) { Triple(-1, null, "dns") }
        catch (e: java.net.ConnectException) { Triple(-1, null, "conn") }
        catch (e: java.net.SocketTimeoutException) { Triple(-1, null, "conn") }
        catch (e: java.io.IOException) { Triple(-1, null, "conn") }
        catch (e: Exception) { Triple(-1, null, "net") }
        finally { conn?.disconnect() }
    }

    /* ================= 设备配对（2026-08-29 新增）=================
       新 exe 要求「密码 + 电脑端人工同意 + 设备密钥」三关，只有密码只能拿到只读权限。
       流程：已配对 → 挑战-响应登录；未配对 → 用密码发起配对 → 等电脑端点同意 → 拿密钥 → 登录；
       三步都失败才回退老式密码登录（此时为只读，旧行为）。 */

    // 本机唯一设备标识：优先用 ANDROID_ID（Android 8+ 按应用签名+设备绑定，
    // 清数据/重装后不变 → 「同一设备只需电脑端同意一次」在重装后依然成立）；
    // 拿不到再退回随机 UUID（仅存于本应用数据，清数据即变）。
    private fun getPairDeviceId(): String {
        var id = prefs.getString(KEY_DEVICE_ID, "") ?: ""
        if (id.isEmpty()) {
            val aid = try {
                android.provider.Settings.Secure.getString(
                    contentResolver, android.provider.Settings.Secure.ANDROID_ID)
            } catch (_: Exception) { null }
            // 9774d56d682e549c 是旧系统 universally 相同的无效值，排除
            id = if (!aid.isNullOrBlank() && aid != "9774d56d682e549c") "and-$aid"
                 else java.util.UUID.randomUUID().toString()
            prefs.edit().putString(KEY_DEVICE_ID, id).apply()
        }
        return id
    }

    // 每台电脑的 master 不同、密钥也不同，用服务器地址的 hash 作存储键（避开 url 里的 : / 等字符）
    private fun devKeyOf(server: String) = KEY_DEVKEY_PREFIX + server.hashCode().toString(16)
    private fun getDeviceKey(server: String) = prefs.getString(devKeyOf(server), "") ?: ""
    private fun saveDeviceKey(server: String, key: String) = prefs.edit().putString(devKeyOf(server), key).apply()
    private fun clearDeviceKey(server: String) = prefs.edit().remove(devKeyOf(server)).apply()

    private fun hmacSha256(key: String, data: String): String {
        val mac = javax.crypto.Mac.getInstance("HmacSHA256")
        mac.init(javax.crypto.spec.SecretKeySpec(key.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(data.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }

    // 挑战-响应登录：GET challenge 取 nonce → HMAC 签名 → POST /api/auth 换令牌（密码不上网）
    private fun deviceLogin(server: String): Boolean {
        val did = getPairDeviceId()
        val key = getDeviceKey(server)
        if (key.isEmpty()) return false
        val ch = httpGet("$server/api/auth/challenge?device_id=$did", "") ?: return false
        val cj = try { JSONObject(ch) } catch (_: Exception) { return false }
        val nonce = cj.optString("nonce", "")
        val sid = cj.optString("server_id", "")
        if (nonce.isEmpty()) return false
        val ts = System.currentTimeMillis() / 1000
        val sig = hmacSha256(key, "auth|$sid|$nonce|$ts")
        val body = JSONObject().put("device_id", did).put("nonce", nonce)
            .put("ts", ts).put("sig", sig)
        val resp = httpPost("$server/api/auth", body) ?: return false
        val j = try { JSONObject(resp) } catch (_: Exception) { return false }
        val tok = j.optString("token", "")
        if (tok.isEmpty()) return false
        // P1：按本次登录的 server 存令牌；设备密钥登录成功即视为已配对
        setToken(server, tok, j.optString("scope", ""), paired = true)
        passEnabled = j.optBoolean("pass_enabled", passEnabled)
        return true
    }

    // 用密码发起配对请求。返回 Triple(rid, 自动重绑定响应, 错误信息)：
    //   rid 非空     → 已排队，等电脑端点「同意」（新设备）
    //   第二项非空   → 服务端确认该设备此前已同意过配对，响应里直接带 device_key+token（免二次同意）
    //   第三项非空   → 服务端拒绝的真实原因（密码错误等），直接给界面显示
    private fun requestPair(server: String, pwd: String): Triple<String?, JSONObject?, String> {
        val body = JSONObject()
            .put("device_id", getPairDeviceId())
            .put("name", android.os.Build.MODEL ?: "Android")
            .put("model", "Android ${android.os.Build.VERSION.RELEASE}")
            .put("pwd", pwd)
        val (code, resp, _) = httpPostEx("$server/api/pair/request", body)
        val j = resp?.let { b -> runCatching { JSONObject(b) }.getOrNull() }
        if (j == null) return Triple(null, null, if (code == -1) "网络错误，请重试" else "电脑返回 HTTP $code")
        if (j.optBoolean("ok")) {
            val auto = if (j.optBoolean("auto_paired")) j else null
            return Triple(j.optString("rid", "").ifEmpty { null }, auto, "")
        }
        return Triple(null, null, j.optString("error", "电脑端拒绝了配对请求"))
    }

    // 轮询配对结果直到电脑端同意/拒绝/超时。返回 (device_key 或 null, 失败原因)。
    private fun pollPairKey(server: String, rid: String, waitMs: Long = 150000): Pair<String?, String> {
        val did = getPairDeviceId()
        val deadline = System.currentTimeMillis() + waitMs
        while (System.currentTimeMillis() < deadline) {
            val r = httpGet("$server/api/pair/status?rid=$rid&did=$did", "")
            if (r != null) {
                val j = try { JSONObject(r) } catch (_: Exception) { null }
                if (j != null) {
                    when (j.optString("state", "")) {
                        "approved" -> {
                            val k = j.optString("device_key", "")
                            if (k.isNotEmpty()) return Pair(k, "")
                        }
                        "rejected" -> return Pair(null, "电脑端点了「拒绝」，请在电脑端重新发起配对")
                        "expired" -> return Pair(null, "配对请求已过期，请重试")
                        "not_found" -> return Pair(null, "配对请求不存在（可能已超时），请重试")
                        // 已配对但本机没有对应密钥（如应用数据被清）：需电脑端撤销后重新配对
                        "already_paired" -> return Pair(null, "设备已配对但本机密钥不一致，请在电脑端「安全与配对」撤销该设备后重试")
                    }
                }
            }
            Thread.sleep(3000)
        }
        return Pair(null, "等待超时（电脑端未点「同意」），请重试并及时在电脑端点「同意」")
    }

    // 忘记此配对：清掉密钥后需要重新走配对流程（同时清令牌）
    private fun forgetPairing(server: String) {
        clearDeviceKey(server)
        setToken(server, "")
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
    private fun toast(msg: String) {
        // 自绘 Toast（2026-08-30）：系统模板的左上角图标由 SystemUI 跨进程加载应用
        // 图标绘制，部分设备/模拟器（MuMu 实测）会回落成默认机器人蓝图图标，
        // 与应用真实图标不一致。改为应用自己的布局 + 应用图标（前台自定义视图
        // 在 Android 11+ 仍合法，被禁的只是「后台」自定义 Toast）；兜底退回系统 Toast。
        runOnUiThread {
            try {
                val v = layoutInflater.inflate(R.layout.toast_branded, null)
                v.findViewById<ImageView>(R.id.toast_icon).setImageResource(R.mipmap.ic_launcher)
                v.findViewById<TextView>(R.id.toast_text).text = msg
                val t = Toast(applicationContext)
                t.view = v
                t.duration = Toast.LENGTH_SHORT
                t.setGravity(Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL, 0, dp(96))
                t.show()
                return@runOnUiThread
            } catch (_: Exception) {}
            Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        }
    }

    /* ================= 原生按压反馈（2026-08-30）=================
       TextView 没有平台默认按压态；此前功能条把缩放动画挂在「点击完成后」播放，
       不跟手，是「披着 app 的网页」手感的另一半来源。改为：手指落下立即缩放
       + 轻震（HapticFeedbackConstants 跟随系统触感设置），抬起/取消恢复。 */
    private fun addPressFeedback(v: android.view.View, scale: Float = 0.92f) {
        v.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                android.view.MotionEvent.ACTION_DOWN -> {
                    view.animate().scaleX(scale).scaleY(scale).setDuration(60).start()
                    view.performHapticFeedback(android.view.HapticFeedbackConstants.VIRTUAL_KEY)
                }
                android.view.MotionEvent.ACTION_UP, android.view.MotionEvent.ACTION_CANCEL ->
                    view.animate().scaleX(1f).scaleY(1f).setDuration(80).start()
            }
            false   // 不消费事件：OnClickListener / LongClickListener 照常工作
        }
    }

    /* ================= 返回键：弹层开着先关弹层；否则回报表首页 / 退出 ================= */
    override fun onBackPressed() {
        if (sheet.visibility == View.VISIBLE) {
            hideSheet()
            return
        }
        // WebView 可后退时优先后退（如从生涯/道具页返回报表），否则交给系统退出
        val wv = webHost.getChildAt(0) as? WebView
        if (wv != null && wv.canGoBack()) {
            wv.goBack()
            return
        }
        // I1：再按一次退出，避免误触返回键直接关掉 App
        val now = System.currentTimeMillis()
        if (now - lastBackPressed < 2000) {
            super.onBackPressed()
        } else {
            lastBackPressed = now
            toast("再按一次返回键退出")
        }
    }

    override fun onDestroy() {
        autoSyncRunning = false
        lowStockRunning = false
        stopLaunchWatchdog("")   // P0：连消息队列一起清掉，不留跨生命周期的轮询
        netCallback?.let {
            try {
                (getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager)
                    .unregisterNetworkCallback(it)
            } catch (_: Exception) { }
        }
        webViews.values.forEach { it.destroy() }
        super.onDestroy()
    }
}