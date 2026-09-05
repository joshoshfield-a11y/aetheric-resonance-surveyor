package com.upe78.ars

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat

/**
 * ARS — Aetheric Resonance Surveyor field shell.
 * Offline WebView wrapper. Mic access is wired end-to-end:
 * manifest RECORD_AUDIO -> runtime request -> WebChromeClient grant,
 * without which getUserMedia silently fails inside a WebView.
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.allowFileAccess = true
        web.settings.mediaPlaybackRequiresUserGesture = false
        web.webViewClient = WebViewClient()
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
                val wantsAudio = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                if (!wantsAudio) { request.deny(); return }
                val granted = ContextCompat.checkSelfPermission(
                    this@MainActivity, Manifest.permission.RECORD_AUDIO
                ) == PackageManager.PERMISSION_GRANTED
                if (granted) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    pendingWebPermission = request
                    micPermission.launch(Manifest.permission.RECORD_AUDIO)
                }
            }
        }
        web.loadUrl("file:///android_asset/www/index.html")
        setContentView(web)
    }
}
