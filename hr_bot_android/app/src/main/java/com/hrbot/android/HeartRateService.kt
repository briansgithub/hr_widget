package com.hrbot.android

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
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

    // ── State ────────────────────────────────────────────────────────

    private val handler = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    private var bluetoothAdapter: BluetoothAdapter? = null
    private var scanner: BluetoothLeScanner? = null
    private var gatt: BluetoothGatt? = null
    private var scanning = false
    var currentBpm = 0
        private set
    private var lastHrTimestamp = 0L
    private val STALE_TIMEOUT = 45_000L

    private val stalenessRunnable = object : Runnable {
        override fun run() {
            if (currentBpm > 0 && System.currentTimeMillis() - lastHrTimestamp > STALE_TIMEOUT) {
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
    private val presenceRunnable = object : Runnable {
        override fun run() {
            sendPresenceUpdate()
            discordHandler.postDelayed(this, 15_000)
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
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, buildNotification("Ready"), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, buildNotification("Ready"))
        }

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
                syncRole = role
                uiCallbacks.forEach { it.onSyncRoleChange(role, peerName) }
                // Resume our own BLE scan if we stopped mirroring and are still meant to be tracking.
                if (role != SyncLinkManager.ROLE_MIRROR && trackingRequested && gatt == null && !scanning) {
                    startScan()
                }
            },
            onStatus = { text -> handler.post { uiCallbacks.forEach { it.onSyncStatusUpdate(text) } } },
            onRemoteBpm = { bpm -> updateBpm(bpm) }
        ).also { it.start() }
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
    override fun onDestroy() {
        log("Service being destroyed. Cleaning up...")
        stopScan()
        disconnectDiscord()
        floatingManager?.hide()
        floatingManager = null
        gatt?.close()
        gatt = null
        syncLink?.notifyFitbitState(false)
        syncLink?.stop()
        syncLink = null
        
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
        
        handler.removeCallbacks(stalenessRunnable)
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }
        discordHandler.removeCallbacks(presenceRunnable)
        super.onDestroy()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        log("App swiped away. Shutting down service...")
        stopSelf()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    // ── Public API for Activity ──────────────────────────────────────

    fun startTracking() {
        if (bluetoothAdapter == null || !bluetoothAdapter!!.isEnabled) {
            log("Cannot start: Bluetooth is not enabled")
            handler.post { uiCallbacks.forEach { it.onStatusUpdate("Enable Bluetooth") } }
            return
        }
        if (scanning) return

        trackingRequested = true

        if (wakeLock?.isHeld == false) {
            wakeLock?.acquire(24 * 60 * 60 * 1000L) // 24 hours max
        }

        handler.removeCallbacks(stalenessRunnable)
        handler.post(stalenessRunnable)

        startScan()
    }

    @SuppressLint("MissingPermission")
    fun stopTracking() {
        log("Stopping all processes...")
        trackingRequested = false
        stopScan()
        
        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
        handler.removeCallbacks(stalenessRunnable)
        
        currentBpm = 0
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
            // Disconnect BLE
            gatt?.disconnect()
            gatt?.close()
            gatt = null
            syncLink?.notifyFitbitState(false)
            
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
            if (isDiscordEnabled && discordSocket != null) {
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

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("HR Bot")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notification_dot)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, buildNotification(text))
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
        if (scanning || gatt != null) return
        if (syncRole == SyncLinkManager.ROLE_MIRROR) {
            log("Mirroring another device's Fitbit connection; not scanning.")
            return
        }
        scanning = true
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Scanning...") } }
        log("Scanning for Charge/Fitbit...")

        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        scanner = bluetoothAdapter?.bluetoothLeScanner
        scanner?.startScan(null, settings, scanCallback)

        // Stop scanning after 2 minutes if nothing found to save battery
        handler.removeCallbacks(scanTimeoutRunnable)
        handler.postDelayed(scanTimeoutRunnable, 120_000)
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        handler.removeCallbacks(scanTimeoutRunnable)
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
                handler.postDelayed({
                    if (trackingRequested && !scanning && gatt == null && syncRole != SyncLinkManager.ROLE_MIRROR) {
                        startScan()
                    }
                }, 30_000)
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
            val name = result.device.name ?: return
            if ("Charge" in name || "Fitbit" in name) {
                log("Found: $name (${result.device.address})")
                stopScan()
                connectToDevice(result.device)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            log("Scan failed (error $errorCode). Retrying in 3s...")
            scanning = false
            handler.postDelayed({ startScan() }, 3_000)
        }
    }

    // ══════════════════════════════════════════════════════════════════
    //  BLE CONNECTION + HEART RATE
    // ══════════════════════════════════════════════════════════════════

    @SuppressLint("MissingPermission")
    private fun connectToDevice(device: BluetoothDevice) {
        handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connecting...") } }
        log("Connecting to ${device.name}...")

        gatt = device.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    log("Connected! Discovering services...")
                    handler.post { uiCallbacks.forEach { it.onStatusUpdate("Discovering...") } }
                    g.discoverServices()
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    log("Disconnected.")
                    updateBpm(0)
                    g.close()
                    gatt = null
                    syncLink?.notifyFitbitState(false)
                    
                    if (trackingRequested && syncRole != SyncLinkManager.ROLE_MIRROR) {
                        log("Persistent mode active. Restarting scan in 5s...")
                        handler.postDelayed({
                            if (trackingRequested && gatt == null && !scanning && syncRole != SyncLinkManager.ROLE_MIRROR) {
                                startScan()
                            }
                        }, 5_000)
                    }
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("Service discovery failed (status $status)")
                return
            }

            val hrService = g.getService(HR_SERVICE_UUID)
            if (hrService == null) {
                log("Heart Rate service not found on device!")
                return
            }

            val hrChar = hrService.getCharacteristic(HR_MEASUREMENT_UUID)
            if (hrChar == null) {
                log("HR Measurement characteristic not found!")
                return
            }

            // Enable notifications locally
            g.setCharacteristicNotification(hrChar, true)

            // Write to the CCC descriptor to enable notifications on the remote device
            val descriptor = hrChar.getDescriptor(CLIENT_CHARACTERISTIC_CONFIG)
            if (descriptor != null) {
                descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                g.writeDescriptor(descriptor)
                if (firstAttempt) {
                    log("HR stream requested! Accept 'Share' on your watch.")
                    handler.post { uiCallbacks.forEach { it.onStatusUpdate("Press Share...") } }
                } else {
                    log("HR stream active!")
                    handler.post { uiCallbacks.forEach { it.onStatusUpdate("Connected") } }
                    updateNotification("Connected — streaming HR")
                }
                syncLink?.notifyFitbitState(true)
            } else {
                log("CCC descriptor not found — notifications may not work")
            }
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic
        ) {
            if (characteristic.uuid == HR_MEASUREMENT_UUID) {
                val data = characteristic.value ?: return
                // BPM is in byte[1] for 8-bit format (flag bit 0 == 0)
                val bpm = if (data[0].toInt() and 0x01 == 0) {
                    data[1].toInt() and 0xFF
                } else {
                    // 16-bit format
                    (data[1].toInt() and 0xFF) or ((data[2].toInt() and 0xFF) shl 8)
                }

                currentBpm = bpm
                lastHrTimestamp = System.currentTimeMillis()
                updateBpm(bpm)
                syncLink?.notifyBpm(bpm)
            }
        }
    }

    private fun updateBpm(bpm: Int) {
        currentBpm = bpm
        floatingManager?.updateBpm(bpm)
        handler.post {
            uiCallbacks.forEach { it.onBpmUpdate(bpm) }
            if (bpm > 0) {
                uiCallbacks.forEach { it.onStatusUpdate("Connected") }
                updateNotification("HR: $bpm BPM")
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

        val client = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS) // infinite for WebSocket
            .build()

        val request = Request.Builder()
            .url(DISCORD_GATEWAY_URL)
            .build()

        discordSocket = client.newWebSocket(request, discordListener)
    }

    private fun disconnectDiscord(onDone: (() -> Unit)? = null) {
        if (discordSocket == null) {
            onDone?.invoke()
            return
        }
        
        log("Disconnecting from Discord...")
        discordHandler.removeCallbacks(presenceRunnable)
        heartbeatRunnable?.let { discordHandler.removeCallbacks(it) }
        
        // Use a background thread to ensure nickname resets are sent before closing
        Thread {
            // Reset nicknames to "BPMsworth" in all guilds before leaving
            guilds.values.forEach { guild ->
                updateNicknameSync(guild, 0)
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
            
            // Disconnect BLE here if we are shutting down
            gatt?.let {
                it.disconnect()
                it.close()
            }
            gatt = null

            handler.post { 
                uiCallbacks.forEach { it.onDiscordStatusUpdate("Offline") }
                onDone?.invoke()
            }
        }.start()
    }

    private fun updateNicknameSync(guild: GuildInfo, bpm: Int) {
        val nickname = if (guild.isEnabled && bpm > 0) "BPM: $bpm" else "BPMsworth"
        val client = OkHttpClient()
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
            client.newCall(request).execute().use { it.close() }
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
        
        val nickname = if (guild.isEnabled && bpm > 0) "BPM: $bpm" else "BPMsworth"
        
        // Discord API: PATCH /guilds/{guild.id}/members/@me
        val client = OkHttpClient()
        val json = JSONObject().apply {
            put("nick", nickname)
        }
        val body = json.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("https://discord.com/api/v10/guilds/${guild.id}/members/@me")
            .patch(body)
            .addHeader("Authorization", "Bot $DISCORD_TOKEN")
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: java.io.IOException) {
                Log.e(TAG, "Failed to update nickname for guild ${guild.name}", e)
            }
            override fun onResponse(call: Call, response: Response) {
                response.close()
            }
        })
    }
}
