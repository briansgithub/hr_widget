package com.hrbot.android

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.Button
import android.widget.AdapterView
import android.widget.ArrayAdapter
import kotlin.math.sin
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import android.widget.Toast
import com.google.android.material.switchmaterial.SwitchMaterial

class MainActivity : AppCompatActivity() {

    private lateinit var bpmText: TextView
    private lateinit var statusText: TextView
    private lateinit var discordStatus: TextView
    private lateinit var logText: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var btnStartHr: Button
    private lateinit var btnStopHr: Button
    private lateinit var btnPulseScreen: Button
    private lateinit var btnLinkDevice: Button
    private lateinit var btnPairCode: Button
    private lateinit var syncStatusText: TextView
    private lateinit var switchDiscord: SwitchMaterial
    private lateinit var switchPulse: SwitchMaterial
    private lateinit var switchHeartbeatSound: SwitchMaterial
    private lateinit var heartbeatSoundVolumeLabel: TextView
    private lateinit var heartbeatSoundVolumeSeekBar: SeekBar
    private lateinit var heartbeatSoundEffectSpinner: Spinner
    private lateinit var switchFloating: SwitchMaterial
    private lateinit var switchShowBpm: SwitchMaterial
    private lateinit var switchNewThresholds: SwitchMaterial
    private lateinit var serverRecyclerView: RecyclerView

    private var service: HeartRateService? = null
    private var bound = false

    private var currentBpmValue = 0
    private var syncingHeartbeatSoundEffect = false


    private val uiCallback = object : HeartRateService.UICallback {
        override fun onBpmUpdate(bpm: Int) {
            runOnUiThread { updateBpm(bpm) }
        }
        override fun onStatusUpdate(status: String) {
            runOnUiThread { statusText.text = status }
        }
        override fun onDiscordStatusUpdate(status: String) {
            runOnUiThread {
                discordStatus.text = "Discord: $status"
                // Do not disable the user's toggle for a transient socket close:
                // the service reconnects automatically. Only reflect a real
                // configuration failure or an intentional service-side stop.
                if (service?.isDiscordEnabled == false && switchDiscord.isChecked) {
                    switchDiscord.isChecked = false
                }
            }
        }
        override fun onLog(message: String) {
            runOnUiThread { appendLog(message) }
        }
        override fun onGuildsUpdated(guilds: List<HeartRateService.GuildInfo>) {
            runOnUiThread { updateServerList() }
        }
        override fun onSyncStatusUpdate(status: String) {
            runOnUiThread { syncStatusText.text = status }
        }
        override fun onSyncRoleChange(role: String, peerName: String?) {
            runOnUiThread {
                btnLinkDevice.text = when (role) {
                    SyncLinkManager.ROLE_HOST -> "Sharing -> $peerName"
                    SyncLinkManager.ROLE_MIRROR -> "Mirror <- $peerName"
                    else -> "Link Device"
                }
            }
        }
        override fun onHeartbeatSoundStateChanged(enabled: Boolean, error: String?) {
            runOnUiThread {
                if (switchHeartbeatSound.isChecked != enabled) {
                    switchHeartbeatSound.isChecked = enabled
                }
                if (error != null) {
                    Toast.makeText(this@MainActivity, error, Toast.LENGTH_LONG).show()
                }
            }
        }
        override fun onPhaseUpdate(phase: Float) {
            runOnUiThread {
                if (!switchPulse.isChecked || currentBpmValue <= 0) {
                    bpmText.scaleX = 1.0f
                    bpmText.scaleY = 1.0f
                    return@runOnUiThread
                }

                // Python-equivalent scale factor:
                // 1.0 + sin(phase) * (bpm/60) * 0.0375
                val scaleFactor = 1.0f + (sin(phase.toDouble()).toFloat() * (currentBpmValue / 60.0f) * 0.0375f)

                bpmText.scaleX = scaleFactor
                bpmText.scaleY = scaleFactor
            }
        }
    }

    private fun startPulseAnimation() {
        // Now driven by HeartRateService's onPhaseUpdate callback
    }

    private fun stopPulseAnimation() {
        bpmText.scaleX = 1.0f
        bpmText.scaleY = 1.0f
    }

    companion object {
        private const val PERMISSION_REQUEST_CODE = 1001
    }

    // ── Service binding ──────────────────────────────────────────────

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as HeartRateService.LocalBinder
            service = localBinder.getService()
            bound = true
            service?.addCallback(uiCallback)
            syncUiFromService()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service?.removeCallback(uiCallback)
            service = null
            bound = false
        }
    }

    // ── Lifecycle ────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        bpmText = findViewById(R.id.bpmText)
        statusText = findViewById(R.id.statusText)
        discordStatus = findViewById(R.id.discordStatus)
        logText = findViewById(R.id.logText)
        logScroll = findViewById(R.id.logScroll)
        btnStartHr = findViewById(R.id.btnStartHr)
        btnStopHr = findViewById(R.id.btnStopHr)
        btnPulseScreen = findViewById(R.id.btnPulseScreen)
        btnLinkDevice = findViewById(R.id.btnLinkDevice)
        btnPairCode = findViewById(R.id.btnPairCode)
        syncStatusText = findViewById(R.id.syncStatusText)
        switchDiscord = findViewById(R.id.switchDiscord)
        switchPulse = findViewById(R.id.switchPulse)
        switchHeartbeatSound = findViewById(R.id.switchHeartbeatSound)
        heartbeatSoundVolumeLabel = findViewById(R.id.heartbeatSoundVolumeLabel)
        heartbeatSoundVolumeSeekBar = findViewById(R.id.heartbeatSoundVolumeSeekBar)
        heartbeatSoundEffectSpinner = findViewById(R.id.heartbeatSoundEffectSpinner)
        switchFloating = findViewById(R.id.switchFloating)
        switchShowBpm = findViewById(R.id.switchShowBpm)
        switchNewThresholds = findViewById(R.id.switchNewThresholds)
        serverRecyclerView = findViewById(R.id.serverRecyclerView)
        serverRecyclerView.layoutManager = LinearLayoutManager(this)

        heartbeatSoundEffectSpinner.adapter = ArrayAdapter(
            this,
            R.layout.item_heartbeat_sound_spinner,
            HeartbeatSoundCatalog.effects.map { it.label }
        ).apply {
            setDropDownViewResource(R.layout.item_heartbeat_sound_spinner)
        }
        heartbeatSoundEffectSpinner.onItemSelectedListener =
            object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: AdapterView<*>?,
                    view: View?,
                    position: Int,
                    id: Long
                ) {
                    if (syncingHeartbeatSoundEffect || !bound) return
                    val effect = HeartbeatSoundCatalog.effects.getOrNull(position) ?: return
                    if (service?.selectedHeartbeatSoundEffectId == effect.id) return

                    val didApply = service?.setHeartbeatSoundEffect(effect.id) ?: false
                    if (!didApply) {
                        Toast.makeText(
                            this@MainActivity,
                            "Heartbeat sound could not be loaded.",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                }

                override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            }

        btnStartHr.setOnClickListener {
            if (bound) {
                service?.startTracking()
            }
        }

        btnStopHr.setOnClickListener {
            if (bound) {
                service?.stopTracking()
                switchPulse.isChecked = false
                switchHeartbeatSound.isChecked = false
                switchFloating.isChecked = false
                switchDiscord.isChecked = false
                finishAffinity()
            }
        }

        btnPulseScreen.setOnClickListener {
            startActivity(Intent(this, PulseActivity::class.java))
        }

        btnLinkDevice.setOnClickListener {
            val svc = service
            if (svc == null) {
                Toast.makeText(this, "Service not ready", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (svc.syncRole == SyncLinkManager.ROLE_UNLINKED) {
                svc.requestSyncLink()
            } else {
                svc.disconnectSyncLink()
            }
        }

        btnLinkDevice.setOnLongClickListener {
            val svc = service ?: return@setOnLongClickListener false
            if (svc.syncRole != SyncLinkManager.ROLE_UNLINKED) return@setOnLongClickListener false

            val prefs = getSharedPreferences("hr_bot_prefs", MODE_PRIVATE)
            val lastIp = prefs.getString("last_sync_ip", "")

            val input = android.widget.EditText(this).apply {
                hint = "192.168.1.XX"
                if (!lastIp.isNullOrEmpty()) {
                    setText(lastIp)
                    setSelection(lastIp.length)
                }
            }
            android.app.AlertDialog.Builder(this)
                .setTitle("Manual Link (IP)")
                .setMessage("Enter the laptop's IP address (shown in the widget's console log) to link directly without mDNS.")
                .setView(input)
                .setPositiveButton("Link") { _, _ ->
                    val host = input.text.toString().trim()
                    if (host.isNotEmpty()) {
                        prefs.edit().putString("last_sync_ip", host).apply()
                        svc.connectManualSyncLink(host)
                    }
                }
                .setNegativeButton("Cancel", null)
                .show()
            true
        }

        btnPairCode.setOnClickListener {
            val svc = service ?: return@setOnClickListener
            val input = android.widget.EditText(this).apply {
                setText(svc.getSyncToken())
            }
            android.app.AlertDialog.Builder(this)
                .setTitle("Pairing Code")
                .setMessage("This code must match on both devices. Share it with the laptop app, or paste the laptop's code here to match it.")
                .setView(input)
                .setPositiveButton("Save") { _, _ ->
                    val value = input.text.toString().trim()
                    if (value.isNotEmpty()) {
                        svc.setSyncToken(value)
                        Toast.makeText(this, "Pairing code set", Toast.LENGTH_SHORT).show()
                    }
                }
                .setNegativeButton("Cancel", null)
                .show()
        }

        switchDiscord.setOnCheckedChangeListener { _, isChecked ->
            if (bound) {
                val didApply = service?.toggleDiscord(isChecked) ?: false
                if (isChecked && !didApply) {
                    switchDiscord.isChecked = false
                    Toast.makeText(
                        this,
                        "Discord bot token is not configured. Add DISCORD_TOKEN to local.properties.",
                        Toast.LENGTH_LONG
                    ).show()
                }
            }
        }

        switchShowBpm.setOnCheckedChangeListener { _, isChecked ->
            if (bound) {
                service?.toggleShowBpm(isChecked)
            }
        }

        switchNewThresholds.setOnCheckedChangeListener { _, isChecked ->
            if (bound) {
                service?.toggleNewThresholds(isChecked)
                // Refresh color of main BPM text
                if (currentBpmValue > 0) {
                    updateBpm(currentBpmValue)
                }
            }
        }

        switchPulse.setOnCheckedChangeListener { _, isChecked ->
            if (isChecked) {
                startPulseAnimation()
            } else {
                stopPulseAnimation()
            }
        }

        switchHeartbeatSound.setOnCheckedChangeListener { _, isChecked ->
            if (!bound) {
                if (isChecked) {
                    switchHeartbeatSound.isChecked = false
                    Toast.makeText(this, "Service not ready", Toast.LENGTH_SHORT).show()
                }
                return@setOnCheckedChangeListener
            }

            val didApply = service?.toggleHeartbeatSound(isChecked) ?: false
            if (isChecked && !didApply) {
                switchHeartbeatSound.isChecked = false
                Toast.makeText(
                    this,
                    "Heartbeat sound could not be loaded.",
                    Toast.LENGTH_LONG
                ).show()
            }
        }

        heartbeatSoundVolumeSeekBar.setOnSeekBarChangeListener(
            object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(
                    seekBar: SeekBar?,
                    progress: Int,
                    fromUser: Boolean
                ) {
                    updateHeartbeatSoundVolumeLabel(progress)
                    if (fromUser && bound) {
                        service?.setHeartbeatSoundVolume(progress)
                    }
                }

                override fun onStartTrackingTouch(seekBar: SeekBar?) = Unit

                override fun onStopTrackingTouch(seekBar: SeekBar?) = Unit
            }
        )

        switchFloating.setOnCheckedChangeListener { _, isChecked ->
            if (isChecked) {
                if (!checkOverlayPermission()) {
                    switchFloating.isChecked = false
                    requestOverlayPermission()
                    Toast.makeText(this, "Please grant overlay permission", Toast.LENGTH_SHORT).show()
                    return@setOnCheckedChangeListener
                }
            }
            if (bound) {
                service?.toggleFloating(isChecked)
            } else {
                Toast.makeText(this, "Service not ready", Toast.LENGTH_SHORT).show()
            }
        }

        requestPermissions()
    }

    private fun syncUiFromService() {
        service?.let {
            if (switchDiscord.isChecked != it.isDiscordEnabled) {
                switchDiscord.isChecked = it.isDiscordEnabled
            }
            if (switchFloating.isChecked != it.isFloatingEnabled) {
                switchFloating.isChecked = it.isFloatingEnabled
            }
            if (switchHeartbeatSound.isChecked != it.isHeartbeatSoundEnabled) {
                switchHeartbeatSound.isChecked = it.isHeartbeatSoundEnabled
            }
            if (heartbeatSoundVolumeSeekBar.progress != it.heartbeatSoundVolumePercent) {
                heartbeatSoundVolumeSeekBar.progress = it.heartbeatSoundVolumePercent
            }
            updateHeartbeatSoundVolumeLabel(it.heartbeatSoundVolumePercent)
            syncHeartbeatSoundEffect(it.selectedHeartbeatSoundEffectId)
            if (switchShowBpm.isChecked != it.isShowBpmEnabled) {
                switchShowBpm.isChecked = it.isShowBpmEnabled
            }
            if (switchNewThresholds.isChecked != it.isNewThresholdsEnabled) {
                switchNewThresholds.isChecked = it.isNewThresholdsEnabled
            }
            // Sync current BPM if already tracking
            if (it.currentBpm > 0) {
                updateBpm(it.currentBpm)
            }
            updateServerList()
        }
    }

    // The service keeps pushing 60 FPS phase updates to every registered callback.
    // Drop ours while backgrounded (Home, not Back -- onDestroy doesn't fire) so
    // those ticks don't run for a window nobody can see.
    override fun onResume() {
        super.onResume()
        if (bound) {
            service?.addCallback(uiCallback)
            syncUiFromService()
        }
    }

    override fun onPause() {
        super.onPause()
        if (bound) {
            service?.removeCallback(uiCallback)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        if (bound) {
            service?.removeCallback(uiCallback)
            unbindService(connection)
            bound = false
        }
    }

    // ── Permissions ──────────────────────────────────────────────────

    private fun checkOverlayPermission(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            android.provider.Settings.canDrawOverlays(this)
        } else {
            true
        }
    }

    private fun requestOverlayPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val intent = Intent(
                android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                android.net.Uri.parse("package:$packageName")
            )
            startActivity(intent)
        }
    }

    private fun requestPermissions() {
        val perms = mutableListOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.ACCESS_FINE_LOCATION
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            perms.add(Manifest.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE)
        }

        val needed = perms.filter {
            ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(
                this, needed.toTypedArray(), PERMISSION_REQUEST_CODE
            )
        } else {
            startHeartRateService()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            val allGranted = grantResults.all { it == PackageManager.PERMISSION_GRANTED }
            if (allGranted) {
                startHeartRateService()
            } else {
                statusText.text = "Permissions denied — BLE won't work"
            }
        }
    }

    // ── Service start ────────────────────────────────────────────────

    private fun startHeartRateService() {
        val intent = Intent(this, HeartRateService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        bindService(intent, connection, Context.BIND_AUTO_CREATE)
        statusText.text = "Ready"
    }

    // ── UI helpers ───────────────────────────────────────────────────

    private fun updateHeartbeatSoundVolumeLabel(percent: Int) {
        heartbeatSoundVolumeLabel.text = "Heartbeat Volume: $percent%"
    }

    private fun syncHeartbeatSoundEffect(effectId: String) {
        val selectedIndex = HeartbeatSoundCatalog.effects.indexOfFirst { it.id == effectId }
        if (
            selectedIndex < 0 ||
            heartbeatSoundEffectSpinner.selectedItemPosition == selectedIndex
        ) return

        syncingHeartbeatSoundEffect = true
        heartbeatSoundEffectSpinner.setSelection(selectedIndex, false)
        syncingHeartbeatSoundEffect = false
    }

    private fun updateBpm(bpm: Int) {
        currentBpmValue = bpm
        if (bpm > 0) {
            bpmText.text = "$bpm BPM"
            bpmText.setTextColor(service?.getBpmColor(bpm) ?: Color.WHITE)
            if (switchPulse.isChecked) {
                startPulseAnimation()
            }
        } else {
            bpmText.text = "?? BPM"
            bpmText.setTextColor(Color.WHITE)
            stopPulseAnimation()
        }
    }

    private fun appendLog(message: String) {
        logText.append("$message\n")
        logScroll.post { logScroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun updateServerList() {
        service?.let {
            val guilds = it.getGuilds()
            findViewById<View>(R.id.serverListLabel).visibility = if (guilds.isNotEmpty()) View.VISIBLE else View.GONE
            serverRecyclerView.adapter = ServerAdapter(guilds) { id, enabled ->
                it.toggleGuild(id, enabled)
            }
        }
    }

    class ServerAdapter(
        private val guilds: List<HeartRateService.GuildInfo>,
        private val onToggle: (String, Boolean) -> Unit
    ) : RecyclerView.Adapter<ServerAdapter.ViewHolder>() {

        class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
            val nameText: TextView = view.findViewById(R.id.guildName)
            val switch: SwitchMaterial = view.findViewById(R.id.guildSwitch)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_server, parent, false)
            return ViewHolder(view)
        }

        override fun onBindViewHolder(holder: ViewHolder, position: Int) {
            val guild = guilds[position]
            holder.nameText.text = guild.name
            holder.switch.isChecked = guild.isEnabled
            holder.switch.setOnCheckedChangeListener { _, isChecked ->
                onToggle(guild.id, isChecked)
            }
        }

        override fun getItemCount() = guilds.size
    }
}
