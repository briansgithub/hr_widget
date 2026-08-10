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
    private val onRemoteBpm: (bpm: Int) -> Unit
) {
    companion object {
        private const val TAG = "SyncLink"
        private const val SERVICE_TYPE = "_hrsync._tcp."
        private const val LINK_SEARCH_TIMEOUT_MS = 8_000L

        const val ROLE_UNLINKED = "UNLINKED"
        const val ROLE_HOST = "HOST"
        const val ROLE_MIRROR = "MIRROR"

        // TEMP DEBUG LOGGING (remove/disable after the test run)
        private const val DEBUG = true
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
    private var peerName: String? = null

    private var fitbitConnected = false
    private var connectedSince: Long? = null

    private var server: HrSyncServer? = null
    private var clientSocket: WebSocket? = null    // active outbound (we dialed the peer)
    private var serverPeerConn: JWebSocket? = null  // active inbound (peer dialed us)
    private var lastPingSentAt: Long = 0
    private val heartbeatRunnable = object : Runnable {
        override fun run() {
            if (clientSocket != null || serverPeerConn != null) {
                sendJson(JSONObject().apply { put("type", "ping") })
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

    fun start() {
        dbg("start() deviceName=$deviceName port=$port token=$token")
        try { multicastLock?.acquire() } catch (_: Exception) {}
        server = HrSyncServer(InetSocketAddress(port)).also { it.start() }
        dbg("WebSocket server listening on port $port")
        registerService()
    }

    fun stop() {
        dbg("stop() called")
        stopDiscoverySafely()
        onPeerLost("shutting down", closeSockets = true)
        unregisterService()
        try { server?.stop(1000) } catch (_: Exception) {}
        server = null
        try { if (multicastLock?.isHeld == true) multicastLock.release() } catch (_: Exception) {}
    }

    fun updateToken(newToken: String) {
        token = newToken
    }

    // ── BLE-side hooks (called by HeartRateService) ─────────────────────

    fun notifyFitbitState(connected: Boolean) {
        dbg("notifyFitbitState(connected=$connected) role=$role")
        fitbitConnected = connected
        connectedSince = if (connected) System.currentTimeMillis() / 1000 else null
        if (clientSocket != null || serverPeerConn != null) {
            sendJson(statusMessage())
        }
        if (!connected && role == ROLE_HOST) {
            handler.post { onRemoteBpm(0) }
        }
    }

    fun notifyBpm(bpm: Int) {
        if (role == ROLE_HOST && (clientSocket != null || serverPeerConn != null)) {
            dbg("relaying bpm=$bpm to $peerName")
            sendJson(JSONObject().apply {
                put("type", "hr")
                put("bpm", bpm)
                put("ts", System.currentTimeMillis())
            })
        }
    }

    // ── Linking ──────────────────────────────────────────────────────

    fun requestLink() {
        if (clientSocket != null || serverPeerConn != null) {
            onStatus("Already linked to $peerName")
            return
        }
        if (linking) return
        linking = true
        dbg("requestLink: browsing mDNS for peers...")
        onStatus("Searching for device...")

        var resolved = false
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(regType: String) {}
            override fun onServiceFound(service: NsdServiceInfo) {
                dbg("onServiceFound: ${service.serviceName}")
                if (resolved || service.serviceName == registeredServiceName) return
                nsdManager.resolveService(service, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(si: NsdServiceInfo, errorCode: Int) {
                        Log.w(TAG, "Resolve failed for ${si.serviceName}: $errorCode")
                    }
                    override fun onServiceResolved(si: NsdServiceInfo) {
                        if (resolved) return
                        val host = si.host?.hostAddress ?: return
                        dbg("onServiceResolved: ${si.serviceName} -> $host:${si.port}")
                        resolved = true
                        finishDiscovery()
                        connectClient(host, si.port)
                    }
                })
            }
            override fun onServiceLost(service: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                onStatus("Discovery failed ($errorCode)")
                finishDiscovery()
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }
        discoveryListener = listener
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            onStatus("Discovery failed to start: ${e.message}")
            linking = false
            discoveryListener = null
            return
        }

        val timeout = Runnable {
            if (!resolved) {
                onStatus("No device found on your network")
                finishDiscovery()
            }
        }
        linkTimeoutRunnable = timeout
        handler.postDelayed(timeout, LINK_SEARCH_TIMEOUT_MS)
    }

    fun connectManual(host: String) {
        if (clientSocket != null || serverPeerConn != null) {
            onStatus("Already linked to $peerName")
            return
        }
        finishDiscovery()
        connectClient(host, port)
    }

    fun disconnect() {
        try { clientSocket?.close(1000, "user disconnected") } catch (_: Exception) {}
        try { serverPeerConn?.close(1000, "user disconnected") } catch (_: Exception) {}
        onPeerLost("user disconnected", closeSockets = false)
    }

    private fun finishDiscovery() {
        linkTimeoutRunnable?.let { handler.removeCallbacks(it) }
        linkTimeoutRunnable = null
        stopDiscoverySafely()
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
                val hello = helloMessage().toString()
                dbg("client onOpen: sending $hello")
                webSocket.send(hello)
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                handleIncomingMessage(text)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (clientSocket === webSocket) {
                    clientSocket = null
                    onPeerLost("link lost", closeSockets = false)
                }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val ip = webSocket.request().url.host
                val port = webSocket.request().url.port
                val errorMsg = "Couldn't connect to $ip:$port: ${t.message}"
                onStatus(errorMsg)
                dbg(errorMsg)
                if (clientSocket === webSocket) {
                    clientSocket = null
                    onPeerLost("connect failed", closeSockets = false)
                }
            }
        })
        clientSocket = socket
    }

    // ── Inbound (server) connection ─────────────────────────────────────

    private inner class HrSyncServer(address: InetSocketAddress) : WebSocketServer(address) {
        override fun onOpen(conn: JWebSocket, handshake: ClientHandshake) {
            dbg("server onOpen: incoming connection from ${conn.remoteSocketAddress}")
            if (clientSocket != null || serverPeerConn != null) {
                dbg("server onOpen: rejecting, already linked")
                conn.close(1013, "busy")
                return
            }
            serverPeerConn = conn
            val hello = helloMessage().toString()
            dbg("server onOpen: sending $hello")
            conn.send(hello)
        }

        override fun onMessage(conn: JWebSocket, message: String) {
            dbg("server onMessage: $message")
            if (serverPeerConn === conn) handleIncomingMessage(message)
        }

        override fun onClose(conn: JWebSocket, code: Int, reason: String, remote: Boolean) {
            if (serverPeerConn === conn) {
                serverPeerConn = null
                onPeerLost("link lost", closeSockets = false)
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

    private fun handleIncomingMessage(raw: String) {
        val msg = try { JSONObject(raw) } catch (e: Exception) { return }
        dbg("handleIncomingMessage: type=${msg.optString("type")}")
        when (msg.optString("type")) {
            "hello" -> {
                if (msg.optString("token") != token) {
                    onStatus("Link rejected: pairing token mismatch")
                    onPeerLost("token mismatch", closeSockets = true)
                    return
                }
                peerName = msg.optString("device", "peer")
                negotiateRole(msg)
                handler.post(heartbeatRunnable)
            }
            "status" -> negotiateRole(msg)
            "ping" -> sendJson(JSONObject().apply { put("type", "pong") })
            "pong" -> { /* Link is alive */ }
            "hr" -> if (role == ROLE_MIRROR) {
                val bpm = msg.optInt("bpm", 0)
                handler.post { onRemoteBpm(bpm) }
            }
            "bye" -> onPeerLost("peer disconnected", closeSockets = false)
        }
    }

    private fun negotiateRole(msg: JSONObject) {
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
        handler.post { onRoleChange(role, peerName) }
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
        if (closeSockets) {
            try { clientSocket?.close(1000, reason) } catch (_: Exception) {}
            try { serverPeerConn?.close(1000, reason) } catch (_: Exception) {}
        }
        clientSocket = null
        serverPeerConn = null
        role = ROLE_UNLINKED
        peerName = null
        handler.post {
            onRoleChange(ROLE_UNLINKED, null)
            onStatus("Not linked ($reason)")
        }
    }
}
