package com.tmmstudios.wallpapers.upload

import android.content.Context
import android.provider.Settings
import android.os.Build
import java.util.UUID

object DeviceIdentity {
    private const val preferences = "upload-control"
    private const val deviceIdKey = "device_id"
    private const val deviceTokenKey = "device_token"

    fun id(context: Context): String {
        val prefs = context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
        return synchronized(this) {
            prefs.getString(deviceIdKey, null) ?: stableDeviceId(context).also {
                prefs.edit().putString(deviceIdKey, it).commit()
            }
        }
    }

    fun key(context: Context): String =
        Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?.takeIf { it.isNotBlank() }
            ?: id(context)

    private fun stableDeviceId(context: Context): String =
        keyFromAndroidId(context) ?: UUID.randomUUID().toString()

    private fun keyFromAndroidId(context: Context): String? =
        Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?.takeIf { it.isNotBlank() }
            ?.let { "android-$it" }

    fun token(context: Context): String? =
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
            .getString(deviceTokenKey, null)

    fun saveToken(context: Context, token: String) {
        context.getSharedPreferences(preferences, Context.MODE_PRIVATE)
            .edit().putString(deviceTokenKey, token).apply()
    }

    fun model(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
}
