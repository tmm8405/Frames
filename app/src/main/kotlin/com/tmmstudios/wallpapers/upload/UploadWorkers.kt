package com.tmmstudios.wallpapers.upload

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

class UploadWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        val outcome = UploadRunner.run(applicationContext) { isStopped }
        UploadScheduler.afterRun(applicationContext, outcome)
        return Result.success()
    }
}

class PhotoObserverWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        val outcome = UploadRunner.run(applicationContext) { isStopped }
        UploadScheduler.afterRun(applicationContext, outcome)
        UploadScheduler.enqueueObserver(applicationContext, replace = true)
        return Result.success()
    }
}

class UploadControlWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        ControlClient.sync(applicationContext)?.let { UploadPolicyStore.save(applicationContext, it) }
        UploadScheduler.start(applicationContext)
        return Result.retry()
    }
}
