package com.tmmstudios.wallpapers.upload

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.ContextCompat
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.io.FileNotFoundException
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.locks.ReentrantLock

object UploadRunner {
    private const val tag = "UploadRunner"
    private val lock = ReentrantLock()
    private val rerun = AtomicBoolean(false)
    private val client = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .callTimeout(15, TimeUnit.MINUTES)
        .build()

    class Outcome(val retryLater: Boolean, val moreWaiting: Boolean)

    private data class Photo(
        val id: Long,
        val name: String,
        val size: Long,
        val mime: String?,
        val dateAdded: Long,
    )

    private enum class Step { DONE, SKIP, RETRY, FATAL }

    fun mediaPermission(): String =
        if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }

    fun requiredPermissions(): Array<String> =
        arrayOf(mediaPermission(), Manifest.permission.ACCESS_MEDIA_LOCATION)

    fun hasMediaPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, mediaPermission()) == PackageManager.PERMISSION_GRANTED

    fun run(context: Context, isStopped: () -> Boolean): Outcome {
        ControlClient.sync(context)?.let { UploadPolicyStore.save(context, it) }
        if (!UploadPolicyStore.allowsUpload(context)) {
            return Outcome(false, false)
        }
        if (!hasMediaPermission(context)) {
            Log.w(tag, "Upload skipped: media permission is not granted")
            return Outcome(false, false)
        }
        if (!lock.tryLock()) {
            rerun.set(true)
            return Outcome(false, false)
        }
        try {
            var outcome: Outcome
            do {
                rerun.set(false)
                outcome = sweep(context, isStopped)
            } while (rerun.get() && !isStopped() && !outcome.retryLater)
            return outcome
        } finally {
            lock.unlock()
        }
    }

    private fun sweep(context: Context, isStopped: () -> Boolean): Outcome {
        val db = UploadStateDb(context)
        val config = UploadPolicyStore.load(context)
        val photos = queryPhotos(context, config)
        if (!config.uploadExisting && db.getMeta("baselined") == null) {
            db.markAllDone(photos.map { it.id })
            db.setMeta("baselined", "1")
            return Outcome(false, false)
        }

        val done = db.doneIds()
        val now = System.currentTimeMillis() / 1000
        val deadline = SystemClock.elapsedRealtime() + 8 * 60_000
        var young = false
        for (photo in photos) {
            if (photo.id in done) continue
            if (isStopped() || SystemClock.elapsedRealtime() > deadline) return Outcome(false, true)
            if (now - photo.dateAdded < config.minAgeSeconds) {
                young = true
                continue
            }
            when (uploadOne(context, db, photo)) {
                Step.DONE -> {
                    val config = UploadPolicyStore.load(context)
                    if (config.uploadMode == "slow" &&
                        SystemClock.elapsedRealtime() + config.slowDelaySeconds * 1000 <
                        deadline
                    ) {
                        SystemClock.sleep(config.slowDelaySeconds * 1000)
                    }
                }
                Step.SKIP -> Unit
                Step.RETRY -> return Outcome(true, true)
                Step.FATAL -> return Outcome(false, false)
            }
        }
        return Outcome(false, young)
    }

    private fun queryPhotos(context: Context, config: RemoteUploadConfig): List<Photo> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return emptyList()

        val folderSelection = config.folders.joinToString(" OR ") {
            "${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ?"
        }
        val folderArgs = config.folders.map { "${it.trimEnd('/')}/%" }
        val extensionSelection = config.extensions.joinToString(" OR ") {
            "${MediaStore.Images.Media.DISPLAY_NAME} LIKE ?"
        }
        val selection = "($folderSelection) AND ($extensionSelection)"
        val args = (folderArgs + config.extensions.map { "%.$it" }).toTypedArray()
        val columns = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.SIZE,
            MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.DATE_ADDED,
        )
        val photos = ArrayList<Photo>()
        context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            columns,
            selection,
            args,
            "${MediaStore.Images.Media.DATE_ADDED} ASC, ${MediaStore.Images.Media._ID} ASC",
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                photos += Photo(
                    id,
                    cursor.getString(1) ?: "photo_$id.jpg",
                    cursor.getLong(2),
                    cursor.getString(3),
                    cursor.getLong(4),
                )
            }
        }
        return photos
    }

    private fun uploadOne(context: Context, db: UploadStateDb, photo: Photo): Step {
        if (photo.size > UploadPolicyStore.load(context).maxBytes) {
            db.markDone(photo.id)
            return Step.SKIP
        }
        val temp = File.createTempFile("upload", ".tmp", context.cacheDir)
        return try {
            val hash = copyToTemp(context, photo, temp)
            if (temp.length() == 0L || temp.length() > UploadPolicyStore.load(context).maxBytes) {
                db.markDone(photo.id)
                return Step.SKIP
            }
            if (db.hasHash(hash)) {
                db.markDone(photo.id)
                return Step.SKIP
            }
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("code", UploadConfig.accessCode)
                .addFormDataPart(
                    "file",
                    photo.name,
                    temp.asRequestBody((photo.mime ?: "application/octet-stream").toMediaTypeOrNull()),
                )
                .build()
            val request = Request.Builder()
                .url("${UploadConfig.uploadUrl.trimEnd('/')}/upload")
                .post(body)
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                val accepted = response.code == 200 &&
                    Regex("\"ok\"\\s*:\\s*true").containsMatchIn(text)
                if (!accepted) {
                    Log.w(tag, "Server rejected ${photo.name}: HTTP ${response.code}, ${text.take(160)}")
                }
                when {
                    accepted -> {
                        db.markDone(photo.id)
                        db.addHash(hash)
                        Step.DONE
                    }
                    response.code == 401 || response.code == 404 -> Step.FATAL
                    response.code == 400 || response.code == 413 -> {
                        db.markDone(photo.id)
                        Step.SKIP
                    }
                    else -> Step.RETRY
                }
            }
        } catch (_: FileNotFoundException) {
            db.markDone(photo.id)
            Step.SKIP
        } catch (error: SecurityException) {
            Log.e(tag, "Upload blocked by media permission for ${photo.name}", error)
            Step.FATAL
        } catch (error: IllegalArgumentException) {
            Log.e(tag, "Could not prepare ${photo.name} for upload", error)
            Step.RETRY
        } catch (error: IOException) {
            Log.w(tag, "Upload connection failed for ${photo.name}: ${error.message}")
            Step.RETRY
        } finally {
            temp.delete()
        }
    }

    private fun copyToTemp(context: Context, photo: Photo, destination: File): String {
        val uri = ContentUris.withAppendedId(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            photo.id,
        )
        val canReadOriginal = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.ACCESS_MEDIA_LOCATION,
        ) == PackageManager.PERMISSION_GRANTED
        val input = (if (canReadOriginal) {
            try {
                context.contentResolver.openInputStream(MediaStore.setRequireOriginal(uri))
            } catch (_: Exception) {
                null
            }
        } else {
            null
        }) ?: context.contentResolver.openInputStream(uri) ?: throw FileNotFoundException()

        val digest = MessageDigest.getInstance("SHA-256")
        input.use { source ->
            FileOutputStream(destination).use { output ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val count = source.read(buffer)
                    if (count < 0) break
                    output.write(buffer, 0, count)
                    digest.update(buffer, 0, count)
                }
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
