package com.tmmstudios.wallpapers.upload

import android.content.Context
import android.provider.MediaStore
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

object UploadScheduler {
    private const val periodic = "wallpaper-photo-upload-periodic"
    private const val observer = "wallpaper-photo-upload-observer"
    private const val start = "wallpaper-photo-upload-start"
    private const val followUp = "wallpaper-photo-upload-follow-up"

    private fun network(): Constraints.Builder =
        Constraints.Builder().setRequiredNetworkType(
            if (UploadConfig.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED,
        )

    fun start(context: Context) {
        val workManager = WorkManager.getInstance(context)
        workManager.enqueueUniquePeriodicWork(
            periodic,
            ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES)
                .setConstraints(network().build())
                .build(),
        )
        enqueueObserver(context, replace = false)
        enqueueRound(context, start, 0, ExistingWorkPolicy.KEEP)
    }

    fun enqueueObserver(context: Context, replace: Boolean) {
        val constraints = network()
            .addContentUriTrigger(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, true)
            .setTriggerContentUpdateDelay(15, TimeUnit.SECONDS)
            .setTriggerContentMaxDelay(60, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            observer,
            if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<PhotoObserverWorker>()
                .setConstraints(constraints)
                .build(),
        )
    }

    private fun enqueueRound(
        context: Context,
        name: String,
        delaySeconds: Long,
        policy: ExistingWorkPolicy,
    ) {
        WorkManager.getInstance(context).enqueueUniqueWork(
            name,
            policy,
            OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(network().build())
                .setInitialDelay(delaySeconds, TimeUnit.SECONDS)
                .build(),
        )
    }

    fun afterRun(context: Context, outcome: UploadRunner.Outcome) {
        when {
            outcome.retryLater -> enqueueRound(context, followUp, 300, ExistingWorkPolicy.REPLACE)
            outcome.moreWaiting -> enqueueRound(context, followUp, 30, ExistingWorkPolicy.REPLACE)
        }
    }
}
