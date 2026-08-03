package com.hrbot.android

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.*
import android.widget.TextView
import kotlin.math.PI
import kotlin.math.sin

class FloatingWidgetManager(private val context: Context) {

    private var windowManager: WindowManager? = null
    private var floatingView: View? = null
    private var isVisible = false

    private val handler = Handler(Looper.getMainLooper())
    private var currentBpm = 0
    private var pulsePhase = 0f
    private var lastFrameTime = 0L

    private val pulseRunnable = object : Runnable {
        override fun run() {
            if (!isVisible || currentBpm <= 0) {
                updateBackgroundColor(Color.parseColor("#333333"))
                handler.postDelayed(this, 100)
                return
            }

            val now = System.currentTimeMillis()
            if (lastFrameTime == 0L) lastFrameTime = now
            val dt = (now - lastFrameTime) / 1000f
            lastFrameTime = now

            val bps = currentBpm / 60.0f
            pulsePhase += (dt * bps * PI.toFloat() * 2f)

            val pulseFactor = (sin(pulsePhase.toDouble()).toFloat() + 1f) / 2f
            val brightness = 0.4f + (pulseFactor * 0.6f)

            val baseColor = (context as? HeartRateService)?.getBpmColor(currentBpm) ?: Color.RED
            val r = (Color.red(baseColor) * brightness).toInt()
            val g = (Color.green(baseColor) * brightness).toInt()
            val b = (Color.blue(baseColor) * brightness).toInt()

            updateBackgroundColor(Color.rgb(r, g, b))

            handler.postDelayed(this, 16)
        }
    }

    private fun updateBackgroundColor(color: Int) {
        val background = floatingView?.background as? GradientDrawable
        background?.setColor(color)
    }

    fun show() {
        Log.d("FloatingWidget", "show() called, isVisible=$isVisible")
        if (isVisible) return
        
        windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val inflater = context.getSystemService(Context.LAYOUT_INFLATER_SERVICE) as LayoutInflater
        floatingView = inflater.inflate(R.layout.floating_widget, null)

        val layoutType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

        val density = context.resources.displayMetrics.density
        val sizePx = (60 * density).toInt()

        val params = WindowManager.LayoutParams(
            sizePx,
            sizePx,
            layoutType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_ALLOW_LOCK_WHILE_SCREEN_ON,
            PixelFormat.TRANSLUCENT
        )

        params.gravity = Gravity.TOP or Gravity.START
        params.x = 100
        params.y = 100

        floatingView?.setOnTouchListener(object : View.OnTouchListener {
            private var initialX = 0
            private var initialY = 0
            private var initialTouchX = 0f
            private var initialTouchY = 0f

            override fun onTouch(v: View, event: MotionEvent): Boolean {
                when (event.action) {
                    MotionEvent.ACTION_DOWN -> {
                        initialX = params.x
                        initialY = params.y
                        initialTouchX = event.rawX
                        initialTouchY = event.rawY
                        return true
                    }
                    MotionEvent.ACTION_MOVE -> {
                        params.x = initialX + (event.rawX - initialTouchX).toInt()
                        params.y = initialY + (event.rawY - initialTouchY).toInt()
                        windowManager?.updateViewLayout(floatingView, params)
                        return true
                    }
                }
                return false
            }
        })

        try {
            windowManager?.addView(floatingView, params)
            isVisible = true
            
            // Set initial color
            updateBackgroundColor(Color.parseColor("#333333"))
            
            lastFrameTime = System.currentTimeMillis()
            handler.post(pulseRunnable)
        } catch (e: Exception) {
            isVisible = false
        }
    }

    fun hide() {
        if (!isVisible) return
        handler.removeCallbacks(pulseRunnable)
        try {
            windowManager?.removeView(floatingView)
        } catch (e: Exception) {}
        isVisible = false
    }

    fun updateBpm(bpm: Int) {
        currentBpm = bpm
    }
}
