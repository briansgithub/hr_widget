package com.hrbot.android

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.*
import android.widget.TextView
import kotlin.math.sin


class FloatingWidgetManager(private val context: Context) {

    private var windowManager: WindowManager? = null
    private var floatingView: View? = null
    private var bpmTextView: TextView? = null
    private var isVisible = false

    private var currentBpm = 0
    private var isShowBpm = true
    private val handler = Handler(Looper.getMainLooper())


    fun updatePhase(phase: Float) {
        if (!isVisible || currentBpm <= 0) {
            updateBackgroundColor(Color.parseColor("#333333"))
            return
        }

        val pulseFactor = (sin(phase.toDouble()).toFloat() + 1f) / 2f
        val brightness = 0.4f + (pulseFactor * 0.6f)

        val baseColor = (context as? HeartRateService)?.getBpmColor(currentBpm) ?: Color.RED
        val r = (Color.red(baseColor) * brightness).toInt()
        val g = (Color.green(baseColor) * brightness).toInt()
        val b = (Color.blue(baseColor) * brightness).toInt()

        updateBackgroundColor(Color.rgb(r, g, b))
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

        val detector = TapDragDetector(
            slopPx = ViewConfiguration.get(context).scaledTouchSlop.toFloat(),
            maxTapDurationMs = ViewConfiguration.getLongPressTimeout().toLong()
        )
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
                        detector.onDown(event.rawX, event.rawY, event.eventTime)
                        return true
                    }
                    MotionEvent.ACTION_MOVE -> {
                        if (detector.onMove(event.rawX, event.rawY)) {
                            params.x = initialX + (event.rawX - initialTouchX).toInt()
                            params.y = initialY + (event.rawY - initialTouchY).toInt()
                            try {
                                windowManager?.updateViewLayout(floatingView, params)
                            } catch (e: Exception) {
                                Log.w("FloatingWidget", "updateViewLayout failed", e)
                            }
                        }
                        return true
                    }
                    MotionEvent.ACTION_UP -> {
                        if (detector.onUp(event.eventTime)) openApp()
                        return true
                    }
                    MotionEvent.ACTION_CANCEL -> {
                        detector.onCancel()
                        return true
                    }
                }
                return false
            }
        })

        try {
            windowManager?.addView(floatingView, params)
            isVisible = true

            
            bpmTextView = floatingView?.findViewById(R.id.bpm_text)
            
            // Set initial color
            updateBackgroundColor(Color.parseColor("#333333"))
            if (isShowBpm) {
                bpmTextView?.text = "??"
                bpmTextView?.visibility = View.VISIBLE
            } else {
                bpmTextView?.visibility = View.GONE
            }
        } catch (e: Exception) {
            isVisible = false
        }
    }

    /** Brings the app to the foreground (existing task if there is one, else a new one). */
    private fun openApp() {
        try {
            val intent = Intent(context, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            }
            context.startActivity(intent)
        } catch (e: Exception) {
            Log.w("FloatingWidget", "Could not open app from widget tap", e)
        }
    }

    fun hide() {
        if (!isVisible) return
        try {
            windowManager?.removeView(floatingView)
        } catch (e: Exception) {}
        isVisible = false
    }

    fun updateBpm(bpm: Int) {
        currentBpm = bpm
        handler.post {
            if (bpm > 0) {
                if (isShowBpm) {
                    bpmTextView?.text = bpm.toString()
                    bpmTextView?.visibility = View.VISIBLE
                } else {
                    bpmTextView?.visibility = View.GONE
                }
            } else {
                if (isShowBpm) {
                    bpmTextView?.text = "??"
                    bpmTextView?.visibility = View.VISIBLE
                } else {
                    bpmTextView?.visibility = View.GONE
                }
                updateBackgroundColor(Color.parseColor("#333333"))
            }
        }
    }

    fun setShowBpm(enabled: Boolean) {
        isShowBpm = enabled
        handler.post {
            if (enabled) {
                bpmTextView?.visibility = View.VISIBLE
                if (currentBpm > 0) {
                    bpmTextView?.text = currentBpm.toString()
                } else {
                    bpmTextView?.text = "??"
                }
            } else {
                bpmTextView?.visibility = View.GONE
            }
        }
    }
}
