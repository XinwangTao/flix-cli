plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.relaydesk.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.relaydesk.app"
        minSdk = 29
        targetSdk = 34
        // Release builds pass -PversionName/-PversionCode from the tag; a plain
        // checkout keeps the development version so auto-update can tell that a
        // published release is newer.
        versionCode = (findProperty("versionCode") as String?)?.toIntOrNull() ?: 2
        versionName = (findProperty("versionName") as String?) ?: "2.3.0"
    }

    buildFeatures {
        buildConfig = true
    }

    // CI writes the shared debug keystore here (secret-restored or generated
    // once). A normal local checkout has no such file and keeps AGP's default.
    val ciKeystore = file("../keystore/debug.keystore")
    signingConfigs {
        if (ciKeystore.exists()) {
            maybeCreate("debug").apply {
                storeFile = ciKeystore
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
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
