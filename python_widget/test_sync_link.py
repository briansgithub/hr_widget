import unittest
import json
from unittest.mock import AsyncMock

from sync_link import ROLE_HOST, ROLE_MIRROR, ROLE_UNLINKED, SyncLink


def _mirroring_link():
    """A SyncLink that has just negotiated itself into MIRROR: linked to a peer
    that holds the Fitbit, with no relayed reading received yet."""
    link = SyncLink("desktop-pc", "token")
    link.peer_name = "Pixel 7a"
    link._fitbit_connected = False
    link._peer_fitbit_connected = True
    link._peer_connected_since = 1000.0
    link._renegotiate()
    return link


class MirrorStalenessTests(unittest.TestCase):
    """A freshly linked mirror has last_bpm_received_at == 0. Comparing that
    sentinel to time.time() makes it look decades stale, which sent the widget
    straight back to scanning and racing the peer for the Fitbit's one BLE
    slot instead of just showing the relayed heart rate."""

    def test_entering_mirror_stamps_the_grace_anchor(self):
        link = _mirroring_link()
        self.assertEqual(link.role, ROLE_MIRROR)
        self.assertGreater(link.mirror_since, 0)

    def test_fresh_mirror_is_not_stale_before_the_first_reading(self):
        link = _mirroring_link()
        self.assertEqual(link.last_bpm_received_at, 0)
        self.assertFalse(link.mirror_is_stale(now=link.mirror_since))
        self.assertFalse(link.mirror_is_stale(now=link.mirror_since + 29.0))

    def test_silent_mirror_goes_stale_after_the_timeout(self):
        link = _mirroring_link()
        self.assertTrue(link.mirror_is_stale(now=link.mirror_since + 31.0))

    def test_a_relayed_reading_restarts_the_window(self):
        link = _mirroring_link()
        link.last_bpm_received_at = link.mirror_since + 25.0
        self.assertFalse(link.mirror_is_stale(now=link.mirror_since + 50.0))
        self.assertTrue(link.mirror_is_stale(now=link.mirror_since + 60.0))

    def test_only_a_mirror_can_be_stale(self):
        link = _mirroring_link()
        anchor = link.mirror_since

        link._fitbit_connected = True
        link._connected_since = 1.0
        link._renegotiate()
        self.assertEqual(link.role, ROLE_HOST)
        self.assertEqual(link.mirror_since, 0)
        self.assertFalse(link.mirror_is_stale(now=anchor + 3600.0))

    def test_unlinking_clears_the_anchor(self):
        link = _mirroring_link()
        link._peer_fitbit_connected = False
        link._renegotiate()
        self.assertEqual(link.role, ROLE_UNLINKED)
        self.assertEqual(link.mirror_since, 0)

    def test_relinking_after_a_gap_restamps_the_anchor(self):
        link = _mirroring_link()
        first = link.mirror_since

        link._peer_fitbit_connected = False
        link._renegotiate()
        link._peer_fitbit_connected = True
        link._renegotiate()

        self.assertEqual(link.role, ROLE_MIRROR)
        self.assertGreaterEqual(link.mirror_since, first)
        self.assertFalse(link.mirror_is_stale(now=link.mirror_since))


class LinkDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_disconnect_preserves_local_fitbit_and_identifies_intent(self):
        link = SyncLink("desktop", "token")
        link._fitbit_connected = True
        link._connected_since = 1000.0
        link.peer_name = "phone"
        link.role = ROLE_HOST
        socket = AsyncMock()
        link._ws = socket

        await link.disconnect()

        self.assertEqual(json.loads(socket.send.call_args.args[0]), {
            "type": "bye", "reason": "user disconnected"
        })
        socket.close.assert_awaited_once()
        self.assertTrue(link._fitbit_connected)
        self.assertEqual(link._connected_since, 1000.0)
        self.assertEqual(link.role, ROLE_UNLINKED)
        self.assertIsNone(link._ws)


if __name__ == "__main__":
    unittest.main()
