package com.hrbot.android

import android.annotation.SuppressLint
import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.SoundPool
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import android.graphics.Color
import com.hrbot.android.MainActivity
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * Foreground service that:
 *  1. Scans for a Fitbit/Charge via BLE
 *  2. Connects and subscribes to Heart Rate Measurement notifications
 *  3. Connects to the Discord Gateway and updates the bot's presence
 *
 * Runs as a foreground service so Android doesn't kill it in the background.
 */
class HeartRateService : Service() {

    // ── Constants ────────────────────────────────────────────────────

    companion object {
        private const val TAG = "HRService"
        private const val CHANNEL_ID = "hr_bot_channel"
        private const val NOTIFICATION_ID = 1

        // Standard Bluetooth LE Heart Rate UUIDs
        private val HR_SERVICE_UUID: UUID =
            UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb")
        private val HR_MEASUREMENT_UUID: UUID =
            UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb")
        private val CLIENT_CHARACTERISTIC_CONFIG: UUID =
            UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        private val DISCORD_TOKEN: String
            get() = BuildConfig.DISCORD_TOKEN.trim()

        private const val DISCORD_GATEWAY_URL =
            "wss://gateway.discord.gg/?v=10&encoding=json"
    }

    // ── UI callback interface ────────────────────────────────────────

    interface UICallback {
        fun onBpmUpdate(bpm: Int)
        fun onStatusUpdate(status: String)
        fun onDiscordStatusUpdate(status: String)
        fun onLog(message: String)
        fun onGuildsUpdated(guilds: List<GuildInfo>)
        fun onSyncStatusUpdate(status: String) {}
        fun onSyncRoleChange(role: String, peerName: String?) {}
        fun onPhaseUpdate(phase: Float) {}
        fun onHeartbeatSoundStateChanged(enabled: Boolean, error: String?) {}
    }

    private val uiCallbacks = mutableSetOf<UICallback>()

    fun addCallback(cb: UICallback) {
        uiCallbacks.add(cb)
    }

    fun removeCallback(cb: UICallback) {
        uiCallbacks.remove(cb)
    }

    data class GuildInfo(val id: String, val name: String, var isEnabled: Boolean = true)

    // ── Binder for Activity binding ──────────────────────────────────

    inner class LocalBinder : Binder() {
        fun getService(): HeartRateService = this@HeartRateService
    }

    private val binder = LocalBinder()
    override fun onBind(intent: Intent?): IBinder = binder

    // OkHttp explicitly recommends sharing a single client: each instance owns its
    // own connection pool + dispatcher thread pool. A fresh OkHttpClient() was being
    // created on every nickname PATCH -- every 15s while Discord is active -- forcing
    // a brand new TLS handshake to discord.com each time instead of reusing one.
    private val restHttpClient = OkHttpClient()
    private val wsHttpClient = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // infinite; required to keep the gateway socket alive
        // Discord's own heartbeat is ~41s, but many networks (mobile NAT especially)
        // silently drop an idle TCP connection well before that if nothing crosses
        // it. OkHttp sends no keepalive traffic on its own -- without this, the
        // gateway connection dies invisibly and we only find out when the first
        // Discord heartbeat write fails with "Broken pipe", ~80s in. A ping well
        // under both the NAT timeout and Discord's interval keeps the path alive.
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    // ── State ────────────────────────────────────────────────────────

    private val handler = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    private var bluetoothAdapter: BluetoothAdapter? = null
    private var scanner: BluetoothLeScanner? = null
    private var gatt: BluetoothGatt? = null
    private var lastFitbitAddress: String? = null
    private var reconnectAttempt = 0
    private var scanning = false
    var currentBpm = 0
        private set
    private var lastBpmTimestamp = 0L
    private val STALE_TIMEOUT = 45_000L

    private val stalenessRunnable = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            if (
                currentBpm > 0 &&
                lastBpmTimestamp > 0L &&
                now - lastBpmTimestamp > STALE_TIMEOUT
            ) {
                log("Heart rate data stale (45s). Neutralizing.")
                updateBpm(0)
                // A live GATT that has stopped delivering measurements is the usual
                // supervision-timeout precursor. Drop it so the reconnect path runs.
                try { gatt?.disconnect() } catch (_: Exception) {}
            }
            handler.postDelayed(this, 5000)
        }
    }

    // Discord state
    var isDiscordEnabled = false
        private set
    var isNewThresholdsEnabled = true
        private set
    var isFloatingEnabled = false
        private set
    var isShowBpmEnabled = true
        private set
    var isHeartbeatSoundEnabled = false
        private set
    var heartbeatSoundVolumePercent = HeartbeatSoundVolume.DEFAULT_PERCENT
        private set
    var isHeartbeatVibrationEnabled = false
        private set
    var heartbeatVibrationIntensityPercent = 75
        private set
    var selectedHeartbeatSoundEffectId = HeartbeatSoundCatalog.DEFAULT_ID
        private set
    private var floatingManager: FloatingWidgetManager? = null
    private var syncLink: SyncLinkManager? = null
    var syncRole: String = SyncLinkManager.ROLE_UNLINKED
        private set
    private var trackingRequested = false
    private var discordSocket: WebSocket? = null
    private var heartbeatInterval = 41250L
    private var lastSequence: Int? = null
    private var heartbeatRunnable: Runnable? = null
    private var lastSentBpm = -1
    private var guilds = mutableMapOf<String, GuildInfo>()
    private val discordHandler = Handler(Looper.getMainLooper())

    private val pulseClock = MonotonicPulseClock()
    private val heartbeatBeatGate = HeartbeatBeatGate()
    private val heartbeatSchedulePolicy = HeartbeatSchedulePolicy()
    private var heartbeatSoundPool: SoundPool? = null
    private var heartbeatSoundSampleId = 0
    private var heartbeatSoundStreamId = 0
    private var heartbeatSoundLoaded = false
    private var heartbeatSoundRunnable: Runnable? = null
    private var vibrator: Vibrator? = null

    private val phaseRunnable = object : Runnable {
        override fun run() {
            // Visual callbacks can pause while hidden because both visuals and audio
            // sample the same monotonic clock; no phase is accumulated in this loop.
            // Skip the 60 FPS work
            // whenever nothing can actually show it: no activity bound and no
            // floating overlay, OR the display is off/locked (screen-off and
            // lock-screen both hide the overlay too -- see isDisplayIdle()).
            if (isDisplayIdle() || (uiCallbacks.isEmpty() && floatingManager == null)) {
                // Nothing depends on sub-second latency while idle -- worst case is
                // up to a 1s delay before the pulse resumes smoothly after unlock,
                // which is imperceptible.
                handler.postDelayed(this, 1000)
                return
            }

            if (currentBpm > 0) {
                val phase = pulseClock.phaseAt(SystemClock.elapsedRealtimeNanos()).toFloat()
                uiCallbacks.forEach { it.onPhaseUpdate(phase) }
                floatingManager?.updatePhase(phase)
            } else {
                uiCallbacks.forEach { it.onPhaseUpdate(0f) }
            }
            handler.postDelayed(this, 16) // ~60 FPS
        }
    }

    private val presenceRunnable = object : Runnable {
        override fun run() {
            sendPresenceUpdate()
            discordHandler.postDelayed(this, 15_000)
        }
    }

    // ── Screen/lock state ────────────────────────────────────────────
    // Nothing this service draws (floating overlay, MainActivity, PulseActivity)
    // is actually visible when the screen is off OR the keyguard is showing --
    // the overlay's SHOW_WHEN_LOCKED flags don't work for a Service-owned
    // TYPE_APPLICATION_OVERLAY window on modern Android (confirmed: it does not
    // render over the lock screen). So the 60 FPS pulse loop below is pure
    // waste during that whole window, not just when nothing is enabled.
    private var isScreenOn = true

    private fun isDisplayIdle(): Boolean {
        if (!isScreenOn) return true
        val km = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
        return km?.isKeyguardLocked == true
    }

    private val screenStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            isScreenOn = intent.action == Intent.ACTION_SCREEN_ON
        }
    }

    // ── Service lifecycle ────────────────────────────────────────────

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        loadGuildSettings()
        loadThresholdSettings()
        loadFloatingSettings()
        loadShowBpmSettings()
        loadHeartbeatVibrationSettings()
        
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "HRBot:TrackingWakeLock")
        isScreenOn = pm.isInteractive
        registerReceiver(screenStateReceiver, IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        })

        refreshForegroundServiceType()

        val manager = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        bluetoothAdapter = manager.adapter

        if (bluetoothAdapter == null || !bluetoothAdapter!!.isEnabled) {
            log("Bluetooth is not available or not enabled!")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
        } else {
            scanner = bluetoothAdapter!!.bluetoothLeScanner
        }
        syncLink = SyncLinkManager(
            context = this,
            deviceName = Build.MODEL ?: "Phone",
            token = loadSyncToken(),
            onRoleChange = { role, peerName ->
                // Already delivered on the main thread by SyncLinkManager.
                val wasMirror = syncRole == SyncLinkManager.ROLE_MIRROR
                syncRole = role
                if (isDiscordEnabled) {
                    if (role == SyncLinkManager.ROLE_MIRROR) disconnectDiscord()
                    else if (discordSocket == null) connectDiscordGateway()
                }
                // A stale lastSentBpm cache could make sendPresenceUpdate() wrongly
                // believe Discord already shows the right value after regaining HOST
                // (if the current bpm happens to match whatever we last wrote before
                // we started MIRRORing). Force the next write unconditionally.
                lastSentBpm = -1
                uiCallbacks.forEach { it.onSyncRoleChange(role, peerName) }

                if (role == SyncLinkManager.ROLE_MIRROR) {
                    ensureTrackingInfrastructure()
                    stopScan()
                    gatt?.let { activeGatt ->
                        try { activeGatt.disconnect() } catch (_: Exception) {}
                        try { activeGatt.close() } catch (_: Exception) {}
                        if (gatt === activeGatt) gatt = null
                        syncLink?.notifyFitbitState(false)
                        updateBpm(0)
                    }
                } else if (wasMirror && gatt == null) {
                    updateBpm(0)
                    stopStalenessMonitorIfIdle()
                    if (!trackingRequested && !isHeartbeatSoundEnabled && wakeLock?.isHeld == true) {
                        wakeLock?.release()
                    }
                }

                // Resume our own BLE scan if we stopped mirroring and are still meant to be tracking.
                if (role != SyncLinkManager.ROLE_MIRROR && trackingRequested && gatt == null && !scanning) {
                    startScan()
                }
            },
            onStatus = { text -> handler.post { uiCallbacks.forEach { it.onSyncStatusUpdate(text) } } },
            onDiscordChange = { enabled ->
                toggleDiscord(enabled, fromPeer = true)
                uiCallbacks.forEach { it.onDiscordStatusUpdate(if (enabled) "Enabled" else "Offline") }
            },
            onRemoteBpm = { bpm: Int, phase: Float? ->
                lastBpmTimestamp = System.currentTimeMillis()
                updateBpm(bpm, phase)
            }
        ).also { it.start() }

        handler.post(phaseRunnable)
    }

    fun requestSyncLink() = syncLink?.requestLink()
    val isSyncLinked: Boolean get() = syncLink?.isLinked == true
    val syncPeerName: String? get() = syncLink?.linkedPeerName
    fun connectManualSyncLink(host: String) = syncLink?.connectManual(host)
    fun disconnectSyncLink() = syncLink?.disconnect()

    fun getSyncToken(): String = loadSyncToken()

    fun setSyncToken(token: String) {
        val trimmed = token.trim()
        if (trimmed.isEmpty()) return
        getSharedPreferences("hr_bot_prefs", MODE_PRIVATE).edit()
            .putString("sync_token", trimmed).apply()
        syncLink?.updateToken(trimmed)
    }

    private fun loadSyncToken(): String {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        var token = prefs.getString("sync_token", null)
        if (token.isNullOrEmpty()) {
            // Generated locally; must be copied to the other device (or overwritten with
            // theirs) via setSyncToken() so both sides share the same pairing code.
            token = (100000..999999).random().toString(16)
            prefs.edit().putString("sync_token", token).apply()
        }
        return token
    }

    @SuppressLint("MissingPermission")
    override fun onDestroy() {
        log("Service being destroyed. Cleaning up...")
        try { unregisterReceiver(screenStateReceiver) } catch (_: Exception) {}
        stopScan()
        disconnectDiscord()
        floatingManager?.hide()
        floatingManager = null
        gatt?.let { activeGatt ->
            try { activeGatt.disconnect() } catch (_: Exception) {}
            try { activeGatt.close() } catch (_: Exception) {}
        }
        gatt = null
        syncLink?.notifyFitbitState(false)
        syncLink?.stop()
        syncLink = null
        
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
        
        handler.removeCallbacks(stalenessRunnable)
        handler.removeCallbacks(phaseRunnable)
        isHeartbeatSoundEnabled = false
        releaseHeartbeatSoundResources()
        try { vibrator?.cancel() } catch (_: Exception) {}
        vibrator = null
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }
        discordHandler.removeCallbacks(presenceRunnable)
        super.onDestroy()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        when {
            isHeartbeatSoundEnabled ->
                log("App swiped away. Heartbeat sound remains active.")
            isHeartbeatVibrationEnabled ->
                log("App swiped away. Heartbeat vibration remains active.")
            syncRole != SyncLinkManager.ROLE_UNLINKED ->
                log("App swiped away. Device link remains active.")
            else -> {
                log("App swiped away while idle. Shutting down service...")
                stopSelf()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    // ── Public API for Activity ──────────────────────────────────────

    private fun ensureWakeLockHeld() {
        if (wakeLock?.isHeld == false) {
            wakeLock?.acquire(24 * 60 * 60 * 1000L) // 24 hours max
        }
    }

    private fun ensureTrackingInfrastructure() {
        ensureWakeLockHeld()
        ensureStalenessMonitorActive()
    }

    private fun ensureStalenessMonitorActive() {
        handler.removeCallbacks(stalenessRunnable)
        handler.post(stalenessRunnable)
    }

    private fun stopStalenessMonitorIfIdle() {
        if (!trackingRequested && !isHeartbeatSoundEnabled && syncRole != SyncLinkManager.ROLE_MIRROR) {
            handler.removeCallbacks(stalenessRunnable)
        }
    }

    fun startTracking() {
        if (bluetoothAdapter == null || !bluetoothAdapter!!.isEnabled) {
            log("Cannot start: Bluetooth is not enabled")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
            return
        }

        trackingRequested = true
        ensureTrackingInfrastructure()
        startScan()
    }

    @SuppressLint("MissingPermission")
    fun stopTracking() {
        log("Stopping all processes...")
        toggleHeartbeatSound(false)
        trackingRequested = false
        stopScan()
        gatt?.let { activeGatt ->
            try { activeGatt.disconnect() } catch (_: Exception) {}
            try { activeGatt.close() } catch (_: Exception) {}
        }
        gatt = null
        syncLink?.notifyFitbitState(false)

        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
        handler.removeCallbacks(stalenessRunnable)
        handler.removeCallbacks(reconnectRunnable)
        
        currentBpm = 0
        pulseClock.synchronize(0, null, SystemClock.elapsedRealtimeNanos())
        handler.post {
            uiCallbacks.forEach { it.onBpmUpdate(0) }
            uiCallbacks.forEach { it.onStatusUpdate("Ready") }
        }

        // Disconnect Discord if it's on, then stop the service
        if (isDiscordEnabled) {
            isDiscordEnabled = false
            disconnectDiscord {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        } else {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    fun toggleDiscord(enabled: Boolean, fromPeer: Boolean = false): Boolean {
        if (isDiscordEnabled == enabled) return true
        if (enabled && syncRole != SyncLinkManager.ROLE_MIRROR && DISCORD_TOKEN.isEmpty()) {
            isDiscordEnabled = false
            log("Discord bot cannot start: DISCORD_TOKEN is not configured.")
            handler.post {
                uiCallbacks.forEach { it.onDiscordStatusUpdate("Token missing - configure DISCORD_TOKEN") }
            }
            return false
        }
        isDiscordEnabled = enabled
        if (!fromPeer) syncLink?.setDiscordEnabled(enabled)
        if (enabled) {
            if (discordSocket == null) {
                connectDiscordGateway()
            }
        } else {
            disconnectDiscord()
        }
        return true
    }

    /**
     * Enables heartbeat audio for this service session only. Loading is
     * asynchronous; a later load error is returned through [UICallback].
     */
    fun toggleHeartbeatSound(enabled: Boolean): Boolean {
        if (isHeartbeatSoundEnabled == enabled) return true

        if (!enabled) {
            isHeartbeatSoundEnabled = false
            releaseHeartbeatSoundResources()
            scheduleNextHeartbeat()
            stopStalenessMonitorIfIdle()
            if (!trackingRequested && wakeLock?.isHeld == true) wakeLock?.release()
            refreshForegroundServiceType()
            notifyHeartbeatSoundState(false, null)
            return true
        }

        isHeartbeatSoundEnabled = true
        ensureWakeLockHeld()
        ensureStalenessMonitorActive()
        if (!prepareHeartbeatSoundPool()) {
            failHeartbeatSound(
                "Heartbeat sound could not be loaded.",
                notifyUiError = false
            )
            return false
        }

        refreshForegroundServiceType()
        notifyHeartbeatSoundState(true, null)
        if (heartbeatSoundLoaded && currentBpm > 0) scheduleNextHeartbeat()
        return true
    }

    fun toggleHeartbeatVibration(enabled: Boolean) {
        if (isHeartbeatVibrationEnabled == enabled) return
        isHeartbeatVibrationEnabled = enabled
        saveHeartbeatVibrationSettings()
        if (enabled) {
            if (currentBpm > 0) scheduleNextHeartbeat()
        } else {
            try { vibrator?.cancel() } catch (_: Exception) {}
            if (!isHeartbeatSoundEnabled) cancelHeartbeatSchedule()
        }
    }

    fun setHeartbeatVibrationIntensity(percent: Int) {
        heartbeatVibrationIntensityPercent = percent.coerceIn(0, 100)
        saveHeartbeatVibrationSettings()
    }

    fun setHeartbeatSoundVolume(percent: Int) {
        heartbeatSoundVolumePercent = HeartbeatSoundVolume.clampPercent(percent)
        val pool = heartbeatSoundPool
        val streamId = heartbeatSoundStreamId
        if (pool != null && streamId != 0) {
            val volume = HeartbeatSoundVolume.scalar(heartbeatSoundVolumePercent)
            try {
                pool.setVolume(streamId, volume, volume)
            } catch (e: Exception) {
                log("Could not update heartbeat sound volume: ${e.message}")
            }
        }
    }

    fun setHeartbeatSoundEffect(effectId: String): Boolean {
        if (HeartbeatSoundCatalog.findById(effectId) == null) return false
        if (selectedHeartbeatSoundEffectId == effectId) return true

        selectedHeartbeatSoundEffectId = effectId
        releaseHeartbeatSoundResources()
        if (!isHeartbeatSoundEnabled || currentBpm <= 0) return true

        if (!prepareHeartbeatSoundPool()) {
            failHeartbeatSound(
                "Heartbeat sound could not be loaded.",
                notifyUiError = false
            )
            return false
        }
        // The load callback deliberately uses the next strictly future crest so
        // changing effects never emits an immediate correction/catch-up beat.
        return true
    }

    private fun prepareHeartbeatSoundPool(): Boolean {
        if (heartbeatSoundPool != null) return true

        val effect = HeartbeatSoundCatalog.findById(selectedHeartbeatSoundEffectId)
            ?: return false
        val rawResourceId = resources.getIdentifier(
            effect.rawResourceName,
            "raw",
            packageName
        )
        if (rawResourceId == 0) {
            log("Heartbeat sound asset is missing: ${effect.rawResourceName}")
            return false
        }

        val pool = try {
            SoundPool.Builder()
                .setMaxStreams(1)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                .build()
        } catch (e: Exception) {
            log("Could not create heartbeat SoundPool: ${e.message}")
            return false
        }

        heartbeatSoundPool = pool
        heartbeatSoundLoaded = false
        heartbeatSoundSampleId = 0
        pool.setOnLoadCompleteListener { loadedPool, sampleId, status ->
            handler.post {
                if (heartbeatSoundPool !== loadedPool) return@post
                loadedPool.setOnLoadCompleteListener(null)
                if (status != 0 || (heartbeatSoundSampleId != 0 && sampleId != heartbeatSoundSampleId)) {
                    failHeartbeatSound("Heartbeat sound failed to load (status $status).")
                    return@post
                }

                heartbeatSoundSampleId = sampleId
                heartbeatSoundLoaded = true
                if (!isHeartbeatSoundEnabled || currentBpm <= 0) {
                    // No sample needs to remain resident while the oscillator is idle.
                    releaseHeartbeatSoundResources()
                } else {
                    scheduleNextHeartbeat()
                }
            }
        }

        return try {
            heartbeatSoundSampleId = pool.load(this, rawResourceId, 1)
            if (heartbeatSoundSampleId == 0) {
                releaseHeartbeatSoundResources()
                false
            } else {
                true
            }
        } catch (e: Exception) {
            log("Could not load heartbeat sound: ${e.message}")
            releaseHeartbeatSoundResources()
            false
        }
    }

    private fun resumeHeartbeatSoundIfNeeded() {
        if (!isHeartbeatSoundEnabled || currentBpm <= 0) return
        if (!prepareHeartbeatSoundPool()) {
            failHeartbeatSound("Heartbeat sound could not be loaded.")
            return
        }
        if (heartbeatSoundLoaded) scheduleNextHeartbeat()
    }

    private fun scheduleNextHeartbeat(allowRecentCrest: Boolean = false) {
        cancelHeartbeatSchedule()
        val soundReady = isHeartbeatSoundEnabled && heartbeatSoundLoaded
        if ((!soundReady && !isHeartbeatVibrationEnabled) || currentBpm <= 0) return

        val schedulingNowNanos = SystemClock.elapsedRealtimeNanos()
        val scheduledAtNanos = heartbeatSchedulePolicy.deadline(
            pulseClock,
            schedulingNowNanos,
            allowRecentCrest
        ) ?: return
        val runnable = object : Runnable {
            override fun run() {
                if (heartbeatSoundRunnable !== this) return
                heartbeatSoundRunnable = null
                if (
                    ((!isHeartbeatSoundEnabled || !heartbeatSoundLoaded) && !isHeartbeatVibrationEnabled) ||
                    currentBpm <= 0
                ) return

                val observedAtNanos = SystemClock.elapsedRealtimeNanos()
                val periodNanos = pulseClock.periodNanos() ?: return
                if (
                    heartbeatBeatGate.shouldPlay(
                        scheduledAtNanos,
                        observedAtNanos,
                        periodNanos
                    )
                ) {
                    if (isHeartbeatSoundEnabled && heartbeatSoundLoaded) {
                        val volume = HeartbeatSoundVolume.scalar(heartbeatSoundVolumePercent)
                        val streamId = try {
                            heartbeatSoundPool?.play(
                                heartbeatSoundSampleId,
                                volume,
                                volume,
                                1,
                                0,
                                1.0f
                            ) ?: 0
                        } catch (e: Exception) {
                            log("Heartbeat playback failed: ${e.message}")
                            0
                        }
                        if (streamId == 0) {
                            failHeartbeatSound("Heartbeat sound playback failed.")
                        } else {
                            heartbeatSoundStreamId = streamId
                        }
                    }
                    if (isHeartbeatVibrationEnabled) vibrateForHeartbeat()
                }

                // A late callback is skipped, then the clock is sampled again. This
                // intentionally never emits missed beats as a catch-up burst.
                scheduleNextHeartbeat()
            }
        }
        heartbeatSoundRunnable = runnable
        val nowNanos = SystemClock.elapsedRealtimeNanos()
        val delayMillis = ((scheduledAtNanos - nowNanos).coerceAtLeast(0L) + 999_999L) / 1_000_000L
        handler.postDelayed(runnable, delayMillis.coerceAtLeast(1L))
    }

    private fun cancelHeartbeatSchedule() {
        heartbeatSoundRunnable?.let(handler::removeCallbacks)
        heartbeatSoundRunnable = null
    }

    private fun vibrateForHeartbeat() {
        if (heartbeatVibrationIntensityPercent <= 0) return
        try {
            val deviceVibrator = vibrator ?: getSystemService(VibratorManager::class.java)
                ?.defaultVibrator
                ?.also { vibrator = it }
            if (deviceVibrator?.hasVibrator() == true) {
                // One palpable pulse: a quick rounded upstroke, a longer soft
                // decay, then a faint rebound. Keep this compact and let the
                // shared heartbeat clock supply the rest between beats.
                val timings = longArrayOf(6, 8, 12, 16, 22, 24, 18, 12, 12, 16, 12)
                val envelope = intArrayOf(25, 100, 200, 255, 170, 95, 40, 0, 30, 15, 0)
                val intensity = heartbeatVibrationIntensityPercent.coerceIn(0, 100)
                val amplitudes = envelope.map { (it * intensity / 100).coerceIn(0, 255) }
                    .toIntArray()
                val effect = if (deviceVibrator.hasAmplitudeControl()) {
                    VibrationEffect.createWaveform(timings, amplitudes, -1)
                } else {
                    // Without amplitude control a shaped envelope becomes a
                    // long full-strength buzz; use a single brief tap instead.
                    VibrationEffect.createOneShot(35L, VibrationEffect.DEFAULT_AMPLITUDE)
                }
                deviceVibrator.vibrate(effect)
            }
        } catch (e: Exception) {
            log("Heartbeat vibration failed: ${e.message}")
        }
    }

    private fun releaseHeartbeatSoundResources() {
        cancelHeartbeatSchedule()
        val pool = heartbeatSoundPool
        heartbeatSoundPool = null
        heartbeatSoundLoaded = false
        heartbeatSoundSampleId = 0
        if (pool != null) {
            try { pool.setOnLoadCompleteListener(null) } catch (_: Exception) {}
            if (heartbeatSoundStreamId != 0) {
                try { pool.stop(heartbeatSoundStreamId) } catch (_: Exception) {}
            }
            try { pool.release() } catch (_: Exception) {}
        }
        heartbeatSoundStreamId = 0
    }

    private fun failHeartbeatSound(message: String, notifyUiError: Boolean = true) {
        log(message)
        isHeartbeatSoundEnabled = false
        releaseHeartbeatSoundResources()
        stopStalenessMonitorIfIdle()
        if (!trackingRequested && wakeLock?.isHeld == true) wakeLock?.release()
        refreshForegroundServiceType()
        notifyHeartbeatSoundState(false, if (notifyUiError) message else null)
    }

    private fun notifyHeartbeatSoundState(enabled: Boolean, error: String?) {
        handler.post {
            uiCallbacks.forEach { it.onHeartbeatSoundStateChanged(enabled, error) }
        }
    }

    fun toggleNewThresholds(enabled: Boolean) {
        if (isNewThresholdsEnabled == enabled) return
        isNewThresholdsEnabled = enabled
        saveThresholdSettings()
    }

    fun toggleFloating(enabled: Boolean, force: Boolean = false) {
        log("toggleFloating: enabled=$enabled, force=$force")
        if (enabled && !android.provider.Settings.canDrawOverlays(this)) {
            log("Missing overlay permission, cannot show widget")
            isFloatingEnabled = false
            return
        }
        
        // Proceed if the state is changing, or if we are forcing, or if we want it enabled but the manager doesn't exist yet
        if (!force && isFloatingEnabled == enabled && (enabled == (floatingManager != null))) return
        isFloatingEnabled = enabled
        if (enabled) {
            if (floatingManager == null) {
                floatingManager = FloatingWidgetManager(this)
            }
            floatingManager?.show()
            floatingManager?.setShowBpm(isShowBpmEnabled)
            floatingManager?.updateBpm(currentBpm)
        } else {
            floatingManager?.hide()
            floatingManager = null
        }
        saveFloatingSettings()
    }

    fun toggleShowBpm(enabled: Boolean) {
        if (isShowBpmEnabled == enabled) return
        isShowBpmEnabled = enabled
        floatingManager?.setShowBpm(enabled)
        saveShowBpmSettings()
        
        // Notify UI callbacks so activities can update
        handler.post {
            uiCallbacks.forEach { it.onBpmUpdate(currentBpm) }
        }
    }

    fun toggleGuild(guildId: String, enabled: Boolean) {
        guilds[guildId]?.let {
            it.isEnabled = enabled
            saveGuildSettings()
            // Same host/mirror rule as sendPresenceUpdate(): a MIRROR never
            // writes to the shared Discord nickname, even for a manual toggle.
            if (isDiscordEnabled && discordSocket != null && syncRole != SyncLinkManager.ROLE_MIRROR) {
                updateNicknameForGuild(it, currentBpm)
            }
        }
    }

    fun getGuilds(): List<GuildInfo> =
        guilds.values.sortedBy { it.name.lowercase() }

    /**
     * Centralized BPM color logic matching the desktop widget thresholds.
     */
    fun getBpmColor(bpm: Int): Int {
        data class Stop(val bpm: Int, val r: Int, val g: Int, val b: Int)
        
        val stops = if (isNewThresholdsEnabled) {
            listOf(
                Stop(0,   0,   0, 255),
                Stop(58,  0,   0, 255),
                Stop(70,  0, 255,   0),
                Stop(100, 255, 0,   0),
                Stop(250, 255, 0,   0)
            )
        } else {
            listOf(
                Stop(0,   0,   0, 255),
                Stop(60,  0,   0, 255),
                Stop(100, 0, 255,   0),
                Stop(155, 255, 0,   0),
                Stop(250, 255, 0,   0)
            )
        }
        
        val clamped = bpm.coerceIn(0, 250)
        for (i in 0 until stops.size - 1) {
            if (clamped in stops[i].bpm..stops[i + 1].bpm) {
                val range = stops[i + 1].bpm - stops[i].bpm
                val t = if (range == 0) 0f else (clamped - stops[i].bpm).toFloat() / range
                val r = (stops[i].r + t * (stops[i + 1].r - stops[i].r)).toInt()
                val g = (stops[i].g + t * (stops[i + 1].g - stops[i].g)).toInt()
                val b = (stops[i].b + t * (stops[i + 1].b - stops[i].b)).toInt()
                return Color.rgb(r, g, b)
            }
        }
        return Color.RED
    }

    private fun saveGuildSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        val enabledSet = guilds.values.filter { it.isEnabled }.map { it.id }.toSet()
        prefs.edit().putStringSet("enabled_guilds", enabledSet).apply()
    }

    private fun loadGuildSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        val enabledSet = prefs.getStringSet("enabled_guilds", null) ?: return
        guilds.values.forEach {
            it.isEnabled = enabledSet.contains(it.id)
        }
    }

    private fun saveThresholdSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        prefs.edit().putBoolean("new_thresholds", isNewThresholdsEnabled).apply()
    }

    private fun loadThresholdSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        isNewThresholdsEnabled = prefs.getBoolean("new_thresholds", true)
    }

    private fun saveFloatingSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        prefs.edit().putBoolean("floating_enabled", isFloatingEnabled).apply()
    }

    private fun loadFloatingSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        isFloatingEnabled = prefs.getBoolean("floating_enabled", false)
        if (isFloatingEnabled) {
            // Wait a bit for the system to be ready
            handler.postDelayed({
                if (isFloatingEnabled) {
                    toggleFloating(true, force = true)
                }
            }, 1000)
        }
    }

    private fun saveShowBpmSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        prefs.edit().putBoolean("show_bpm", isShowBpmEnabled).apply()
    }

    private fun loadShowBpmSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        isShowBpmEnabled = prefs.getBoolean("show_bpm", true)
    }

    private fun saveHeartbeatVibrationSettings() {
        getSharedPreferences("hr_bot_prefs", MODE_PRIVATE).edit()
            .putBoolean("heartbeat_vibration_enabled", isHeartbeatVibrationEnabled)
            .putInt("heartbeat_vibration_intensity", heartbeatVibrationIntensityPercent)
            .apply()
    }

    private fun loadHeartbeatVibrationSettings() {
        val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
        isHeartbeatVibrationEnabled = prefs.getBoolean("heartbeat_vibration_enabled", false)
        heartbeatVibrationIntensityPercent =
            prefs.getInt("heartbeat_vibration_intensity", 75).coerceIn(0, 100)
    }

    // ── Notification ─────────────────────────────────────────────────

    private var notificationStatusText = "Ready"

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "Heart Rate Service",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Keeps the HR BLE connection alive"
        }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val displayText = if (isHeartbeatSoundEnabled) {
            "$text • Heartbeat sound on"
        } else {
            text
        }

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("HR Bot")
            .setContentText(displayText)
            .setSmallIcon(R.drawable.ic_notification_dot)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        notificationStatusText = text
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun refreshForegroundServiceType() {
        val notification = buildNotification(notificationStatusText)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            var types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            if (isHeartbeatSoundEnabled) {
                types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            }
            startForeground(NOTIFICATION_ID, notification, types)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    // ── Logging helper ───────────────────────────────────────────────

    private fun log(msg: String) {
        Log.d(TAG, msg)
        handler.post { uiCallbacks.forEach { it.onLog(msg) } }
    }

    // ══════════════════════════════════════════════════════════════════
    //  BLE SCANNING
    // ══════════════════════════════════════════════════════════════════

    @SuppressLint("MissingPermission")
    private fun startScan() {
        if (!trackingRequested || scanning || gatt != null) return
        if (syncRole == SyncLinkManager.ROLE_MIRROR) {
            log("Mirroring another device's Fitbit connection; not scanning.")
            return
        }

        if (bluetoothAdapter?.isEnabled != true) {
            log("Bluetooth is not enabled.")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
            return
        }

        scanning = true
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scanning...") } }
        log("Scanning for Charge/Fitbit...")

        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        scanner = bluetoothAdapter?.bluetoothLeScanner
        try {
            scanner?.startScan(null, settings, scanCallback)
        } catch (e: Exception) {
            scanning = false
            log("Could not start BLE scan: ${e.message}")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scan failed") } }
            return
        }

        // Stop scanning after 2 minutes if nothing found to save battery
        handler.removeCallbacks(scanTimeoutRunnable)
        handler.postDelayed(scanTimeoutRunnable, 120_000)
        connectKnownFitbit()
    }

    @SuppressLint("MissingPermission")
    private fun knownFitbitDevice(): BluetoothDevice? {
        val adapter = bluetoothAdapter ?: return null
        val bonded = adapter.bondedDevices.orEmpty()
        val byName = bonded.firstOrNull { device ->
            val name = try { device.name } catch (_: Exception) { null } ?: ""
            "Charge" in name || "Fitbit" in name
        }
        if (byName != null) return byName
        val saved = lastFitbitAddress ?: return null
        return try { adapter.getRemoteDevice(saved) } catch (_: Exception) { null }
    }

    @SuppressLint("MissingPermission")
    private fun connectKnownFitbit() {
        val match = knownFitbitDevice()
        if (match != null && gatt == null && trackingRequested) {
            stopScan()
            connectToDevice(match)
        }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        handler.removeCallbacks(scanTimeoutRunnable)
        if (!scanning) return
        scanning = false
        try {
            scanner?.stopScan(scanCallback)
        } catch (_: Exception) {}
    }

    private val scanTimeoutRunnable = Runnable {
        if (scanning) {
            stopScan()
            if (trackingRequested) {
                log("Scan timeout (2m). Reconnecting.")
                scheduleReconnect("scan-timeout")
            } else {
                log("Scan timeout (2m). Stopping.")
                handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scan Timeout") } }
                updateNotification("Scan Timeout")
            }
        }
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (!scanning || gatt != null || !trackingRequested) return
            val name = result.device.name
            val mfg = result.scanRecord?.manufacturerSpecificData
            var fitbitMfg = false
            if (mfg != null) {
                for (i in 0 until mfg.size()) {
                    if (mfg.keyAt(i) == 0x012D) fitbitMfg = true
                }
            }
            val namedFitbit = name != null && ("Charge" in name || "Fitbit" in name)
            if (namedFitbit || fitbitMfg) {
                log("Found: ${name ?: "Heart Rate"} (${result.device.address})")
                stopScan()
                connectToDevice(result.device)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            log("Scan failed (error $errorCode). Retrying...")
            scanning = false
            handler.removeCallbacks(scanTimeoutRunnable)
            scheduleReconnect("scan-failed")
        }
    }

    private val reconnectRunnable = Runnable {
        if (!trackingRequested || gatt != null || scanning || syncRole == SyncLinkManager.ROLE_MIRROR) return@Runnable
        startScan()
    }

    private fun scheduleReconnect(reason: String) {
        if (!trackingRequested || syncRole == SyncLinkManager.ROLE_MIRROR) return
        handler.removeCallbacks(reconnectRunnable)
        val delayMs = (1_000L shl reconnectAttempt.coerceAtMost(4)).coerceAtMost(16_000L)
        reconnectAttempt += 1
        log("Reconnecting ($reason) in ${delayMs}ms...")
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Reconnecting...") } }
        handler.postDelayed(reconnectRunnable, delayMs)
    }

    // ══════════════════════════════════════════════════════════════════
    //  BLE CONNECTION + HEART RATE
    // ══════════════════════════════════════════════════════════════════

    @SuppressLint("MissingPermission")
    private fun connectToDevice(device: BluetoothDevice) {
        if (!trackingRequested || syncRole == SyncLinkManager.ROLE_MIRROR || gatt != null) return
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connecting...") } }
        val name = try { device.name } catch (_: SecurityException) { null }
        log("Connecting to ${name ?: "Fitbit"} (${device.address})...")

        gatt = try {
            device.connectGatt(
                this,
                false,
                gattCallback,
                BluetoothDevice.TRANSPORT_LE
            )
        } catch (e: Exception) {
            log("Could not create GATT connection: ${e.message}")
            null
        }
        if (gatt == null) scheduleReconnect("connect-failed")
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (gatt !== g) {
                try { g.close() } catch (_: Exception) {}
                return
            }

            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    if (!trackingRequested || syncRole == SyncLinkManager.ROLE_MIRROR) {
                        try { g.disconnect() } catch (_: Exception) {}
                        try { g.close() } catch (_: Exception) {}
                        if (gatt === g) gatt = null
                        return
                    }
                    lastFitbitAddress = g.device.address
                    reconnectAttempt = 0
                    g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
                    log("Connected! Discovering services...")
                    handler.post { uiCallbacks.forEach { it.onStatusUpdate("Discovering...") } }
                    g.discoverServices()
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    log("Disconnected (status $status).")
                    updateBpm(0)
                    try { g.close() } catch (_: Exception) {}
                    if (gatt === g) gatt = null
                    syncLink?.notifyFitbitState(false)

                    if (trackingRequested && syncRole != SyncLinkManager.ROLE_MIRROR) {
                        scheduleReconnect("status-$status")
                    }
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (gatt !== g) {
                try { g.close() } catch (_: Exception) {}
                return
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("Service discovery failed (status $status)")
                try { g.disconnect() } catch (_: Exception) {}
                return
            }

            val hrService = g.getService(HR_SERVICE_UUID)
            if (hrService == null) {
                log("Heart Rate service not found on device!")
                try { g.disconnect() } catch (_: Exception) {}
                return
            }

            val hrChar = hrService.getCharacteristic(HR_MEASUREMENT_UUID)
            if (hrChar == null) {
                log("HR Measurement characteristic not found!")
                try { g.disconnect() } catch (_: Exception) {}
                return
            }

            // Enable notifications locally
            if (!g.setCharacteristicNotification(hrChar, true)) {
                log("Could not enable local HR notifications")
                try { g.disconnect() } catch (_: Exception) {}
                return
            }

            // Write to the CCC descriptor to enable notifications on the remote device
            val descriptor = hrChar.getDescriptor(CLIENT_CHARACTERISTIC_CONFIG)
            if (descriptor == null) {
                log("CCC descriptor not found — notifications may not work")
                try { g.disconnect() } catch (_: Exception) {}
                return
            }

            @Suppress("DEPRECATION")
            descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            @Suppress("DEPRECATION")
            val writeStarted = g.writeDescriptor(descriptor)
            if (writeStarted) {
                log("HR stream requested! Accept 'Share' on your watch.")
                handler.post {
                    uiCallbacks.forEach { it.onStatusUpdate("Press Share on Fitbit") }
                }
                updateNotification("Connected — waiting for heart rate")
                syncLink?.notifyFitbitState(true)
            } else {
                log("Could not start HR notification setup")
                try { g.disconnect() } catch (_: Exception) {}
            }
        }

        override fun onDescriptorWrite(
            g: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int
        ) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                try { g.disconnect() } catch (_: Exception) {}
                return
            }
            reconnectAttempt = 0
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic
        ) {
            if (characteristic.uuid != HR_MEASUREMENT_UUID || gatt !== g) return
            @Suppress("DEPRECATION")
            val data = characteristic.value?.clone() ?: return
            if (data.size < 2) return

            // BPM is in byte[1] for 8-bit format (flag bit 0 == 0)
            val bpm = if (data[0].toInt() and 0x01 == 0) {
                data[1].toInt() and 0xFF
            } else {
                if (data.size < 3) return
                (data[1].toInt() and 0xFF) or ((data[2].toInt() and 0xFF) shl 8)
            }

            lastBpmTimestamp = System.currentTimeMillis()
            updateBpm(bpm)
        }
    }

    private fun updateBpm(bpm: Int, correctedPhase: Float? = null) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            handler.post { updateBpm(bpm, correctedPhase) }
            return
        }

        val sanitizedBpm = bpm.coerceAtLeast(0)
        val previousBpm = currentBpm
        val phaseCorrection = correctedPhase
            ?.toDouble()
            ?.takeIf { it.isFinite() }
        val nowNanos = SystemClock.elapsedRealtimeNanos()
        pulseClock.synchronize(sanitizedBpm, phaseCorrection, nowNanos)
        currentBpm = sanitizedBpm
        floatingManager?.updateBpm(sanitizedBpm)

        // Relay to the mirror here -- once per BLE notification (~1 Hz) rather than
        // from the 60 FPS phase loop. The mirror free-runs its own pulse phase from
        // the same bpm using the same formula, so streaming phase adds nothing but
        // ~60x the radio wakeups.
        if (syncRole == SyncLinkManager.ROLE_HOST) {
            syncLink?.notifyBpm(
                sanitizedBpm,
                pulseClock.phaseAt(nowNanos).toFloat()
            )
        }

        if (sanitizedBpm <= 0) {
            // Keep the user's session toggle on through a transient disconnect, but
            // stop playback and release the native sample until BPM returns.
            releaseHeartbeatSoundResources()
            try { vibrator?.cancel() } catch (_: Exception) {}
        } else if (isHeartbeatSoundEnabled) {
            val timingChanged = sanitizedBpm != previousBpm || phaseCorrection != null
            if (heartbeatSoundPool == null) {
                resumeHeartbeatSoundIfNeeded()
            } else if (heartbeatSoundLoaded && timingChanged) {
                scheduleNextHeartbeat(allowRecentCrest = true)
            }
            if (isHeartbeatVibrationEnabled && !heartbeatSoundLoaded &&
                (sanitizedBpm != previousBpm || phaseCorrection != null)
            ) {
                scheduleNextHeartbeat(allowRecentCrest = true)
            }
        } else if (isHeartbeatVibrationEnabled && (sanitizedBpm != previousBpm || phaseCorrection != null)) {
            scheduleNextHeartbeat(allowRecentCrest = true)
        }

        handler.post {
            uiCallbacks.forEach { it.onBpmUpdate(sanitizedBpm) }
            if (sanitizedBpm > 0) {
                uiCallbacks.forEach { it.onStatusUpdate("Connected") }
                updateNotification("HR: $sanitizedBpm BPM")
            } else {
                uiCallbacks.forEach { it.onStatusUpdate("Stale/Disconnected") }
                updateNotification("Heart Rate Lost")
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════
    //  DISCORD GATEWAY (minimal implementation for presence updates)
    // ══════════════════════════════════════════════════════════════════

    private fun connectDiscordGateway() {
        if (!isDiscordEnabled || syncRole == SyncLinkManager.ROLE_MIRROR) return
        log("Connecting to Discord Gateway...")
        handler.post { uiCallbacks.forEach { it.onDiscordStatusUpdate("Connecting...") } }

        val request = Request.Builder()
            .url(DISCORD_GATEWAY_URL)
            .build()

        discordSocket = wsHttpClient.newWebSocket(request, discordListener)
    }

    private fun disconnectDiscord(onDone: (() -> Unit)? = null) {
        if (discordSocket == null) {
            onDone?.invoke()
            return
        }
        
        log("Disconnecting from Discord...")
        discordHandler.removeCallbacks(presenceRunnable)
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }
        
        // If we're MIRRORing, we never wrote the nickname in the first place
        // (see sendPresenceUpdate) -- resetting it here would stomp on the
        // HOST's live value instead of leaving it alone.
        val wasMirror = syncRole == SyncLinkManager.ROLE_MIRROR

        // Use a background thread to ensure nickname resets are sent before closing
        Thread {
            // Reset nicknames to "BPMsworth" in all guilds before leaving
            if (!wasMirror) {
                guilds.values.forEach { guild ->
                    updateNicknameSync(guild, 0)
                }
            }

            try {
                // Send "invisible" status to appear offline immediately
                val presence = JSONObject().apply {
                    put("op", 3)
                    put("d", JSONObject().apply {
                        put("since", JSONObject.NULL)
                        put("activities", JSONArray())
                        put("status", "invisible")
                        put("afk", false)
                    })
                }
                discordSocket?.send(presence.toString())
            } catch (e: Exception) {
                Log.e(TAG, "Error sending offline presence", e)
            }
            
            // Graceful wait for messages to flush
            Thread.sleep(500)
            
            discordSocket?.close(1000, "Graceful Shutdown")
            discordSocket = null
            lastSentBpm = -1
            
            handler.post { 
                uiCallbacks.forEach { it.onDiscordStatusUpdate("Offline") }
                onDone?.invoke()
            }
        }.start()
    }

    private fun updateNicknameSync(guild: GuildInfo, bpm: Int) {
        val nickname = if (guild.isEnabled && bpm > 0) "$bpm BPM" else "BPMsworth"
        val json = JSONObject().apply {
            put("nick", nickname)
        }
        val body = json.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("https://discord.com/api/v10/guilds/${guild.id}/members/@me")
            .patch(body)
            .addHeader("Authorization", "Bot $DISCORD_TOKEN")
            .build()

        try {
            restHttpClient.newCall(request).execute().use { it.close() }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to update nickname sync for guild ${guild.name}", e)
        }
    }

    private val discordListener = object : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            log("Discord WebSocket opened")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val json = JSONObject(text)
                val op = json.getInt("op")
                if (!json.isNull("s")) {
                    lastSequence = json.getInt("s")
                }

                when (op) {
                    // HELLO — start heartbeating and identify
                    10 -> {
                        heartbeatInterval =
                            json.getJSONObject("d").getLong("heartbeat_interval")
                        startHeartbeating()
                        sendIdentify()
                    }
                    // HEARTBEAT ACK
                    11 -> { /* all good */ }
                    // DISPATCH
                    0 -> {
                        val eventName = json.optString("t", "")
                        when (eventName) {
                            "READY" -> {
                                log("Discord bot online!")
                                handler.post { uiCallbacks.forEach { it.onDiscordStatusUpdate("Online") } }
                                // Start the 15-second presence update loop
                                discordHandler.post(presenceRunnable)
                            }
                            "GUILD_CREATE" -> {
                                val d = json.getJSONObject("d")
                                val id = d.getString("id")
                                val name = d.getString("name")
                                log("Discovered server: $name")
                                if (!guilds.containsKey(id)) {
                                    val info = GuildInfo(id, name)
                                    guilds[id] = info
                                    loadGuildSettings()
                                    handler.post { uiCallbacks.forEach { it.onGuildsUpdated(guilds.values.toList()) } }
                                }
                            }
                            "GUILD_DELETE" -> {
                                val d = json.getJSONObject("d")
                                val id = d.getString("id")
                                guilds.remove(id)
                                handler.post { uiCallbacks.forEach { it.onGuildsUpdated(guilds.values.toList()) } }
                            }
                        }
                    }
                    // RECONNECT
                    7 -> {
                        log("Discord requested reconnect")
                        webSocket.close(1000, "Reconnecting")
                        handler.postDelayed({ if (isDiscordEnabled) connectDiscordGateway() }, 2_000)
                    }
                    // INVALID SESSION
                    9 -> {
                        log("Discord invalid session, re-identifying in 3s...")
                        handler.postDelayed({ if (isDiscordEnabled) connectDiscordGateway() }, 3_000)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Discord message parse error", e)
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (!isDiscordEnabled) return
            log("Discord connection failed: ${t.message}")
            handler.post { uiCallbacks.forEach { it.onDiscordStatusUpdate("Error") } }
            // Reconnect after delay
            handler.postDelayed({ if (isDiscordEnabled) connectDiscordGateway() }, 5_000)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            log("Discord WebSocket closed: $reason")
            // Discord closes with 4004 when the bot token is invalid. Treat that as
            // a configuration error rather than a transient offline event.
            if (code == 4004 && webSocket === discordSocket) {
                isDiscordEnabled = false
                discordSocket = null
                handler.post {
                    uiCallbacks.forEach {
                        it.onDiscordStatusUpdate("Token invalid - update DISCORD_TOKEN")
                    }
                }
            } else {
                handler.post { uiCallbacks.forEach { it.onDiscordStatusUpdate("Offline") } }
            }
        }
    }

    // ── Discord protocol helpers ─────────────────────────────────────

    private fun startHeartbeating() {
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }

        val runnable = object : Runnable {
            override fun run() {
                val hb = JSONObject().apply {
                    put("op", 1)
                    put("d", lastSequence ?: JSONObject.NULL)
                }
                discordSocket?.send(hb.toString())
                discordHandler.postDelayed(this, heartbeatInterval)
            }
        }
        heartbeatRunnable = runnable
        discordHandler.postDelayed(runnable, heartbeatInterval)
    }

    private fun sendIdentify() {
        val identify = JSONObject().apply {
            put("op", 2)
            put("d", JSONObject().apply {
                put("token", DISCORD_TOKEN)
                put("intents", 1) // Bit 0: GUILDS (required to receive server list)
                put("properties", JSONObject().apply {
                    put("os", "android")
                    put("browser", "hr_bot")
                    put("device", "hr_bot")
                })
                put("presence", JSONObject().apply {
                    put("activities", JSONArray())
                    put("status", "online")
                    put("since", 0)
                    put("afk", false)
                })
            })
        }
        discordSocket?.send(identify.toString())
        log("Sent IDENTIFY to Discord")
    }

    private fun sendPresenceUpdate() {
        if (discordSocket == null || !isDiscordEnabled) return

        // Both this app and the desktop widget share one Discord bot token. If
        // both independently PATCH the same guild nickname from their own
        // (possibly lagging) local bpm, they race and flicker. Only the sync
        // link's HOST -- the device with the real, authoritative BLE connection
        // -- writes to Discord; a MIRROR leaves it alone regardless of whether
        // its own Discord toggle is on.
        if (syncRole == SyncLinkManager.ROLE_MIRROR) return

        val bpm = currentBpm
        if (bpm == lastSentBpm) return
        lastSentBpm = bpm

        val presence = JSONObject().apply {
            put("op", 3)
            put("d", JSONObject().apply {
                put("since", JSONObject.NULL)
                put("activities", JSONArray())
                put("status", "online")
                put("afk", false)
            })
        }
        discordSocket?.send(presence.toString())

        // Update nicknames in each guild
        guilds.values.forEach { updateNicknameForGuild(it, bpm) }
    }

    private fun updateNicknameForGuild(guild: GuildInfo, bpm: Int) {
        if (discordSocket == null) return

        val nickname = if (guild.isEnabled && bpm > 0) "$bpm BPM" else "BPMsworth"

        // Discord API: PATCH /guilds/{guild.id}/members/@me
        val json = JSONObject().apply {
            put("nick", nickname)
        }
        val body = json.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("https://discord.com/api/v10/guilds/${guild.id}/members/@me")
            .patch(body)
            .addHeader("Authorization", "Bot $DISCORD_TOKEN")
            .build()

        restHttpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: java.io.IOException) {
                Log.e(TAG, "Failed to update nickname for guild ${guild.name}", e)
            }
            override fun onResponse(call: Call, response: Response) {
                response.close()
            }
        })
    }
}
