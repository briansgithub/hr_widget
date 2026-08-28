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
        private const val BLE_RECONNECT_DELAY_MS = 5_000L
        private const val BLE_SCAN_RETRY_DELAY_MS = 30_000L
        private const val BLE_OPERATION_TIMEOUT_MS = 30_000L
        private const val BLE_BOND_TIMEOUT_MS = 60_000L
        private const val BLE_FIRST_PACKET_TIMEOUT_MS = 60_000L

        private const val PREFS_NAME = "hr_bot_prefs"
        private const val PREF_TRACKING_REQUESTED = "tracking_requested"
        private const val PREF_LAST_FITBIT_ADDRESS = "last_fitbit_address"
        private const val PREF_LAST_FITBIT_NAME = "last_fitbit_name"
        private const val PREF_FITBIT_REPAIR_REQUIRED = "fitbit_repair_required"

        // Logs every BLE advertisement seen while scanning -- hundreds per second
        // in a busy RF environment. Debugging only; leave off in normal use.
        private const val VERBOSE_SCAN_LOG = false

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
        if (guidedRepairRequired) {
            cb.onStatusUpdate("Reconnect Fitbit in Bluetooth settings")
        }
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
    @Volatile private var gatt: BluetoothGatt? = null
    private var scanning = false
    private var hrNotificationsReady = false
    private var lastFitbitAddress: String? = null
    private var lastFitbitName: String? = null
    private var bleReconnectRunnable: Runnable? = null
    private var gattOperationTimeoutRunnable: Runnable? = null
    private var bondTimeoutRunnable: Runnable? = null
    private var firstPacketTimeoutRunnable: Runnable? = null
    private var waitingForBond = false
    private var awaitingFirstBpm = false
    private var consecutiveSecurityFailures = 0
    private var guidedRepairRequired = false
    var currentBpm = 0
        private set
    private var lastBpmTimestamp = 0L
    private var lastLocalHrTimestamp = 0L
    private val STALE_TIMEOUT = 45_000L

    private val stalenessRunnable = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            val activeGatt = gatt
            if (
                trackingRequested &&
                syncRole != SyncLinkManager.ROLE_MIRROR &&
                activeGatt != null &&
                hrNotificationsReady &&
                lastLocalHrTimestamp > 0L &&
                now - lastLocalHrTimestamp > STALE_TIMEOUT
            ) {
                // Android does not always emit STATE_DISCONNECTED when a peripheral
                // disappears. A silent notification stall must recycle the GATT too;
                // merely showing 0 BPM leaves startScan() blocked by a non-null GATT.
                recoverGatt(activeGatt, "Heart rate stream stalled for 45s")
            } else if (
                currentBpm > 0 &&
                lastBpmTimestamp > 0L &&
                now - lastBpmTimestamp > STALE_TIMEOUT
            ) {
                log("Heart rate data stale (45s). Neutralizing.")
                updateBpm(0)
            }
            handler.postDelayed(this, 5000)
        }
    }

    private var firstAttempt = true

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

    private val bluetoothStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothAdapter.ACTION_STATE_CHANGED) return
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)) {
                BluetoothAdapter.STATE_TURNING_OFF,
                BluetoothAdapter.STATE_OFF -> {
                    // Keep trackingRequested intact so STATE_ON can resume without
                    // requiring another tap from the user.
                    cancelBleReconnect()
                    stopScan()
                    gatt?.let { closeGatt(it, "Bluetooth turned off", reconnect = false) }
                    if (currentBpm != 0) updateBpm(0)
                    handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
                    updateNotification("Bluetooth off — waiting to reconnect")
                }
                BluetoothAdapter.STATE_ON -> {
                    scanner = bluetoothAdapter?.bluetoothLeScanner
                    if (
                        trackingRequested &&
                        syncRole != SyncLinkManager.ROLE_MIRROR &&
                        gatt == null &&
                        !scanning
                    ) {
                        scheduleBleReconnect("Bluetooth restored", delayMs = 0L)
                    }
                }
            }
        }
    }

    private val bondStateReceiver = object : BroadcastReceiver() {
        @SuppressLint("MissingPermission")
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return

            val device = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                intent.getParcelableExtra(
                    BluetoothDevice.EXTRA_DEVICE,
                    BluetoothDevice::class.java
                )
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
            } ?: return

            val activeGatt = gatt ?: return
            if (device.address != activeGatt.device.address) return

            val newState = intent.getIntExtra(
                BluetoothDevice.EXTRA_BOND_STATE,
                BluetoothDevice.ERROR
            )
            val previousState = intent.getIntExtra(
                BluetoothDevice.EXTRA_PREVIOUS_BOND_STATE,
                BluetoothDevice.ERROR
            )

            when (newState) {
                BluetoothDevice.BOND_BONDING -> {
                    if (waitingForBond) {
                        handler.post {
                            uiCallbacks.forEach { it.onStatusUpdate("Pairing Fitbit...") }
                        }
                    }
                }
                BluetoothDevice.BOND_BONDED -> {
                    if (waitingForBond && gatt === activeGatt) {
                        waitingForBond = false
                        cancelBondTimeout()
                        log("Fitbit pairing complete. Discovering services...")
                        beginServiceDiscovery(activeGatt)
                    }
                }
                BluetoothDevice.BOND_NONE -> {
                    if (
                        waitingForBond &&
                        previousState == BluetoothDevice.BOND_BONDING &&
                        gatt === activeGatt
                    ) {
                        waitingForBond = false
                        cancelBondTimeout()
                        handleSecurityFailure(activeGatt, "Fitbit pairing did not complete")
                    }
                }
            }
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
        loadBleRecoveryState()

        if (bluetoothAdapter == null || !bluetoothAdapter!!.isEnabled) {
            log("Bluetooth is not available or not enabled!")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
        } else {
            scanner = bluetoothAdapter!!.bluetoothLeScanner
        }
        registerReceiver(
            bluetoothStateReceiver,
            IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED)
        )
        registerReceiver(
            bondStateReceiver,
            IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        )

        syncLink = SyncLinkManager(
            context = this,
            deviceName = Build.MODEL ?: "Phone",
            token = loadSyncToken(),
            onRoleChange = { role, peerName ->
                // Already delivered on the main thread by SyncLinkManager.
                val oldRole = syncRole
                syncRole = role
                // A stale lastSentBpm cache could make sendPresenceUpdate() wrongly
                // believe Discord already shows the right value after regaining HOST
                // (if the current bpm happens to match whatever we last wrote before
                // we started MIRRORing). Force the next write unconditionally.
                lastSentBpm = -1
                uiCallbacks.forEach { it.onSyncRoleChange(role, peerName) }

                // If we become a MIRROR, cancel every pending local BLE path before
                // dropping the active session. Otherwise an already-posted scan retry
                // can race the role change and take the Fitbit back from the HOST.
                if (role == SyncLinkManager.ROLE_MIRROR) {
                    cancelBleReconnect()
                    cancelGattOperationTimeout()
                    stopScan()
                    gatt?.let {
                        closeGatt(
                            it,
                            "Handing over BLE control to $peerName",
                            reconnect = false
                        )
                    }
                }

                // Resume our own BLE scan if we stopped mirroring and are still meant to be tracking.
                // We also auto-enable tracking if we were previously mirroring (implies user interest).
                val wasMirror = (oldRole == SyncLinkManager.ROLE_MIRROR)
                if (role != SyncLinkManager.ROLE_MIRROR && (trackingRequested || wasMirror) && gatt == null && !scanning) {
                    if (wasMirror) {
                        log("Mirror lost; picking up session as Host.")
                        trackingRequested = true
                        persistTrackingRequested(true)
                        ensureTrackingInfrastructure()
                    }
                    scheduleBleReconnect("Mirror link ended", delayMs = 0L)
                }
            },
            onStatus = { text -> handler.post { uiCallbacks.forEach { it.onSyncStatusUpdate(text) } } },
            onRemoteBpm = { bpm: Int, phase: Float? ->
                lastBpmTimestamp = System.currentTimeMillis()
                updateBpm(bpm, phase)
            }
        ).also { it.start() }

        handler.post(phaseRunnable)

        if (trackingRequested) {
            ensureTrackingInfrastructure()
            if (guidedRepairRequired) {
                showGuidedRepairStatus()
            } else if (bluetoothAdapter?.isEnabled == true) {
                scheduleBleReconnect("Resuming heart rate tracking", delayMs = 0L)
            }
        }
    }

    fun requestSyncLink() = syncLink?.requestLink()
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
    private fun loadBleRecoveryState() {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        trackingRequested = prefs.getBoolean(PREF_TRACKING_REQUESTED, false)
        guidedRepairRequired = prefs.getBoolean(PREF_FITBIT_REPAIR_REQUIRED, false)
        lastFitbitAddress = prefs.getString(PREF_LAST_FITBIT_ADDRESS, null)
        lastFitbitName = prefs.getString(PREF_LAST_FITBIT_NAME, null)

        // Once bonded, Android retains the Fitbit's identity keys. Prefer the
        // framework's bonded-device identity over an old private scan address.
        val bondedFitbit = try {
            val bondedDevices = bluetoothAdapter?.bondedDevices.orEmpty()
            bondedDevices.firstOrNull { it.address == lastFitbitAddress }
                ?: bondedDevices.firstOrNull { device ->
                    val name = try { device.name } catch (_: SecurityException) { null }
                    !lastFitbitName.isNullOrBlank() && name == lastFitbitName
                }
        } catch (_: SecurityException) {
            null
        }

        bondedFitbit?.let { device ->
            lastFitbitAddress = device.address
            val name = try { device.name } catch (_: SecurityException) { null }
            if (!name.isNullOrBlank()) lastFitbitName = name
        }
    }

    @SuppressLint("MissingPermission")
    private fun rememberStreamingFitbit(device: BluetoothDevice) {
        lastFitbitAddress = device.address
        val name = try { device.name } catch (_: SecurityException) { null }
        if (!name.isNullOrBlank()) lastFitbitName = name

        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
            .putString(PREF_LAST_FITBIT_ADDRESS, lastFitbitAddress)
            .putString(PREF_LAST_FITBIT_NAME, lastFitbitName)
            .putBoolean(PREF_FITBIT_REPAIR_REQUIRED, false)
            .apply()
    }

    private fun persistTrackingRequested(requested: Boolean) {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
            .putBoolean(PREF_TRACKING_REQUESTED, requested)
            .apply()
    }

    private fun persistGuidedRepairRequired(required: Boolean) {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
            .putBoolean(PREF_FITBIT_REPAIR_REQUIRED, required)
            .apply()
    }

    private fun showGuidedRepairStatus() {
        handler.post {
            uiCallbacks.forEach {
                it.onStatusUpdate("Reconnect Fitbit in Bluetooth settings")
            }
            updateNotification("Fitbit needs reconnecting")
        }
    }

    @SuppressLint("MissingPermission")
    override fun onDestroy() {
        log("Service being destroyed. Cleaning up...")
        try { unregisterReceiver(screenStateReceiver) } catch (_: Exception) {}
        try { unregisterReceiver(bluetoothStateReceiver) } catch (_: Exception) {}
        try { unregisterReceiver(bondStateReceiver) } catch (_: Exception) {}
        cancelBleReconnect()
        cancelGattOperationTimeout()
        cancelBondTimeout()
        cancelFirstPacketTimeout()
        stopScan()
        disconnectDiscord()
        floatingManager?.hide()
        floatingManager = null
        gatt?.let { closeGatt(it, "Service destroyed", reconnect = false) }
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
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }
        discordHandler.removeCallbacks(presenceRunnable)
        super.onDestroy()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        when {
            trackingRequested ->
                log("App swiped away. Heart rate tracking remains active.")
            isHeartbeatSoundEnabled ->
                log("App swiped away. Heartbeat sound remains active.")
            syncRole == SyncLinkManager.ROLE_MIRROR ->
                log("App swiped away. Mirror link remains active.")
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
        if (!trackingRequested && !isHeartbeatSoundEnabled) {
            handler.removeCallbacks(stalenessRunnable)
        }
    }

    fun startTracking() {
        trackingRequested = true
        guidedRepairRequired = false
        consecutiveSecurityFailures = 0
        persistTrackingRequested(true)
        persistGuidedRepairRequired(false)
        ensureTrackingInfrastructure()

        if (bluetoothAdapter == null || !bluetoothAdapter!!.isEnabled) {
            log("Cannot start: Bluetooth is not enabled")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
            return
        }

        startScan()
    }

    @SuppressLint("MissingPermission")
    fun stopTracking() {
        log("Stopping all processes...")
        toggleHeartbeatSound(false)
        trackingRequested = false
        guidedRepairRequired = false
        consecutiveSecurityFailures = 0
        persistTrackingRequested(false)
        persistGuidedRepairRequired(false)
        cancelBleReconnect()
        cancelGattOperationTimeout()
        cancelBondTimeout()
        cancelFirstPacketTimeout()
        stopScan()
        gatt?.let { closeGatt(it, "Tracking stopped", reconnect = false) }

        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
        handler.removeCallbacks(stalenessRunnable)
        
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

    fun toggleDiscord(enabled: Boolean): Boolean {
        if (isDiscordEnabled == enabled) return true
        if (enabled && DISCORD_TOKEN.isEmpty()) {
            isDiscordEnabled = false
            log("Discord bot cannot start: DISCORD_TOKEN is not configured.")
            handler.post {
                uiCallbacks.forEach { it.onDiscordStatusUpdate("Token missing - configure DISCORD_TOKEN") }
            }
            return false
        }
        isDiscordEnabled = enabled
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
        if (!isHeartbeatSoundEnabled || !heartbeatSoundLoaded || currentBpm <= 0) return

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
                if (!isHeartbeatSoundEnabled || !heartbeatSoundLoaded || currentBpm <= 0) return

                val observedAtNanos = SystemClock.elapsedRealtimeNanos()
                val periodNanos = pulseClock.periodNanos() ?: return
                if (
                    heartbeatBeatGate.shouldPlay(
                        scheduledAtNanos,
                        observedAtNanos,
                        periodNanos
                    )
                ) {
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
                        return
                    }
                    heartbeatSoundStreamId = streamId
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

    fun getGuilds(): List<GuildInfo> = guilds.values.toList()

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

    private fun cancelBleReconnect() {
        bleReconnectRunnable?.let { handler.removeCallbacks(it) }
        bleReconnectRunnable = null
    }

    private fun cancelGattOperationTimeout() {
        gattOperationTimeoutRunnable?.let { handler.removeCallbacks(it) }
        gattOperationTimeoutRunnable = null
    }

    private fun cancelBondTimeout() {
        bondTimeoutRunnable?.let { handler.removeCallbacks(it) }
        bondTimeoutRunnable = null
    }

    private fun cancelFirstPacketTimeout() {
        firstPacketTimeoutRunnable?.let { handler.removeCallbacks(it) }
        firstPacketTimeoutRunnable = null
    }

    private fun scheduleBleReconnect(
        reason: String,
        delayMs: Long = BLE_RECONNECT_DELAY_MS
    ) {
        cancelBleReconnect()
        if (
            !trackingRequested ||
            syncRole == SyncLinkManager.ROLE_MIRROR ||
            guidedRepairRequired
        ) return

        val retry = Runnable {
            bleReconnectRunnable = null
            if (
                trackingRequested &&
                syncRole != SyncLinkManager.ROLE_MIRROR &&
                gatt == null &&
                !scanning &&
                !guidedRepairRequired
            ) {
                startScan()
            }
        }
        bleReconnectRunnable = retry

        val delayText = if (delayMs == 0L) "now" else "in ${delayMs / 1000}s"
        log("$reason. Retrying $delayText...")
        handler.post {
            uiCallbacks.forEach { it.onStatusUpdate("Reconnecting...") }
            updateNotification("Fitbit lost — reconnecting")
        }
        handler.postDelayed(retry, delayMs)
    }

    private fun armGattOperationTimeout(
        expectedGatt: BluetoothGatt,
        operation: String
    ) {
        cancelGattOperationTimeout()
        val timeout = Runnable {
            gattOperationTimeoutRunnable = null
            if (
                gatt === expectedGatt &&
                trackingRequested &&
                syncRole != SyncLinkManager.ROLE_MIRROR
            ) {
                // A timeout can be caused by ordinary radio loss. Only an
                // explicit security status or the separate first-BPM watchdog
                // may advance the guided pairing-repair breaker.
                recoverGatt(expectedGatt, "$operation timed out")
            }
        }
        gattOperationTimeoutRunnable = timeout
        handler.postDelayed(timeout, BLE_OPERATION_TIMEOUT_MS)
    }

    private fun armBondTimeout(expectedGatt: BluetoothGatt) {
        cancelBondTimeout()
        val timeout = Runnable {
            bondTimeoutRunnable = null
            if (
                gatt === expectedGatt &&
                waitingForBond &&
                trackingRequested &&
                syncRole != SyncLinkManager.ROLE_MIRROR
            ) {
                waitingForBond = false
                handleSecurityFailure(expectedGatt, "Fitbit pairing timed out")
            }
        }
        bondTimeoutRunnable = timeout
        handler.postDelayed(timeout, BLE_BOND_TIMEOUT_MS)
    }

    private fun armFirstPacketTimeout(expectedGatt: BluetoothGatt) {
        cancelFirstPacketTimeout()
        awaitingFirstBpm = true
        val timeout = Runnable {
            firstPacketTimeoutRunnable = null
            if (
                gatt === expectedGatt &&
                awaitingFirstBpm &&
                hrNotificationsReady &&
                trackingRequested &&
                syncRole != SyncLinkManager.ROLE_MIRROR
            ) {
                awaitingFirstBpm = false
                handleSecurityFailure(
                    expectedGatt,
                    "Fitbit connected but sent no heart rate"
                )
            }
        }
        firstPacketTimeoutRunnable = timeout
        handler.postDelayed(timeout, BLE_FIRST_PACKET_TIMEOUT_MS)
    }

    private fun handleSecurityFailure(expectedGatt: BluetoothGatt, reason: String) {
        if (gatt !== expectedGatt) return

        consecutiveSecurityFailures += 1
        log(
            "$reason " +
                "($consecutiveSecurityFailures/${FitbitBlePolicy.FAILURES_BEFORE_GUIDED_REPAIR})"
        )

        if (FitbitBlePolicy.needsGuidedRepair(consecutiveSecurityFailures)) {
            enterGuidedRepair(expectedGatt, reason)
        } else {
            closeGatt(expectedGatt, reason, reconnect = true)
        }
    }

    private fun enterGuidedRepair(expectedGatt: BluetoothGatt, reason: String) {
        guidedRepairRequired = true
        persistGuidedRepairRequired(true)
        cancelBleReconnect()
        stopScan()
        log(
            "$reason occurred twice. Automatic retries are paused; " +
                "reconnect the Fitbit in Android Bluetooth settings, then tap Start."
        )
        closeGatt(expectedGatt, reason, reconnect = false)
        showGuidedRepairStatus()
    }

    @SuppressLint("MissingPermission")
    private fun closeGatt(expectedGatt: BluetoothGatt, reason: String, reconnect: Boolean) {
        if (gatt !== expectedGatt) {
            // A callback from an older attempt must never clear a newer session.
            try { expectedGatt.close() } catch (_: Exception) {}
            return
        }

        cancelGattOperationTimeout()
        cancelBondTimeout()
        cancelFirstPacketTimeout()
        gatt = null
        hrNotificationsReady = false
        waitingForBond = false
        awaitingFirstBpm = false
        lastLocalHrTimestamp = 0L
        try { expectedGatt.disconnect() } catch (_: Exception) {}
        try { expectedGatt.close() } catch (_: Exception) {}
        syncLink?.notifyFitbitState(false)
        updateBpm(0)

        if (reconnect) scheduleBleReconnect(reason)
    }

    private fun recoverGatt(expectedGatt: BluetoothGatt, reason: String) {
        log(reason)
        closeGatt(expectedGatt, reason, reconnect = true)
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        Log.d(TAG, "startScan() called. scanning=$scanning gatt=$gatt isEnabled=${bluetoothAdapter?.isEnabled}")
        if (!trackingRequested || scanning || gatt != null) return
        if (guidedRepairRequired) {
            showGuidedRepairStatus()
            return
        }
        if (syncRole == SyncLinkManager.ROLE_MIRROR) {
            log("Mirroring another device's Fitbit connection; not scanning.")
            return
        }

        val adapter = bluetoothAdapter
        if (adapter?.isEnabled != true) {
            log("Bluetooth is off; waiting for it to be enabled.")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
            return
        }

        val activeScanner = adapter.bluetoothLeScanner
        if (activeScanner == null) {
            scheduleBleReconnect("BLE scanner unavailable")
            return
        }

        cancelBleReconnect()
        scanning = true
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scanning...") } }
        log("Scanning for Charge/Fitbit...")

        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        // Android suspends unfiltered scans while the display is off. The first
        // user-initiated scan may be unfiltered so we can learn the Fitbit; every
        // recovery scan has non-empty OR filters for its address/name/HR service.
        val filters: List<ScanFilter>? = if (
            lastFitbitAddress != null || lastFitbitName != null
        ) {
            val recoveryFilters = mutableListOf<ScanFilter>()
            recoveryFilters.add(
                ScanFilter.Builder()
                    .setServiceUuid(android.os.ParcelUuid(HR_SERVICE_UUID))
                    .build()
            )
            lastFitbitAddress?.let { address ->
                try {
                    recoveryFilters.add(
                        ScanFilter.Builder().setDeviceAddress(address).build()
                    )
                } catch (_: IllegalArgumentException) {}
            }
            lastFitbitName?.let { name ->
                recoveryFilters.add(ScanFilter.Builder().setDeviceName(name).build())
            }
            recoveryFilters
        } else {
            null
        }

        scanner = activeScanner
        try {
            activeScanner.startScan(filters, settings, scanCallback)
        } catch (e: Exception) {
            scanning = false
            log("Could not start BLE scan: ${e.message}")
            scheduleBleReconnect("BLE scan could not start")
            return
        }

        // Stop scanning after 2 minutes if nothing found to save battery
        handler.removeCallbacks(scanTimeoutRunnable)
        handler.postDelayed(scanTimeoutRunnable, 120_000)
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
                log("Scan timeout (2m). Waiting 30s before persistent retry...")
                handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scan Idle...") } }
                updateNotification("Scan Idle — Waiting to retry")
                scheduleBleReconnect("Scan timed out", BLE_SCAN_RETRY_DELAY_MS)
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
            handler.post {
                // stopScan() can leave already-queued results behind. These guards
                // prevent a duplicate result from opening and overwriting a second GATT.
                if (
                    !scanning ||
                    gatt != null ||
                    !trackingRequested ||
                    guidedRepairRequired
                ) return@post

                val device = result.device
                val name = try { device.name } catch (_: SecurityException) { null }
                val uuids = result.scanRecord?.serviceUuids

                if (VERBOSE_SCAN_LOG) Log.d(TAG, "ScanResult: addr=${device.address} name=$name")

                val hasHrService = uuids?.contains(
                    android.os.ParcelUuid(HR_SERVICE_UUID)
                ) ?: false
                val isFitbit = name?.let {
                    it.contains("Charge", ignoreCase = true) ||
                        it.contains("Fitbit", ignoreCase = true)
                } ?: false
                val isRememberedFitbit = device.address == lastFitbitAddress

                if (isFitbit || hasHrService || isRememberedFitbit) {
                    log("Match Found! name=${name ?: "Unknown"} addr=${device.address} hasHr=$hasHrService")
                    stopScan()
                    connectToDevice(device)
                }
            }
        }

        override fun onScanFailed(errorCode: Int) {
            handler.post {
                // Ignore a failure callback belonging to a scan we already stopped.
                if (!scanning) return@post
                scanning = false
                handler.removeCallbacks(scanTimeoutRunnable)
                scheduleBleReconnect("Scan failed (error $errorCode)", delayMs = 3_000L)
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════
    //  BLE CONNECTION + HEART RATE
    // ══════════════════════════════════════════════════════════════════

    @SuppressLint("MissingPermission")
    private fun fitbitBondState(device: BluetoothDevice): FitbitBlePolicy.BondState =
        when (device.bondState) {
            BluetoothDevice.BOND_BONDED -> FitbitBlePolicy.BondState.BONDED
            BluetoothDevice.BOND_BONDING -> FitbitBlePolicy.BondState.BONDING
            else -> FitbitBlePolicy.BondState.NONE
        }

    @SuppressLint("MissingPermission")
    private fun ensureBondThenDiscover(expectedGatt: BluetoothGatt) {
        if (gatt !== expectedGatt) return

        when (FitbitBlePolicy.bondAction(fitbitBondState(expectedGatt.device))) {
            FitbitBlePolicy.BondAction.DISCOVER_SERVICES -> {
                waitingForBond = false
                cancelBondTimeout()
                beginServiceDiscovery(expectedGatt)
            }
            FitbitBlePolicy.BondAction.WAIT_FOR_BOND -> {
                waitingForBond = true
                log("Fitbit pairing is already in progress...")
                handler.post {
                    uiCallbacks.forEach { it.onStatusUpdate("Pairing Fitbit...") }
                }
                updateNotification("Pairing Fitbit")
                armBondTimeout(expectedGatt)
            }
            FitbitBlePolicy.BondAction.START_BONDING -> {
                waitingForBond = true
                log("Securing the Fitbit connection...")
                handler.post {
                    uiCallbacks.forEach { it.onStatusUpdate("Pairing Fitbit...") }
                }
                updateNotification("Pairing Fitbit")

                val started = try {
                    expectedGatt.device.createBond()
                } catch (e: Exception) {
                    log("Could not start Fitbit pairing: ${e.message}")
                    false
                }

                if (started) {
                    armBondTimeout(expectedGatt)
                } else {
                    // Bonding can start or complete between the state check and
                    // createBond(), so inspect the state again before treating a
                    // false return as an actual failure.
                    when (expectedGatt.device.bondState) {
                        BluetoothDevice.BOND_BONDED -> {
                            waitingForBond = false
                            beginServiceDiscovery(expectedGatt)
                        }
                        BluetoothDevice.BOND_BONDING -> armBondTimeout(expectedGatt)
                        else -> {
                            waitingForBond = false
                            handleSecurityFailure(
                                expectedGatt,
                                "Could not start Fitbit pairing"
                            )
                        }
                    }
                }
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun beginServiceDiscovery(expectedGatt: BluetoothGatt) {
        if (gatt !== expectedGatt) return
        if (!trackingRequested || syncRole == SyncLinkManager.ROLE_MIRROR) {
            closeGatt(expectedGatt, "Connection no longer needed", reconnect = false)
            return
        }

        log("Connected securely. Discovering services...")
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connecting...") } }
        cancelGattOperationTimeout()
        if (expectedGatt.discoverServices()) {
            armGattOperationTimeout(expectedGatt, "Service discovery")
        } else {
            recoverGatt(expectedGatt, "Could not start service discovery")
        }
    }

    @SuppressLint("MissingPermission")
    private fun connectToDevice(device: BluetoothDevice) {
        if (
            !trackingRequested ||
            syncRole == SyncLinkManager.ROLE_MIRROR ||
            gatt != null
        ) return

        cancelBleReconnect()
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connecting...") } }
        val name = try { device.name } catch (_: SecurityException) { null }
        log("Connecting to ${name ?: "Fitbit"} (${device.address})...")

        // A direct connection is intentionally used after a positive scan result.
        // Supplying our Handler serializes all GATT callbacks with retry state.
        val newGatt = try {
            device.connectGatt(
                this,
                false,
                gattCallback,
                BluetoothDevice.TRANSPORT_LE,
                BluetoothDevice.PHY_LE_1M_MASK,
                handler
            )
        } catch (e: Exception) {
            log("Could not create GATT connection: ${e.message}")
            null
        }

        if (newGatt == null) {
            scheduleBleReconnect("Could not create GATT connection")
            return
        }

        gatt = newGatt
        hrNotificationsReady = false
        lastLocalHrTimestamp = 0L
        armGattOperationTimeout(newGatt, "Fitbit connection")
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (gatt !== g) {
                try { g.close() } catch (_: Exception) {}
                return
            }

            if (status != BluetoothGatt.GATT_SUCCESS) {
                // Connection-state status 0x08 is also Android's legacy
                // out-of-range connection timeout, so it is not safe to treat
                // attribute-security numeric codes as security failures here.
                // Security classification is limited to GATT operation callbacks.
                recoverGatt(g, "GATT connection failed (status $status)")
                return
            }

            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    if (!trackingRequested || syncRole == SyncLinkManager.ROLE_MIRROR) {
                        closeGatt(g, "Connection no longer needed", reconnect = false)
                        return
                    }
                    cancelGattOperationTimeout()
                    ensureBondThenDiscover(g)
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    recoverGatt(g, "Fitbit disconnected")
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
                if (FitbitBlePolicy.isSecurityGattFailure(status)) {
                    handleSecurityFailure(g, "Fitbit security check failed")
                } else {
                    recoverGatt(g, "Service discovery failed (status $status)")
                }
                return
            }

            val hrService = g.getService(HR_SERVICE_UUID)
            if (hrService == null) {
                recoverGatt(g, "Heart Rate service not found on device")
                return
            }

            val hrChar = hrService.getCharacteristic(HR_MEASUREMENT_UUID)
            if (hrChar == null) {
                recoverGatt(g, "HR Measurement characteristic not found")
                return
            }

            // Enable notifications locally
            if (!g.setCharacteristicNotification(hrChar, true)) {
                recoverGatt(g, "Could not enable local HR notifications")
                return
            }

            // Write to the CCC descriptor to enable notifications on the remote device
            val descriptor = hrChar.getDescriptor(CLIENT_CHARACTERISTIC_CONFIG)
            if (descriptor == null) {
                recoverGatt(g, "HR notification descriptor not found")
                return
            }

            @Suppress("DEPRECATION")
            descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            armGattOperationTimeout(g, "HR notification setup")
            @Suppress("DEPRECATION")
            val writeStarted = g.writeDescriptor(descriptor)
            if (!writeStarted) {
                recoverGatt(g, "Could not start HR notification setup")
            }
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorWrite(
            g: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int
        ) {
            if (descriptor.uuid != CLIENT_CHARACTERISTIC_CONFIG) return
            if (gatt !== g) {
                try { g.close() } catch (_: Exception) {}
                return
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                if (FitbitBlePolicy.isSecurityGattFailure(status)) {
                    handleSecurityFailure(g, "Fitbit authorization failed")
                } else {
                    recoverGatt(g, "HR notification setup failed (status $status)")
                }
                return
            }

            cancelGattOperationTimeout()
            hrNotificationsReady = true
            lastLocalHrTimestamp = 0L
            armFirstPacketTimeout(g)
            if (firstAttempt) {
                log("HR stream requested! Accept 'Share' on your watch.")
                handler.post {
                    uiCallbacks.forEach { it.onStatusUpdate("Press Share on Fitbit") }
                }
            } else {
                log("Fitbit connected; waiting for heart rate...")
                handler.post { uiCallbacks.forEach { it.onStatusUpdate("Waiting for HR...") } }
            }
            updateNotification("Connected — waiting for heart rate")
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

            val now = System.currentTimeMillis()
            val firstPacketForSession = awaitingFirstBpm
            if (firstPacketForSession) {
                awaitingFirstBpm = false
                cancelFirstPacketTimeout()
                consecutiveSecurityFailures = 0
                guidedRepairRequired = false
                persistGuidedRepairRequired(false)
                rememberStreamingFitbit(g.device)
                syncLink?.notifyFitbitState(true)
                log("HR stream active!")
                handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connected") } }
                updateNotification("Connected — streaming HR")
            }
            firstAttempt = false
            lastLocalHrTimestamp = now
            lastBpmTimestamp = now
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
        } else if (isHeartbeatSoundEnabled) {
            val timingChanged = sanitizedBpm != previousBpm || phaseCorrection != null
            if (heartbeatSoundPool == null) {
                resumeHeartbeatSoundIfNeeded()
            } else if (heartbeatSoundLoaded && timingChanged) {
                scheduleNextHeartbeat(allowRecentCrest = true)
            }
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
        if (!isDiscordEnabled) return
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
