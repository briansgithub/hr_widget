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
import kotlin.math.PI
import kotlin.math.sin
import android.widget.ScrollView
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
    private lateinit var switchDiscord: SwitchMaterial
    private lateinit var switchPulse: SwitchMaterial
    private lateinit var switchFloating: SwitchMaterial
    private lateinit var switchNewThresholds: SwitchMaterial
    private lateinit var serverRecyclerView: RecyclerView

    private var service: HeartRateService? = null
    private var bound = false

    private var currentBpmValue = 0
    private var pulsePhase = 0f
    private var lastFrameTime = 0L
    private val animationHandler = Handler(Looper.getMainLooper())
    private var isPulseRunning = false

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
                // Ensure switch matches state if changed by Stop button
                if (status == "Offline" && switchDiscord.isChecked) {
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
    }

    private val pulseRunnable = object : Runnable {
        override fun run() {
            if (!switchPulse.isChecked || currentBpmValue <= 0) {
                bpmText.scaleX = 1.0f
                bpmText.scaleY = 1.0f
                isPulseRunning = false
                return
            }

            isPulseRunning = true
            val now = System.currentTimeMillis()
            if (lastFrameTime == 0L) lastFrameTime = now
            val dt = (now - lastFrameTime) / 1000f
            lastFrameTime = now

            val bps = currentBpmValue / 60.0f
            pulsePhase += (dt * bps * PI.toFloat() * 2f)

            // Python-equivalent scale factor:
            // 1.0 + sin(phase) * (bpm/60) * 0.0375
            val scaleFactor = 1.0f + (sin(pulsePhase.toDouble()).toFloat() * (currentBpmValue / 60.0f) * 0.0375f)

            bpmText.scaleX = scaleFactor
            bpmText.scaleY = scaleFactor

            animationHandler.postDelayed(this, 16)
        }
    }

    private fun startPulseAnimation() {
        if (isPulseRunning) return
        lastFrameTime = System.currentTimeMillis()
        animationHandler.removeCallbacks(pulseRunnable)
        animationHandler.post(pulseRunnable)
    }

    private fun stopPulseAnimation() {
        animationHandler.removeCallbacks(pulseRunnable)
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

            // Sync UI with current service state
            service?.let {
                if (switchDiscord.isChecked != it.isDiscordEnabled) {
                    switchDiscord.isChecked = it.isDiscordEnabled
                }
                if (switchFloating.isChecked != it.isFloatingEnabled) {
                    switchFloating.isChecked = it.isFloatingEnabled
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
        switchDiscord = findViewById(R.id.switchDiscord)
        switchPulse = findViewById(R.id.switchPulse)
        switchFloating = findViewById(R.id.switchFloating)
        switchNewThresholds = findViewById(R.id.switchNewThresholds)
        serverRecyclerView = findViewById(R.id.serverRecyclerView)
        serverRecyclerView.layoutManager = LinearLayoutManager(this)

        btnStartHr.setOnClickListener {
            if (bound) {
                service?.startTracking()
            }
        }

        btnStopHr.setOnClickListener {
            if (bound) {
                service?.stopTracking()
                switchPulse.isChecked = false
                switchFloating.isChecked = false
                switchDiscord.isChecked = false
                finishAffinity()
            }
        }

        btnPulseScreen.setOnClickListener {
            startActivity(Intent(this, PulseActivity::class.java))
        }

        switchDiscord.setOnCheckedChangeListener { _, isChecked ->
            if (bound) {
                service?.toggleDiscord(isChecked)
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

    private fun updateBpm(bpm: Int) {
        currentBpmValue = bpm
        if (bpm > 0) {
            bpmText.text = "$bpm BPM"
            bpmText.setTextColor(service?.getBpmColor(bpm) ?: Color.WHITE)
            if (switchPulse.isChecked) {
                startPulseAnimation()
            }
        } else {
            bpmText.text = "-- BPM"
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
