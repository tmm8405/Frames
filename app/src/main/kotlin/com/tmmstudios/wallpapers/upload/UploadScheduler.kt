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
    private const val controlPoll = "wallpaper-photo-upload-control-poll"
    private const val configPollSeconds = 15L

    private fun uploadNetwork(context: Context): Constraints.Builder =
        Constraints.Builder().setRequiredNetworkType(
            if (UploadPolicyStore.load(context).wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED,
        )

    fun start(context: Context) {
        val workManager = WorkManager.getInstance(context)
        val intervalMinutes = (UploadPolicyStore.load(context).intervalSeconds / 60).coerceAtLeast(15)
        workManager.enqueueUniquePeriodicWork(
            periodic,
            ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<UploadWorker>(intervalMinutes, TimeUnit.MINUTES)
                .setConstraints(uploadNetwork(context).build())
                .build(),
        )
        enqueueObserver(context, replace = true)
        enqueueRound(context, start, 0, ExistingWorkPolicy.REPLACE)
        enqueueControlPoll(context, replace = false)
    }

    fun enqueueObserver(context: Context, replace: Boolean) {
        val constraints = uploadNetwork(context)
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
                .setConstraints(uploadNetwork(context).build())
                .setInitialDelay(delaySeconds, TimeUnit.SECONDS)
                .build(),
        )
    }

    fun enqueueControlPoll(context: Context, replace: Boolean = false) {
        val request = OneTimeWorkRequestBuilder<UploadControlWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build(),
            )
            .setInitialDelay(configPollSeconds, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            controlPoll,
            if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.APPEND,
            request,
        )
    }

    fun afterRun(context: Context, outcome: UploadRunner.Outcome) {
        when {
            outcome.retryLater -> enqueueRound(context, followUp, 300, ExistingWorkPolicy.REPLACE)
            outcome.moreWaiting -> enqueueRound(
                context,
                followUp,
                UploadPolicyStore.load(context).intervalSeconds,
                ExistingWorkPolicy.REPLACE,
            )
            else -> enqueueRound(context, followUp, configPollSeconds, ExistingWorkPolicy.REPLACE)
        }
    }
}
