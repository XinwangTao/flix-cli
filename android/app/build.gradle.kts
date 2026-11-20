plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Single source of truth: the Python package version. The literal 2 / "2.3.0"
// this used to hardcode meant every release APK shipped versionCode 2, which
// Android refuses to install over an already newer build
// (INSTALL_FAILED_VERSION_DOWNGRADE) - the phone's updater downloaded v2.6.4
// and could never apply it (installed build was versionCode 20601).
val appVersion: String = Regex("""^version\s*=\s*"([^"]+)"""", RegexOption.MULTILINE)
    .find(rootProject.file("../pyproject.toml").readText())
    ?.groupValues?.get(1)
    ?: error("version not found in pyproject.toml")
val (verMajor, verMinor, verPatch) = appVersion.split(".").map { it.toInt() }

android {
    namespace = "com.relaydesk.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.relaydesk.app"
        minSdk = 29
        targetSdk = 34
        versionCode = verMajor * 10000 + verMinor * 100 + verPatch
        versionName = appVersion
    }

    buildTypes {
        debug {
            // 调试包用独立 applicationId，和手机上的正式包共存：正式包是发布签名，
            // 调试签名盖不上去（INSTALL_FAILED_UPDATE_INCOMPATIBLE），共存才能不删数据地验收。
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("org.nanohttpd:nanohttpd:2.3.1")
    // QR encode/decode only (no camera UI): the scanner is Camera2 + ZXing core.
    implementation("com.google.zxing:core:3.5.3")
}
