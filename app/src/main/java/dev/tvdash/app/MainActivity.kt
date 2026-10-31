package dev.tvdash.app

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
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
 * Full-screen WebView that shows the TV dashboard.
 * - Loads the hosted dashboard (GitHub Pages); falls back to the copy bundled in the APK when offline.
 * - Keeps the screen awake and hides system bars.
 * - Remote Back button is sent to the page as Escape (closes cards) instead of quitting the app.
 * - Reloads every few hours so code changes on the hosted page show up.
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())

    private val remoteUrl: String = BuildConfig.DASHBOARD_URL
    private val localUrl = "file:///android_asset/index.html"
    private val remoteHost: String? = Uri.parse(remoteUrl).host
    private var showingFallback = false

    private val reloadEveryMs = 6 * 60 * 60 * 1000L   // full reload every 6 hours
    private val retryRemoteMs = 10 * 60 * 1000L       // when offline, retry the hosted page every 10 minutes

    private val reloader = object : Runnable {
        override fun run() {
            loadDashboard()
            handler.postDelayed(this, if (showingFallback && remoteUrl.isNotBlank()) retryRemoteMs else reloadEveryMs)
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
            domStorageEnabled = true            // keeps the custom word list (localStorage)
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
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
                // Stay inside the dashboard; ignore links to other sites.
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
            web.loadUrl(remoteUrl)
        }
    }

    private fun useFallback() {
        if (!showingFallback) {
            showingFallback = true
            web.loadUrl(localUrl)
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        when (event.keyCode) {
            KeyEvent.KEYCODE_BACK -> {
                web.dispatchKeyEvent(KeyEvent(event.action, KeyEvent.KEYCODE_ESCAPE))
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
        handler.removeCallbacks(reloader)
        handler.postDelayed(reloader, reloadEveryMs)
    }

    override fun onPause() {
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
