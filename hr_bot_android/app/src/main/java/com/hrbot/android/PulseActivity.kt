package com.hrbot.android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity
import kotlin.math.PI
import kotlin.math.sin

class PulseActivity : AppCompatActivity() {

    private lateinit var pulseContainer: View
    private var service: HeartRateService? = null
    private var bound = false

    private var currentBpmValue = 0
    private var pulsePhase = 0f
    private var lastFrameTime = 0L
    private val animationHandler = Handler(Looper.getMainLooper())

    private val uiCallback = object : HeartRateService.UICallback {
        override fun onBpmUpdate(bpm: Int) {
            runOnUiThread { currentBpmValue = bpm }
        }
        override fun onStatusUpdate(status: String) {}
        override fun onDiscordStatusUpdate(status: String) {}
        override fun onLog(message: String) {}
        override fun onGuildsUpdated(guilds: List<HeartRateService.GuildInfo>) {}
    }

    private val pulseRunnable = object : Runnable {
        override fun run() {
            if (currentBpmValue <= 0) {
                pulseContainer.setBackgroundColor(Color.BLACK)
                animationHandler.postDelayed(this, 100)
                return
            }

            val now = System.currentTimeMillis()
            if (lastFrameTime == 0L) lastFrameTime = now
            val dt = (now - lastFrameTime) / 1000f
            lastFrameTime = now

            val bps = currentBpmValue / 60.0f
            pulsePhase += (dt * bps * PI.toFloat() * 2f)

            // Pulse factor between 0.0 and 1.0
            val pulseFactor = (sin(pulsePhase.toDouble()).toFloat() + 1f) / 2f
            
            // Brightness pulses between 0.4 and 1.0
            val brightness = 0.4f + (pulseFactor * 0.6f)

            // Constant color for the current BPM
            val baseColor = service?.getBpmColor(currentBpmValue) ?: Color.BLACK

            val r = (Color.red(baseColor) * brightness).toInt()
            val g = (Color.green(baseColor) * brightness).toInt()
            val b = (Color.blue(baseColor) * brightness).toInt()

            pulseContainer.setBackgroundColor(Color.rgb(r, g, b))

            animationHandler.postDelayed(this, 16)
        }
    }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as HeartRateService.LocalBinder
            service = localBinder.getService()
            bound = true
            service?.addCallback(uiCallback)
            currentBpmValue = service?.currentBpm ?: 0
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service?.removeCallback(uiCallback)
            service = null
            bound = false
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        // Full screen
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN 
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION 
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY)
        
        setContentView(R.layout.activity_pulse)
        pulseContainer = findViewById(R.id.pulseContainer)

        val gestureDetector = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDoubleTap(e: MotionEvent): Boolean {
                finish()
                return true
            }
        })

        pulseContainer.setOnTouchListener { _, event ->
            gestureDetector.onTouchEvent(event)
            true
        }

        val intent = Intent(this, HeartRateService::class.java)
        bindService(intent, connection, Context.BIND_AUTO_CREATE)

        lastFrameTime = System.currentTimeMillis()
        animationHandler.post(pulseRunnable)
    }

    override fun onDestroy() {
        super.onDestroy()
        animationHandler.removeCallbacks(pulseRunnable)
        if (bound) {
            service?.removeCallback(uiCallback)
            unbindService(connection)
            bound = false
        }
    }
}
