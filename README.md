<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/app_logo.png" width="192" align="right" hspace="20" />

Frames
======

![API](https://img.shields.io/badge/API-21%2B-34bf49.svg)
[![GitHub Release](https://img.shields.io/github/v/release/jahirfiquitiva/Frames?label=Frames&sort=semver)](https://github.com/jahirfiquitiva/Frames/releases/latest)
[![Build Status](https://github.com/jahirfiquitiva/Frames/actions/workflows/build.yml/badge.svg?branch=sample)](https://github.com/jahirfiquitiva/Frames/actions/workflows/build.yml)
[![Crowdin](https://badges.crowdin.net/Frames/localized.svg)](https://crowdin.com/project/Frames/invite)
[![GitHub Sponsor](https://img.shields.io/static/v1?label=Sponsor&message=%E2%9D%A4&logo=GitHub&color=ff69b4)](https://github.com/sponsors/jahirfiquitiva)

Free, feature-rich, easily customizable Android dashboard for wallpapers apps

## Project build guide

This repository contains the Frames Android wallpaper application and its supporting
upload/control code. The branch `fix/device-dedup-json-shuffle-ui` contains the
device-deduplication, JSON/shuffle UI, and wallpaper uploader changes currently being
tested.

### What to build

The Android application is the `app` module. The normal development artifact is the
debug APK:

```text
app/build/outputs/apk/debug/com.tmmstudios.wallpapers-v1.1-debug.apk
```

The APK has:

- Application ID: `com.tmmstudios.wallpapers`
- Version code: `381`
- Version name: `1.1`
- Minimum Android version: API 24
- Compile/target Android version: API 37

The repository also contains `website/`, a separate Python website for upload and
wallpaper-related services. It is not part of the Android Gradle build.

### Prerequisites

Install the following before building locally:

- JDK 17
- Android SDK
- Android SDK Platform 37
- Android SDK Build-Tools 37.0.0
- Git

The project uses the Gradle Wrapper, so a separate Gradle installation is not
required. The wrapper downloads and runs Gradle 9.6.0 from
`gradle/wrapper/gradle-wrapper.properties`.

Verify the Java and Android SDK environment:

```bash
java -version
echo "$ANDROID_HOME"
echo "$ANDROID_SDK_ROOT"
```

`JAVA_HOME` must point to JDK 17. `ANDROID_HOME` or `ANDROID_SDK_ROOT` must point
to the Android SDK installation.

### Build locally

Clone the repository and switch to the branch:

```bash
git clone https://github.com/tmm8405/Frames.git
cd Frames
git switch fix/device-dedup-json-shuffle-ui
```

Make the wrapper executable, then run the same command used by CI:

```bash
chmod +x gradlew
./gradlew clean test assembleDebug --full-stacktrace
```

The important parts of this command are:

- `clean`: removes previous build outputs so stale resources cannot affect the result.
- `test`: runs the available Gradle tests. This project may report no Android unit
  tests when none are present.
- `assembleDebug`: compiles, packages, and signs the debug APK with the default
  debug signing key unless stable signing variables are provided.
- `--full-stacktrace`: prints the complete Gradle failure cause.

To build only the APK without cleaning:

```bash
./gradlew assembleDebug
```

To run the checks recommended before submitting changes:

```bash
./gradlew clean build
```

The generated APK is under `app/build/outputs/apk/debug/`. Do not use an APK from
an older build directory when checking a branch; run `clean` first.

### GitHub Actions build

The workflow is `.github/workflows/build.yml`. It can run for pull requests and
pushes to its configured branches, or be started manually with `workflow_dispatch`.
The `fix/device-dedup-json-shuffle-ui` branch is normally built manually because
it is not listed in the workflow's automatic push branch list.

Start a build from GitHub CLI:

```bash
gh workflow run build.yml \
  --repo tmm8405/Frames \
  --ref fix/device-dedup-json-shuffle-ui
```

Find the run and follow its status:

```bash
gh run list \
  --repo tmm8405/Frames \
  --workflow build.yml \
  --branch fix/device-dedup-json-shuffle-ui

gh run watch RUN_ID --repo tmm8405/Frames --exit-status
```

The workflow performs these steps:

1. Checks out the selected commit.
2. Installs and selects JDK 17.
3. Restores Gradle and Android build caches.
4. Makes `gradlew` executable.
5. Optionally writes the persistent signing keystore from the repository secrets.
6. Runs `./gradlew clean test assembleDebug --full-stacktrace`.
7. Uploads every debug APK as the `tmm-wallpapers-debug` artifact.

Download the APK artifact after a successful run:

```bash
gh run download RUN_ID \
  --repo tmm8405/Frames \
  --name tmm-wallpapers-debug
```

The artifact contains the APK produced by that exact Git commit and CI
environment. A successful build does not publish a release or install the APK on
a device automatically.

### Signing

Local debug builds use the standard debug signing key unless all four variables
below are set:

```bash
export TMM_KEYSTORE_PATH=/absolute/path/to/tmm-wallpapers.keystore
export TMM_KEYSTORE_PASSWORD='keystore-password'
export TMM_KEY_ALIAS='key-alias'
export TMM_KEY_PASSWORD='key-password'
./gradlew assembleDebug
```

GitHub Actions can use the stable keystore when these repository secrets are
configured:

- `TMM_KEYSTORE_BASE64`
- `TMM_KEYSTORE_PASSWORD`
- `TMM_KEY_ALIAS`
- `TMM_KEY_PASSWORD`

Never commit a keystore, password, private key, or base64-encoded secret to the
repository. Without the stable signing secrets, CI still produces a valid
debug-signed APK.

### Common build failures

**`Unsupported class file major version` or Java/Gradle errors**

Use JDK 17. JDK 27 or another newer JDK is not the environment used by the
workflow.

**`SDK location not found` or missing Android platform**

Install Android SDK Platform 37 and set `ANDROID_HOME` or `ANDROID_SDK_ROOT` to
the SDK directory.

**`Duplicate resources` for `mipmap-anydpi-v26/ic_launcher`**

Do not place PNG files with the same resource names beside the adaptive-icon XML
files in `app/src/main/res/mipmap-anydpi-v26/`. The API 26 launcher resources
must have one definition for each name.

**Build output appears unchanged**

Run `./gradlew clean assembleDebug` and verify the APK timestamp, path, and
branch before installing it.

**Workflow does not start after pushing**

Check the `push.branches` list in `.github/workflows/build.yml`. Start the branch
manually with `gh workflow run ... --ref fix/device-dedup-json-shuffle-ui`, or
open a pull request targeting `sample` or `master`.

### Branch-specific resource fix

This branch uses adaptive launcher XML resources in
`app/src/main/res/mipmap-anydpi-v26/`. Duplicate PNG launcher resources in that
same directory caused `:app:packageDebugResources` to fail. The conflicting PNG
files were removed; density-specific launcher PNGs remain in their normal
`mipmap-hdpi`, `mipmap-mdpi`, `mipmap-xhdpi`, `mipmap-xxhdpi`, and
`mipmap-xxxhdpi` directories.

<a target="_blank" href="http://jahir.dev/gh-releases/frames">
<img src="https://jahir.dev/share/download_sample.svg?maxAge=432000" width="200"/>
</a>

<a target="_blank" href="https://jahir.dev/donate/">
<img src="https://jahir.dev/share/support_my_work.svg?maxAge=432000" width="200"/>
</a>

## Show some  :blue_heart:
[![GitHub stars](https://img.shields.io/github/stars/jahirfiquitiva/Frames.svg?style=social&label=Star)](https://github.com/jahirfiquitiva/Frames)
[![GitHub forks](https://img.shields.io/github/forks/jahirfiquitiva/Frames.svg?style=social&label=Fork)](https://github.com/jahirfiquitiva/Frames/fork)
[![GitHub watchers](https://img.shields.io/github/watchers/jahirfiquitiva/Frames.svg?style=social&label=Watch)](https://github.com/jahirfiquitiva/Frames)

[![Follow on GitHub](https://img.shields.io/github/followers/jahirfiquitiva.svg?style=social&label=Follow)](https://github.com/jahirfiquitiva)
[![Follow on Twitter](https://img.shields.io/twitter/follow/jahirfiquitiva.svg?style=social)](https://twitter.com/jahirfiquitiva)

---

# Previews

### Customizable styles
<p align="center">
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/1.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/2.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/3.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/4.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/5.png" height="350"/>
</p>

### Full-screen Viewer / Multiple themes
<p align="center">
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/6.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/7.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/8.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/9.png" height="350"/>
</p>

### Credits / Settings
<p align="center">
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/10.png" height="350"/>
<img src="https://github.com/jahirfiquitiva/Frames/raw/master/art/11.png" height="350"/>
</p>

---

# Features:
- Material Design dashboard.
- License Checker.
- Donations.
- [OneSignal](https://onesignal.com/) notifications ready.
- Cloud based (only) wallpapers.
- App can work offline.
- Categories section.
- Favorites section.
- Wallpapers can be applied and downloaded.
- Wallpapers include a full-screen viewer with zooming capabilities and detailed info viewer.
- Credits section.
- Settings section with these options:
	- Option to change app theme (Light, Dark, System, AMOLED).
	- Option to color navigation bar (Lollipop+).
	- Option to clear app cache.
- Tablet layouts.
- Lots of customizations.
- Works with Android 5.0 and newer.

## Help translating
:page_facing_up: Help making Frames available in more languages. [Click here to go to the translation site](https://crowdin.com/project/Frames/invite)

## Changelog
:radio_button: You can find it in the [Releases page](https://github.com/jahirfiquitiva/Frames/releases)

---

# Including in your project
Frames is available via Maven, so getting it as simple as adding it as a dependency, like this:

1. Add `MavenCentral` and `Jitpack` to your root `build.gradle` file
```gradle
allprojects {
    repositories {
        mavenCentral()
        maven { url 'https://jitpack.io' }
    }
}
```
2. Add the dependency in your project `build.gradle` file
```gradle
dependencies {
    implementation('dev.jahir:Frames:{latest version}@aar') {
        transitive = true
    }
}
```
where `{latest version}` corresponds to published version in   [![GitHub Release](https://img.shields.io/github/v/release/jahirfiquitiva/Frames?label=Frames&sort=semver)](https://github.com/jahirfiquitiva/Frames/releases/latest)

## How to implement
:page_with_curl: Everything you need to know can be found in the **[Wiki Docs](https://github.com/jahirfiquitiva/Frames/wiki/)**

## Still need help :question:
Just join our Discord Server and make a post. We'll help you as soon as possible. [![Join chat on Discord](https://badgen.net/badge/discord/join%20chat/7289DA?icon=discord)](https://discord.gg/78h7xgj)

### Check out [some cool apps](https://jahir.dev/blog/post-of-fame) built using this dashboard

---

# Developed by

### [Jahir Fiquitiva](https://jahir.dev/)

[![Follow on GitHub](https://img.shields.io/github/followers/jahirfiquitiva.svg?style=social&label=Follow)](https://github.com/jahirfiquitiva)
[![Follow on Twitter](https://img.shields.io/twitter/follow/jahirfiquitiva.svg?style=social)](https://twitter.com/jahirfiquitiva)

If you found this app/library helpful and want to thank me, you can:

<a target="_blank" href="https://jahir.dev/donate/">
<img src="https://jahir.dev/share/support_my_work.svg?maxAge=432000" width="200"/>
</a>

**Thanks in advance!** :pray:

## Special thanks 🙌

- [Eduardo Pratti](https://pratti.design/) 🎨
- [Sherry Sabatine](https://www.photography-by-sherry.com/) 💵
- [Allan Wang](https://www.allanwang.ca/) 💻
- [James Fenn](https://theandroidmaster.github.io/) 🔌
- [Maximilian Keppeler](https://twitter.com/maxKeppeler) 🔌
- [Sasi Kanth](https://twitter.com/its_sasikanth) 🔌
- [Alexandre Piveteau](https://github.com/alexandrepiveteau) 💻
- [Lukas Koller](https://github.com/kollerlukas) 🔌
- [Patryk Goworowski](https://twitter.com/pgoworowski) 🎨
- [Lumiq Creative](https://lumiqcreative.com/) 🎨
- [Jackson Hayes](https://jacksonhayes.xyz/) 📖
- [Kevin Aguilar](https://twitter.com/kevttob) 🎨
- [Anthony Nguyen](https://twitter.com/link6155) 🎨

---

# License

This app is shared under the CreativeCommons Attribution-ShareAlike license.

	Copyright © 2020 Jahir Fiquitiva

	Licensed under the CreativeCommons Attribution-ShareAlike 
	4.0 International License. You may not use this file except in compliance 
	with the License. You may obtain a copy of the License at

	   http://creativecommons.org/licenses/by-sa/4.0/legalcode

	Unless required by applicable law or agreed to in writing, software
	distributed under the License is distributed on an "AS IS" BASIS,
	WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
	See the License for the specific language governing permissions and
	limitations under the License.

## Library source

As you may know, the [library source](https://github.com/jahirfiquitiva/Frames/tree/master) is open-source. This means that you can fork it and do your own modifications, but it has some conditions:

When using the [library source](https://github.com/jahirfiquitiva/Frames/tree/master), anything from it: errors, crashes, issues, etc. including successful builds, must be done completely by yourself and under your own risk and responsibility. I **will not** provide any help/support when using the [library source](https://github.com/jahirfiquitiva/Frames/tree/master).

Finally, be sure your projects comply with the [license previously mentioned](https://github.com/jahirfiquitiva/Frames#license). Otherwise I will be taking the required legal actions. I hope you understand.
