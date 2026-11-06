package com.tmmstudios.wallpapers

import android.app.Activity
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import com.tmmstudios.wallpapers.upload.ControlClient
import com.tmmstudios.wallpapers.upload.DeviceIdentity
import com.tmmstudios.wallpapers.upload.UploadConfig

class UploadConfigWebActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = WebViewClient()
        }
        setContentView(webView)
        val openPanel = {
            val token = DeviceIdentity.token(this)
            if (token == null) {
                webView.loadData(
                    "<h2>Upload configuration unavailable</h2><p>Connect to the internet and try again.</p>",
                    "text/html",
                    "UTF-8",
                )
            } else {
                val url = "${UploadConfig.controlUrl.trimEnd('/')}/v1/device/panel" +
                    "?device_id=${java.net.URLEncoder.encode(DeviceIdentity.id(this), "UTF-8")}" +
                    "&token=${java.net.URLEncoder.encode(token, "UTF-8")}"
                webView.loadUrl(url)
            }
        }
        if (DeviceIdentity.token(this) == null) {
            Thread {
                ControlClient.sync(this)
                runOnUiThread(openPanel)
            }.start()
        } else {
            openPanel()
        }
    }
}
