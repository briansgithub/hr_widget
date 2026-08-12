import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Keep the bot credential out of source control. Local Android builds can put
// DISCORD_TOKEN in hr_bot_android/local.properties; CI can provide the same
// value as an environment variable.
val localProperties = Properties().apply {
    val localPropertiesFile = rootProject.file("local.properties")
    if (localPropertiesFile.exists()) {
        localPropertiesFile.inputStream().use(::load)
    }
}
val discordToken =
    localProperties.getProperty("DISCORD_TOKEN") ?: System.getenv("DISCORD_TOKEN") ?: ""
val escapedDiscordToken = discordToken.replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "com.hrbot.android"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.hrbot.android"
        minSdk = 31
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        buildConfigField("String", "DISCORD_TOKEN", "\"$escapedDiscordToken\"")
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }

    kotlinOptions {
        jvmTarget = "1.8"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    // OkHttp for Discord Gateway WebSocket
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Embedded WebSocket server for the phone<->laptop sync link (OkHttp is client-only)
    implementation("org.java-websocket:Java-WebSocket:1.5.7")
}
