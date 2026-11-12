"""Local-network sync link between this desktop widget and a companion device
(e.g. the Android app), so both can share one Fitbit's live heart rate.

The Fitbit only accepts one active BLE GATT subscriber at a time, so instead
of both apps racing to connect over Bluetooth, whichever app actually holds
the live connection ("HOST") relays BPM readings to the other ("MIRROR")
over the LAN. Peers are found via mDNS; the actual data channel is a small
JSON-over-WebSocket protocol. See sync_link's plan doc for the full design:
C:\\Users\\user1\\.claude\\plans\\is-it-possible-to-bubbly-patterson.md

Only one peer link is supported at a time (this is a pairwise phone<->laptop
feature, not a general fan-out).
"""
import asyncio
import json
import os
import secrets
import socket
import time

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo, AsyncServiceBrowser
from zeroconf import ServiceStateChange
import websockets
from websockets.exceptions import ConnectionClosed

SERVICE_TYPE = "_hrsync._tcp.local."
DEFAULT_PORT = 8765
LINK_SEARCH_TIMEOUT = 8.0
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_token.txt")

ROLE_UNLINKED = "UNLINKED"
ROLE_HOST = "HOST"
ROLE_MIRROR = "MIRROR"

# --- SYNC LINK PROTOCOL ---
def _dbg(msg):
    print(f"[sync_link] {msg}", flush=True)


def load_or_create_token(prompt_fn=None):
    """Returns the shared pairing token, generating one on first run.

    prompt_fn, if given, is called with the freshly-generated token so the
    caller can show it to the user (they'll need to enter the same token on
    the other device). Not called if a token already exists on disk.
    """
    try:
        with open(TOKEN_FILE, "r") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass

    token = secrets.token_hex(4)
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    if prompt_fn:
        prompt_fn(token)
    return token


def save_token(token):
    """Overwrite the stored pairing token (e.g. to match a code shown on the
    other device)."""
    token = token.strip()
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    return token


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class SyncLink:
    """Owns the mDNS advertisement/discovery and the single active WebSocket
    peer link. All public methods are safe to call from the asyncio loop
    thread; use asyncio.run_coroutine_threadsafe(...) to reach them from Tk.
    """

    def __init__(self, device_name, token, port=DEFAULT_PORT,
                 on_role_change=None, on_remote_bpm=None, on_status=None):
        self.device_name = device_name
        self.token = token
        self.port = port
        # on_role_change(role, peer_name)  -- role is ROLE_UNLINKED/HOST/MIRROR
        self.on_role_change = on_role_change or (lambda role, peer: None)
        # on_remote_bpm(bpm, phase) -- called with relayed readings while MIRROR
        self.on_remote_bpm = on_remote_bpm or (lambda bpm, phase: None)
        # on_status(text) -- human-readable status for the UI ("Linking...", "No device found", ...)
        self.on_status = on_status or (lambda text: None)

        self.role = ROLE_UNLINKED
        self.peer_name = None

        self._fitbit_connected = False
        self._connected_since = None
        self._peer_fitbit_connected = False
        self._peer_connected_since = None
        self.last_bpm_received_at = 0
        # When we last entered ROLE_MIRROR. Anchors the relay-staleness window
        # until the first relayed reading arrives; see mirror_is_stale().
        self.mirror_since = 0
        self.last_peer_activity_at = 0

        self._aiozc = None
        self._service_info = None
        self._server = None
        self._ws = None  # the single active peer connection (either accepted or dialed)
        self._link_lock = asyncio.Lock()
        self._link_task = None

    # --- lifecycle -----------------------------------------------------

    async def start(self):
        _dbg(f"start() device_name={self.device_name!r} port={self.port} token={self.token!r}")
        self._aiozc = AsyncZeroconf()
        try:
            self._server = await websockets.serve(self._handle_incoming, "0.0.0.0", self.port)
        except OSError as e:
            _dbg(f"Failed to bind port {self.port}: {e}. "
                 f"Is another copy of the widget already running?")
            raise
        _dbg(f"WebSocket server listening on 0.0.0.0:{self.port}")
        await self._register_mdns()

    async def stop(self):
        _dbg("stop() called")
        if self._ws is not None:
            await self._close_peer("shutting down")
        if self._server is not None:
            self._server.close()
        if self._aiozc is not None:
            if self._service_info is not None:
                await self._aiozc.async_unregister_service(self._service_info)
            await self._aiozc.async_close()
        _dbg("stop() complete")

    async def _register_mdns(self):
        ip = _local_ip()
        safe_name = "".join(c for c in self.device_name if c.isalnum() or c in "-_") or "hr-widget"
        instance = f"{safe_name}-{secrets.token_hex(2)}"
        self._service_info = ServiceInfo(
            SERVICE_TYPE,
            f"{instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties={},
            server=f"{instance}.local.",
        )
        await self._aiozc.async_register_service(self._service_info)
        _dbg(f"Registered mDNS service {self._service_info.name!r} at {ip}:{self.port}")

    # --- BLE-side hooks (called by hr_widget.py) ------------------------

    def notify_fitbit_state(self, connected):
        """Call whenever the local BLE connection to the Fitbit changes."""
        _dbg(f"notify_fitbit_state(connected={connected}) role={self.role}")
        self._fitbit_connected = connected
        self._connected_since = time.time() if connected else None
        if self._ws is not None:
            asyncio.ensure_future(self._send_status())
            # Our own state changed; the peer only learns of it once its
            # status message arrives, but we know enough right now to
            # re-negotiate our own role immediately.
            self._renegotiate()
        if not connected and self.role == ROLE_HOST:
            # Our own source dropped; let the mirror show staleness too.
            self.on_remote_bpm(0, None)

    def notify_bpm(self, bpm, phase=None):
        """Call with every BPM reading from the local BLE connection."""
        if self.role == ROLE_HOST and self._ws is not None:
            _dbg(f"relaying bpm={bpm} phase={phase} to {self.peer_name}")
            payload = {"type": "hr", "bpm": bpm, "ts": time.time()}
            if phase is not None:
                payload["phase"] = phase
            asyncio.ensure_future(self._send(self._ws, payload))

    def mirror_is_stale(self, timeout=30.0, now=None):
        """True when we are MIRRORing but the peer's relay has gone quiet.

        Before the first relayed reading arrives the window runs from the
        moment we became MIRROR, not from the epoch. Measuring against a bare
        ``last_bpm_received_at == 0`` makes a freshly-linked mirror look
        decades stale, which sends the widget straight back to scanning and
        racing the peer for the Fitbit's single BLE slot.
        """
        if self.role != ROLE_MIRROR:
            return False
        now = time.time() if now is None else now
        anchor = self.last_bpm_received_at or self.mirror_since or now
        return (now - anchor) > timeout

    # --- linking ---------------------------------------------------------

    def request_link(self):
        """Kick off a (re)link attempt. Safe to call repeatedly; a search
        already in progress is not duplicated."""
        if self._link_task is None or self._link_task.done():
            self._link_task = asyncio.ensure_future(self._do_request_link())
        return self._link_task

    async def disconnect(self):
        await self._close_peer("user disconnected")

    async def _do_request_link(self):
        async with self._link_lock:
            _dbg("request_link: browsing mDNS for peers...")
            self.on_status("Searching for device...")
            found = asyncio.get_event_loop().create_future()
            seen = set()

            def handler(zeroconf, service_type, name, state_change):
                _dbg(f"mDNS event: {state_change} name={name!r}")
                if name in seen or name == (self._service_info.name if self._service_info else None):
                    return
                if state_change is ServiceStateChange.Added and not found.done():
                    seen.add(name)
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda: not found.done() and found.set_result(name)
                    )

            browser = AsyncServiceBrowser(self._aiozc.zeroconf, SERVICE_TYPE, handlers=[handler])
            try:
                name = await asyncio.wait_for(found, timeout=LINK_SEARCH_TIMEOUT)
            except asyncio.TimeoutError:
                _dbg("request_link: timed out, no peer found")
                self.on_status("No device found on your network")
                return
            finally:
                await browser.async_cancel()

            _dbg(f"request_link: resolving {name!r}")
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            ok = await info.async_request(self._aiozc.zeroconf, 3000)
            if not ok or not info.parsed_addresses():
                _dbg(f"request_link: resolve failed ok={ok} addresses={info.parsed_addresses()}")
                self.on_status("Found device but couldn't resolve its address")
                return

            uri = f"ws://{info.parsed_addresses()[0]}:{info.port}"
            _dbg(f"request_link: connecting to {uri}")
            self.on_status(f"Connecting to {uri}...")
            try:
                ws = await asyncio.wait_for(websockets.connect(uri), timeout=5.0)
            except Exception as e:
                _dbg(f"request_link: connect failed: {e!r}")
                self.on_status(f"Couldn't connect: {e}")
                return

            await self._adopt_peer(ws, initiator=True)

    # --- WebSocket peer handling ------------------------------------------

    async def _handle_incoming(self, ws):
        _dbg(f"incoming connection from {ws.remote_address}")
        if self._ws is not None:
            # Already linked to someone; reject additional peers.
            _dbg(f"incoming connection rejected from {ws.remote_address}: already linked to {self.peer_name}")
            await ws.close(code=1013, reason="busy")
            return
        await self._adopt_peer(ws, initiator=False)
        # Keep the handler alive for the lifetime of the connection; the
        # server closes it automatically once this coroutine returns.
        try:
            while self._ws is ws:
                await asyncio.sleep(0.5)
        except ConnectionClosed:
            pass

    async def _adopt_peer(self, ws, initiator):
        _dbg(f"_adopt_peer(initiator={initiator}) sending hello: fitbit_connected={self._fitbit_connected} connected_since={self._connected_since}")
        self._ws = ws
        # Fresh link: don't let activity/bpm timestamps from a previous,
        # already-closed link make this new one look stale immediately.
        self.last_peer_activity_at = 0
        self.last_bpm_received_at = 0
        try:
            await self._send(ws, {
                "type": "hello",
                "device": self.device_name,
                "fitbit_connected": self._fitbit_connected,
                "connected_since": self._connected_since,
                "token": self.token,
            })
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            remote = json.loads(raw)
            _dbg(f"_adopt_peer: received hello: {remote}")
        except Exception as e:
            _dbg(f"_adopt_peer: handshake failed: {e!r}")
            self.on_status(f"Link handshake failed: {e}")
            await self._close_peer("handshake failed")
            return

        if remote.get("type") != "hello" or remote.get("token") != self.token:
            _dbg(f"_adopt_peer: token mismatch (got {remote.get('token')!r}, expected {self.token!r})")
            self.on_status("Link rejected: pairing token mismatch")
            await self._close_peer("token mismatch")
            return

        self.peer_name = remote.get("device", "peer")
        self._negotiate_role(remote)

        # Listen for further messages (hr/status/bye) for the life of the link.
        asyncio.ensure_future(self._recv_loop(ws))
        # Start heartbeat
        asyncio.ensure_future(self._heartbeat_loop(ws))

    async def _heartbeat_loop(self, ws):
        """Send a ping every 2s to keep the link alive and detect stale peers."""
        try:
            while self._ws is ws:
                await self._send(ws, {"type": "ping"})

                # Timeout check: 6 seconds of silence = peer lost
                if self.last_peer_activity_at > 0 and (time.time() - self.last_peer_activity_at > 6.0):
                    _dbg("Link timed out (6s silence); closing.")
                    await self._close_peer("timeout")
                    return

                await asyncio.sleep(2.0)
        except Exception:
            pass

    def _negotiate_role(self, remote):
        self._peer_fitbit_connected = bool(remote.get("fitbit_connected"))
        self._peer_connected_since = remote.get("connected_since")
        self._renegotiate()

    def _renegotiate(self):
        """Recompute our role from the last-known local + peer state. Safe to
        call whenever either side of that state changes, not just when a
        message arrives from the peer."""
        if self.peer_name is None:
            return

        mine_ts = self._connected_since or 0
        theirs_ts = self._peer_connected_since or 0
        remote_connected = self._peer_fitbit_connected

        if self._fitbit_connected and not remote_connected:
            new_role = ROLE_HOST
        elif remote_connected and not self._fitbit_connected:
            new_role = ROLE_MIRROR
        elif self._fitbit_connected and remote_connected:
            # Tie-break: earlier connection wins
            if mine_ts <= theirs_ts:
                new_role = ROLE_HOST
            else:
                new_role = ROLE_MIRROR
        else:
            new_role = ROLE_UNLINKED

        _dbg(f"_renegotiate: mine(conn={self._fitbit_connected}, ts={mine_ts}) "
             f"theirs(conn={remote_connected}, ts={theirs_ts}) -> {new_role}")
        if new_role == ROLE_MIRROR:
            if self.role != ROLE_MIRROR:
                self.mirror_since = time.time()
        else:
            self.mirror_since = 0
        self.role = new_role
        self.on_role_change(self.role, self.peer_name)
        if self.role == ROLE_MIRROR:
            self.on_status(f"Mirroring HR from {self.peer_name}")
        elif self.role == ROLE_HOST:
            self.on_status(f"Sharing HR with {self.peer_name}")
        else:
            self.on_status(f"Linked to {self.peer_name} (waiting for a Fitbit connection)")

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                self.last_peer_activity_at = time.time()

                mtype = msg.get("type")
                if mtype == "hr" and self.role == ROLE_MIRROR:
                    _dbg(f"_recv_loop: got relayed bpm={msg.get('bpm', 0)} phase={msg.get('phase')}")
                    self.last_bpm_received_at = time.time()
                    self.on_remote_bpm(msg.get("bpm", 0), msg.get("phase"))
                elif mtype == "ping":
                    await self._send(ws, {"type": "pong"})
                elif mtype == "pong":
                    pass # alive
                elif mtype == "status":
                    _dbg(f"_recv_loop: got status {msg}")
                    self._negotiate_role(msg)
                elif mtype == "bye":
                    _dbg("_recv_loop: got bye")
                    break
        except ConnectionClosed:
            _dbg("_recv_loop: connection closed")
        finally:
            if self._ws is ws:
                await self._close_peer("link lost")

    async def _send_status(self):
        if self._ws is not None:
            await self._send(self._ws, {
                "type": "status",
                "fitbit_connected": self._fitbit_connected,
                "connected_since": self._connected_since,
            })

    async def _send(self, ws, payload):
        try:
            await ws.send(json.dumps(payload))
        except ConnectionClosed:
            pass

    async def _close_peer(self, reason):
        _dbg(f"_close_peer(reason={reason!r})")
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await self._send(ws, {"type": "bye"})
                await ws.close()
            except Exception:
                pass
        self.role = ROLE_UNLINKED
        self.peer_name = None
        self.mirror_since = 0
        self.on_role_change(self.role, None)
        self.on_status(f"Not linked ({reason})" if reason else "Not linked")
