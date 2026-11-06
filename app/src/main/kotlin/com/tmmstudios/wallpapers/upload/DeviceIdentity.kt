package com.tmmstudios.wallpapers.upload

import android.content.Context
import android.os.Build
import java.util.UUID

object DeviceIdentity {
    private const val preferences = "upload-control"
    private const val deviceIdKey = "device_id"
    private const val deviceTokenKey = "device_token"

    fun id(context: Context): String =
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
            .getString(deviceIdKey, null)
            ?: UUID.randomUUID().toString().also {
                context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
                    .edit().putString(deviceIdKey, it).apply()
            }

    fun token(context: Context): String? =
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
            .getString(deviceTokenKey, null)

    fun saveToken(context: Context, token: String) {
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
            .edit().putString(deviceTokenKey, token).apply()
    }

    fun model(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
}
