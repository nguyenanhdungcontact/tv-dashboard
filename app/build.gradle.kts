plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The GitHub Actions workflow passes -PdashboardUrl=https://<you>.github.io/<repo>/
// Left empty, the app shows the copy of the dashboard bundled inside the APK.
val dashboardUrl = (project.findProperty("dashboardUrl") as String?) ?: ""

android {
    namespace = "dev.tvdash.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.tvdash.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        buildConfigField("String", "DASHBOARD_URL", "\"$dashboardUrl\"")
    }

    // Fixed key so every build from GitHub Actions can be installed over the previous one.
    signingConfigs {
        create("tvdash") {
            storeFile = file("tvdash.keystore")
            storePassword = "tvdash123"
            keyAlias = "tvdash"
            keyPassword = "tvdash123"
        }
    }
    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("tvdash") }
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
