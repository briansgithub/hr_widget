package com.hrbot.android

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.java_websocket.WebSocket as JWebSocket
import org.java_websocket.handshake.ClientHandshake
import org.java_websocket.server.WebSocketServer
import org.json.JSONObject
import java.net.InetSocketAddress
import java.util.concurrent.TimeUnit

/**
 * Local-network sync link with a companion device (the desktop widget), so
 * both can share one Fitbit's live heart rate. The Fitbit only accepts one
 * active BLE GATT subscriber at a time, so whichever app actually holds the
 * live connection ("HOST") relays BPM readings to the other ("MIRROR") over
 * the LAN. Peers are found via mDNS (NsdManager); the data channel is a
 * small JSON-over-WebSocket protocol shared with sync_link.py on desktop.
 *
 * Only one peer link is supported at a time.
 */
class SyncLinkManager(
    private val context: Context,
    private val deviceName: String,
    private var token: String,
    private val port: Int = 8765,
    private val onRoleChange: (role: String, peerName: String?) -> Unit,
    private val onStatus: (text: String) -> Unit,
    private val onRemoteBpm: (bpm: Int, phase: Float?) -> Unit
) {
    companion object {
        private const val TAG = "SyncLink"
        private const val SERVICE_TYPE = "_hrsync._tcp."
        private const val LINK_SEARCH_TIMEOUT_MS = 8_000L

        const val ROLE_UNLINKED = "UNLINKED"
        const val ROLE_HOST = "HOST"
        const val ROLE_MIRROR = "MIRROR"

        // Verbose per-message link logging. Off by default: this fires on every
        // ping/pong/hr frame, which is pure battery/IO burn in normal use.
        private const val DEBUG = false
        private fun dbg(msg: String) {
            if (DEBUG) Log.d(TAG, msg)
        }
    }

    private val handler = Handler(Looper.getMainLooper())
    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
    private val multicastLock: WifiManager.MulticastLock? =
        wifiManager?.createMulticastLock("hr_sync_mdns")?.apply { setReferenceCounted(true) }
    private val httpClient = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // infinite, for WebSocket
        .build()

    var role: String = ROLE_UNLINKED
        private set
    val isLinked: Boolean get() = authenticated
    val linkedPeerName: String? get() = peerName
    private var peerName: String? = null

    private var fitbitConnected = false
    private var connectedSince: Long? = null
    private var remoteState: JSONObject? = null
    private var authenticated = false
    private val prefs = context.getSharedPreferences("hr_bot_prefs", Context.MODE_PRIVATE)
    private var reconnectEnabled = prefs.getBoolean("sync_link_requested", false)
    private var stopped = false
    private val reconnectRunnable = Runnable { requestLink() }

    private fun setReconnectEnabled(enabled: Boolean) {
        reconnectEnabled = enabled
        prefs.edit().putBoolean("sync_link_requested", enabled).apply()
        handler.removeCallbacks(reconnectRunnable)
    }

    private fun scheduleReconnect() {
        handler.removeCallbacks(reconnectRunnable)
        if (reconnectEnabled && !stopped) handler.postDelayed(reconnectRunnable, 2000)
    }

    private var server: HrSyncServer? = null
    private var clientSocket: WebSocket? = null    // active outbound (we dialed the peer)
    private var serverPeerConn: JWebSocket? = null  // active inbound (peer dialed us)
    private var lastPeerActivityAt: Long = 0
    private val heartbeatRunnable = object : Runnable {
        override fun run() {
            if (clientSocket != null || serverPeerConn != null) {
                sendJson(JSONObject().apply { put("type", "ping") })
                
                // Timeout check: 6 seconds of silence = peer lost
                if (lastPeerActivityAt > 0 && System.currentTimeMillis() - lastPeerActivityAt > 6000) {
                    dbg("Link timed out (6s silence); closing.")
                    onPeerLost("timeout", closeSockets = true)
                    return
                }
                
                handler.postDelayed(this, 2000)
            }
        }
    }

    private var registrationListener: NsdManager.RegistrationListener? = null
    private var registeredServiceName: String? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var linkTimeoutRunnable: Runnable? = null
    private var linking = false

    // ── Lifecycle ────────────────────────────────────────────────────

    /** Holding a MulticastLock disables Wi-Fi power save, so take it only while
     * actually browsing for peers -- not for the whole service lifetime. Our own
     * mDNS advertisement is served by the system daemon and needs no app lock. */
    private fun acquireMulticast() {
        try { if (multicastLock?.isHeld != true) multicastLock?.acquire() } catch (_: Exception) {}
    }

    private fun releaseMulticast() {
        try { if (multicastLock?.isHeld == true) multicastLock?.release() } catch (_: Exception) {}
    }

    fun start() {
        stopped = false
        dbg("start() deviceName=$deviceName port=$port")
        server = HrSyncServer(InetSocketAddress(port)).also { it.start() }
        dbg("WebSocket server listening on port $port")
        registerService()
        scheduleReconnect()
    }

    fun stop() {
        dbg("stop() called")
        stopped = true
        handler.removeCallbacks(reconnectRunnable)
        finishDiscovery()
        onPeerLost("shutting down", closeSockets = true)
        unregisterService()
        try { server?.stop(1000) } catch (_: Exception) {}
        server = null
        releaseMulticast()
    }

    fun updateToken(newToken: String) {
        token = newToken
    }

    // ── BLE-side hooks (called by HeartRateService) ─────────────────────

    fun notifyFitbitState(connected: Boolean) {
        dbg("notifyFitbitState(connected=$connected) role=$role")
        fitbitConnected = connected
        connectedSince = if (connected) connectedSince ?: (System.currentTimeMillis() / 1000) else null
        if (clientSocket != null || serverPeerConn != null) {
            sendJson(statusMessage())
        }
        remoteState?.let { negotiateRole(it) }
        if (!connected && role == ROLE_HOST) {
            handler.post { onRemoteBpm(0, null) }
        }
    }

    fun notifyBpm(bpm: Int, phase: Float? = null) {
        if (role == ROLE_HOST && (clientSocket != null || serverPeerConn != null)) {
            dbg("relaying bpm=$bpm phase=$phase to $peerName")
            sendJson(JSONObject().apply {
                put("type", "hr")
                put("bpm", bpm)
                if (phase != null) put("phase", phase)
                put("ts", System.currentTimeMillis())
            })
        }
    }

    // ── Linking ──────────────────────────────────────────────────────

    fun requestLink() {
        if (stopped) return
        setReconnectEnabled(true)
        if (clientSocket != null || serverPeerConn != null) {
            onStatus("Already linked to $peerName")
            return
        }
        if (linking) return
        linking = true
        acquireMulticast()
        dbg("requestLink: browsing mDNS for peers...")
        onStatus("Searching for device...")

        var resolved = false
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(regType: String) {}
            override fun onServiceFound(service: NsdServiceInfo) {
                val discovery = this
                handler.post found@{
                    if (resolved || discoveryListener !== discovery ||
                        service.serviceName == registeredServiceName) return@found
                    nsdManager.resolveService(service, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(si: NsdServiceInfo, errorCode: Int) {
                        Log.w(TAG, "Resolve failed for ${si.serviceName}: $errorCode")
                    }
                    override fun onServiceResolved(si: NsdServiceInfo) {
                        handler.post resolved@{
                            if (resolved || discoveryListener !== discovery || stopped) return@resolved
                            val host = si.host?.hostAddress ?: return@resolved
                            resolved = true
                            finishDiscovery()
                            connectClient(host, si.port)
                        }
                    }
                    })
                }
            }
            override fun onServiceLost(service: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                val discovery = this
                handler.post {
                    if (discoveryListener === discovery) {
                        onStatus("Discovery failed ($errorCode)")
                        finishDiscovery()
                        scheduleReconnect()
                    }
                }
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }
        discoveryListener = listener
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            onStatus("Discovery failed to start: ${e.message}")
            finishDiscovery()
            scheduleReconnect()
            return
        }

        val timeout = Runnable {
            if (!resolved) {
                onStatus("No device found on your network")
                finishDiscovery()
                scheduleReconnect()
            }
        }
        linkTimeoutRunnable = timeout
        handler.postDelayed(timeout, LINK_SEARCH_TIMEOUT_MS)
    }

    fun connectManual(host: String) {
        setReconnectEnabled(true)
        if (clientSocket != null || serverPeerConn != null) {
            onStatus("Already linked to $peerName")
            return
        }
        finishDiscovery()
        connectClient(host, port)
    }

    fun disconnect() {
        setReconnectEnabled(false)
        finishDiscovery()
        sendJson(JSONObject().apply { put("type", "bye"); put("reason", "user disconnected") })
        onPeerLost("user disconnected", closeSockets = true)
    }

    private fun finishDiscovery() {
        linkTimeoutRunnable?.let { handler.removeCallbacks(it) }
        linkTimeoutRunnable = null
        stopDiscoverySafely()
        releaseMulticast()
        linking = false
    }

    private fun stopDiscoverySafely() {
        discoveryListener?.let {
            try { nsdManager.stopServiceDiscovery(it) } catch (_: Exception) {}
        }
        discoveryListener = null
    }

    // ── mDNS advertisement ───────────────────────────────────────────

    private fun registerService() {
        val info = NsdServiceInfo().apply {
            serviceName = "${sanitize(deviceName)}-${(1000..9999).random()}"
            serviceType = SERVICE_TYPE
            port = this@SyncLinkManager.port
        }
        val listener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(si: NsdServiceInfo) {
                registeredServiceName = si.serviceName
            }
            override fun onRegistrationFailed(si: NsdServiceInfo, errorCode: Int) {
                Log.e(TAG, "mDNS registration failed: $errorCode")
            }
            override fun onServiceUnregistered(si: NsdServiceInfo) {}
            override fun onUnregistrationFailed(si: NsdServiceInfo, errorCode: Int) {}
        }
        registrationListener = listener
        try {
            nsdManager.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to register mDNS service", e)
        }
    }

    private fun unregisterService() {
        registrationListener?.let { try { nsdManager.unregisterService(it) } catch (_: Exception) {} }
        registrationListener = null
    }

    private fun sanitize(name: String) =
        name.filter { it.isLetterOrDigit() || it == '-' || it == '_' }.ifEmpty { "hr-bot" }

    // ── Outbound (client) connection ────────────────────────────────────

    private fun connectClient(host: String, remotePort: Int) {
        dbg("connectClient: dialing ws://$host:$remotePort")
        onStatus("Connecting to $host:$remotePort...")
        val request = Request.Builder().url("ws://$host:$remotePort").build()
        val socket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                handler.post {
                    if (clientSocket !== webSocket) return@post
                    webSocket.send(helloMessage().toString())
                    startPeerHeartbeat()
                }
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                handler.post {
                    if (clientSocket === webSocket) handleIncomingMessage(text)
                }
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                handler.post {
                    if (clientSocket === webSocket) {
                        if (reason == "user disconnected") setReconnectEnabled(false)
                        onPeerLost("link lost", closeSockets = false)
                    }
                }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                handler.post {
                    if (clientSocket === webSocket) {
                        onPeerLost("connect failed", closeSockets = false)
                    }
                }
            }
        })
        clientSocket = socket
    }

    // ── Inbound (server) connection ─────────────────────────────────────

    private inner class HrSyncServer(address: InetSocketAddress) : WebSocketServer(address) {
        override fun onOpen(conn: JWebSocket, handshake: ClientHandshake) {
            handler.post {
                if (stopped || clientSocket != null || serverPeerConn != null) {
                    conn.close(1013, "busy")
                    return@post
                }
                serverPeerConn = conn
                conn.send(helloMessage().toString())
                startPeerHeartbeat()
            }
        }

        override fun onMessage(conn: JWebSocket, message: String) {
            handler.post {
                if (serverPeerConn === conn) handleIncomingMessage(message)
            }
        }

        override fun onClose(conn: JWebSocket, code: Int, reason: String, remote: Boolean) {
            handler.post {
                if (serverPeerConn === conn) {
                    if (reason == "user disconnected") setReconnectEnabled(false)
                    onPeerLost("link lost", closeSockets = false)
                }
            }
        }

        override fun onError(conn: JWebSocket?, ex: Exception) {
            Log.e(TAG, "Sync server error", ex)
        }

        override fun onStart() {}
    }

    // ── Shared protocol handling ─────────────────────────────────────

    private fun helloMessage() = JSONObject().apply {
        put("type", "hello")
        put("device", deviceName)
        put("fitbit_connected", fitbitConnected)
        put("connected_since", connectedSince ?: JSONObject.NULL)
        put("token", token)
    }

    private fun statusMessage() = JSONObject().apply {
        put("type", "status")
        put("fitbit_connected", fitbitConnected)
        put("connected_since", connectedSince ?: JSONObject.NULL)
    }

    private fun sendJson(json: JSONObject) {
        val text = json.toString()
        clientSocket?.send(text)
        serverPeerConn?.let { if (it.isOpen) it.send(text) }
    }

    private fun startPeerHeartbeat() {
        lastPeerActivityAt = System.currentTimeMillis()
        handler.removeCallbacks(heartbeatRunnable)
        handler.postDelayed(heartbeatRunnable, 2000)
    }

    private fun handleIncomingMessage(raw: String) {
        val msg = try { JSONObject(raw) } catch (e: Exception) { return }
        if (!authenticated && msg.optString("type") != "hello") return
        
        lastPeerActivityAt = System.currentTimeMillis()
        
        dbg("handleIncomingMessage: type=${msg.optString("type")}")
        when (msg.optString("type")) {
            "hello" -> {
                if (msg.optString("token") != token) {
                    setReconnectEnabled(false)
                    onStatus("Link rejected: pairing token mismatch")
                    onPeerLost("token mismatch", closeSockets = true)
                    return
                }
                peerName = msg.optString("device", "peer")
                authenticated = true
                setReconnectEnabled(true)
                finishDiscovery()
                negotiateRole(msg)
            }
            "status" -> negotiateRole(msg)
            "ping" -> sendJson(JSONObject().apply { put("type", "pong") })
            "pong" -> { /* Link is alive */ }
            "hr" -> if (role == ROLE_MIRROR) {
                val bpm = msg.optInt("bpm", 0)
                val phase = if (msg.has("phase")) msg.optDouble("phase").toFloat() else null
                handler.post { onRemoteBpm(bpm, phase) }
            }
            "bye" -> {
                if (msg.optString("reason") == "user disconnected") setReconnectEnabled(false)
                onPeerLost("peer disconnected", closeSockets = true)
            }
        }
    }

    private fun negotiateRole(msg: JSONObject) {
        remoteState = msg
        val remoteConnected = msg.optBoolean("fitbit_connected", false)
        val remoteSince = if (msg.isNull("connected_since")) null else msg.optLong("connected_since")

        role = when {
            fitbitConnected && !remoteConnected -> ROLE_HOST
            remoteConnected && !fitbitConnected -> ROLE_MIRROR
            fitbitConnected && remoteConnected -> {
                val mine = connectedSince ?: Long.MAX_VALUE
                val theirs = remoteSince ?: Long.MAX_VALUE
                if (mine <= theirs) ROLE_HOST else ROLE_MIRROR
            }
            else -> ROLE_UNLINKED
        }

        dbg("negotiateRole: mine(connected=$fitbitConnected, since=$connectedSince) " +
            "remote(connected=$remoteConnected, since=$remoteSince) -> $role")
        val negotiatedRole = role
        val negotiatedPeer = peerName
        handler.post { onRoleChange(negotiatedRole, negotiatedPeer) }
        onStatus(
            when (role) {
                ROLE_HOST -> "Sharing HR with $peerName"
                ROLE_MIRROR -> "Mirroring HR from $peerName"
                else -> "Linked to $peerName (waiting for a Fitbit connection)"
            }
        )
    }

    private fun onPeerLost(reason: String, closeSockets: Boolean) {
        dbg("onPeerLost(reason=$reason, closeSockets=$closeSockets)")
        handler.removeCallbacks(heartbeatRunnable)
        authenticated = false
        remoteState = null
        if (closeSockets) {
            try { clientSocket?.close(1000, reason) } catch (_: Exception) {}
            try { serverPeerConn?.close(1000, reason) } catch (_: Exception) {}
        }
        clientSocket = null
        serverPeerConn = null
        lastPeerActivityAt = 0
        role = ROLE_UNLINKED
        peerName = null
        handler.post {
            onRoleChange(ROLE_UNLINKED, null)
            onStatus("Not linked ($reason)")
        }
        scheduleReconnect()
    }
}
