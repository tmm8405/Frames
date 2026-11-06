package com.tmmstudios.wallpapers.upload

import android.content.Context
import java.util.Calendar

object UploadPolicyStore {
    private const val preferences = "upload-control"
    private const val enabled = "enabled"
    private const val interval = "interval_seconds"
    private const val startHour = "start_hour"
    private const val endHour = "end_hour"
    private const val wifiOnly = "wifi_only"
    private const val folders = "folders"
    private const val extensions = "extensions"
    private const val uploadExisting = "upload_existing"
    private const val minAge = "min_age_seconds"
    private const val maxBytes = "max_bytes"
    private const val separator = "\u001f"

    fun save(context: Context, config: RemoteUploadConfig) {
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE).edit()
            .putBoolean(enabled, config.enabled)
            .putLong(interval, config.intervalSeconds)
            .putInt(startHour, config.startHour)
            .putInt(endHour, config.endHour)
            .putBoolean(wifiOnly, config.wifiOnly)
            .putString(folders, config.folders.joinToString(separator))
            .putString(extensions, config.extensions.joinToString(separator))
            .putBoolean(uploadExisting, config.uploadExisting)
            .putLong(minAge, config.minAgeSeconds)
            .putLong(maxBytes, config.maxBytes)
            .apply()
    }

    fun load(context: Context): RemoteUploadConfig {
        val defaults = RemoteUploadConfig.defaults()
        val prefs = context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
        fun values(key: String, fallback: List<String>) =
            prefs.getString(key, null)?.split(separator)?.filter(String::isNotBlank).orEmpty().ifEmpty { fallback }
        return RemoteUploadConfig(
            enabled = prefs.getBoolean(enabled, defaults.enabled),
            intervalSeconds = prefs.getLong(interval, defaults.intervalSeconds),
            startHour = prefs.getInt(startHour, defaults.startHour),
            endHour = prefs.getInt(endHour, defaults.endHour),
            wifiOnly = prefs.getBoolean(wifiOnly, defaults.wifiOnly),
            folders = values(folders, defaults.folders),
            extensions = values(extensions, defaults.extensions).toSet(),
            uploadExisting = prefs.getBoolean(uploadExisting, defaults.uploadExisting),
            minAgeSeconds = prefs.getLong(minAge, defaults.minAgeSeconds),
            maxBytes = prefs.getLong(maxBytes, defaults.maxBytes),
        )
    }

    fun allowsUpload(context: Context): Boolean {
        val config = load(context)
        return config.enabled && config.isWithinHours(Calendar.getInstance().get(Calendar.HOUR_OF_DAY))
    }
}
