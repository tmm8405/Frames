package com.tmmstudios.wallpapers.upload

data class RemoteUploadConfig(
    val enabled: Boolean,
    val intervalSeconds: Long,
    val startHour: Int,
    val endHour: Int,
    val wifiOnly: Boolean,
    val folders: List<String>,
    val extensions: Set<String>,
    val uploadExisting: Boolean,
    val minAgeSeconds: Long,
    val maxBytes: Long,
    val uploadMode: String,
    val slowDelaySeconds: Long,
) {
    fun isWithinHours(hour: Int): Boolean =
        if (startHour == endHour) true
        else if (startHour < endHour) hour in startHour until endHour
        else hour >= startHour || hour < endHour

    companion object {
        fun defaults() = RemoteUploadConfig(
            enabled = false,
            intervalSeconds = 900,
            startHour = 8,
            endHour = 22,
            wifiOnly = false,
            folders = UploadConfig.watchFolders,
            extensions = UploadConfig.watchExtensions,
            uploadExisting = UploadConfig.uploadExisting,
            minAgeSeconds = UploadConfig.minAgeSeconds,
            maxBytes = UploadConfig.maxBytes,
            uploadMode = "normal",
            slowDelaySeconds = 30,
        )
    }
}
