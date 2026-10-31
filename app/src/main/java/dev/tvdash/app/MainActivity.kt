package dev.tvdash.app

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Remote keys:
 * - Back: handled by the page (closes the word card / leaves the news rail). Hold Back ~1s to quit.
 * - Exit (and Escape on a keyboard): quits the app.
 * - Play/Pause: pauses the dashboard.
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())

    private val remoteUrl: String = BuildConfig.DASHBOARD_URL
    private val localUrl = "file:///android_asset/index.html"
    private val remoteHost: String? = Uri.parse(remoteUrl).host
    private var showingFallback = false

    private val reloadEveryMs = 6 * 60 * 60 * 1000L
    private val reloadAfterAwayMs = 30 * 1000L
    private var pausedAt = 0L
    private var backDownAt = 0L

    private val reloader = object : Runnable {
        override fun run() {
            loadDashboard()
            handler.postDelayed(this, reloadEveryMs)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#060A24"))
        setContentView(web)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_NO_CACHE
            useWideViewPort = true
            loadWithOverviewMode = true
        }

        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) useFallback()
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame && response.statusCode >= 400) useFallback()
            }
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                val allowed = url.scheme == "file" || (remoteHost != null && url.host == remoteHost)
                return !allowed
            }
        }

        loadDashboard()
        web.requestFocus()
    }

    private fun loadDashboard() {
        if (remoteUrl.isBlank()) {
            showingFallback = true
            web.loadUrl(localUrl)
        } else {
            showingFallback = false
            web.settings.cacheMode = WebSettings.LOAD_NO_CACHE
            val sep = if (remoteUrl.contains("?")) "&" else "?"
            web.loadUrl(remoteUrl + sep + "t=" + System.currentTimeMillis())
        }
    }

    private fun useFallback() {
        if (!showingFallback) {
            showingFallback = true
            web.settings.cacheMode = WebSettings.LOAD_DEFAULT
            web.loadUrl(localUrl)
        }
    }

    private fun quit() {
        finish()
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        when (event.keyCode) {
            // Remote "Exit" button usually arrives as ESCAPE (some remotes use TV-specific codes).
            KeyEvent.KEYCODE_ESCAPE -> {
                if (event.action == KeyEvent.ACTION_UP) quit()
                return true
            }

            KeyEvent.KEYCODE_BACK -> {
                when (event.action) {
                    KeyEvent.ACTION_DOWN -> {
                        if (event.repeatCount == 0) backDownAt = SystemClock.elapsedRealtime()
                        // Holding Back for about a second also quits.
                        if (SystemClock.elapsedRealtime() - backDownAt > 1000) quit()
                    }
                    KeyEvent.ACTION_UP -> {
                        if (SystemClock.elapsedRealtime() - backDownAt <= 1000) {
                            web.evaluateJavascript("window.tvBack && window.tvBack();", null)
                        }
                    }
                }
                return true
            }

            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> {
                web.dispatchKeyEvent(KeyEvent(event.action, KeyEvent.KEYCODE_SPACE))
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    @Suppress("DEPRECATION")
    private fun hideSystemUi() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION)
    }

    override fun onResume() {
        super.onResume()
        hideSystemUi()
        web.onResume()
        if (pausedAt > 0 && SystemClock.elapsedRealtime() - pausedAt > reloadAfterAwayMs) {
            loadDashboard()
        }
        handler.removeCallbacks(reloader)
        handler.postDelayed(reloader, reloadEveryMs)
    }

    override fun onPause() {
        pausedAt = SystemClock.elapsedRealtime()
        handler.removeCallbacks(reloader)
        web.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        web.destroy()
        super.onDestroy()
    }
}
