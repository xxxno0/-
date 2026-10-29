plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.growai"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.growai"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "0.3"
    }

    // 고정 서명키: 새 버전을 설치해도 "덮어쓰기 설치"가 되어 기억(DB)과 설정이 유지됨
    signingConfigs {
        create("growai") {
            storeFile = file("growai.keystore")
            storePassword = "growai123"
            keyAlias = "growai"
            keyPassword = "growai123"
        }
    }
    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("growai") }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("growai")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
