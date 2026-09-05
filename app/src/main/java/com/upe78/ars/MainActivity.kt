package com.upe78.ars

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewClientCompat

/**
 * ARS — Aetheric Resonance Surveyor field shell.
 *
 * v5.0.1 fix: file:// is NOT a reliable secure context in Android WebView —
 * navigator.mediaDevices (mic) and crypto.subtle can be undefined there,
 * which silently killed the mic path. Assets are now served over the
 * https://appassets.androidplatform.net origin via WebViewAssetLoader,
 * a guaranteed secure context. RECORD_AUDIO is pre-requested at startup
 * so the first getUserMedia grant path is already hot.
 */
class MainActivity : ComponentActivity() {

    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingWebPermission: PermissionRequest? = null

    private val filePicker =
        registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            fileCallback?.onReceiveValue(uris.toTypedArray())
            fileCallback = null
        }

    private val micPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                pendingWebPermission?.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            } else {
                pendingWebPermission?.deny()
            }
            pendingWebPermission = null
        }

    private fun hasMicPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val assetLoader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        val web = WebView(this)
        WebView.setWebContentsDebuggingEnabled(true)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.allowFileAccess = false
        web.settings.mediaPlaybackRequiresUserGesture = false

        web.webViewClient = object : WebViewClientCompat() {
            override fun shouldInterceptRequest(
                view: WebView,
                request: WebResourceRequest
            ): WebResourceResponse? = assetLoader.shouldInterceptRequest(request.url)
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                fileCallback = callback
                filePicker.launch(arrayOf("*/*"))
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    val wantsAudio =
                        request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                    if (!wantsAudio) {
                        request.deny()
                        return@runOnUiThread
                    }
                    if (hasMicPermission()) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        pendingWebPermission = request
                        micPermission.launch(Manifest.permission.RECORD_AUDIO)
                    }
                }
            }
        }

        // Pre-request mic permission at startup: the WebView permission grant
        // can only succeed if the app-level RECORD_AUDIO is already held.
        if (!hasMicPermission()) {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }

        web.loadUrl("https://appassets.androidplatform.net/assets/www/index.html")
        setContentView(web)
    }
}
