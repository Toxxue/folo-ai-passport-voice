#!/usr/bin/env python3
"""Pure dispatch tests: validate key mappings without injecting into the desktop."""

import argparse
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
import hotkey_forwarder as h  # noqa: E402


class RecordingBackend:
    """Key backend that records calls instead of touching a real injection API.

    Keeps the dispatch tests free of side effects: without this stub the
    forwarder would initialise the Interception driver (when installed) or
    call SendInput, so a unit test could reach the real desktop.
    """

    def __init__(self):
        self.calls = []

    def key_down(self, keys):
        self.calls.append(("down", tuple(keys)))

    def key_up(self, keys):
        self.calls.append(("up", tuple(keys)))

    def key_click(self, keys, hold_seconds=0.06):
        self.calls.append(("down", tuple(keys)))
        self.calls.append(("up", tuple(keys)))

    def close(self):
        pass


class ForwarderDispatchTest(unittest.TestCase):
    def _forwarder(self, key="right-shift", mode="tap"):
        args = argparse.Namespace(
            key=key,
            mode=mode,
            no_audio=True,
            output_substr="CABLE Input",
            key_backend="sendinput",
        )
        backend = RecordingBackend()
        with mock.patch.object(h, "create_key_backend", return_value=backend):
            forwarder = h.Forwarder(args)
        forwarder.test_backend = backend
        return forwarder

    def setUp(self):
        self.forwarder = self._forwarder()

    @staticmethod
    def event(name, **fields):
        return (json.dumps({"event": name, **fields}) + "\n").encode("utf-8")

    def test_up_ptt_hold_mode_holds_key_and_releases_on_end(self):
        fwd = self._forwarder(mode="hold")
        with mock.patch.object(h.time, "sleep"):
            fwd.on_event(self.event("voice.start"))
            fwd.on_event(self.event("voice.end"))

        calls = fwd.test_backend.calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "down")
        self.assertEqual(calls[0][1][0][1:], h.KEY_MAP["right-shift"])
        self.assertEqual(calls[1][0], "up")
        self.assertEqual(calls[1][1][0][1:], h.KEY_MAP["right-shift"])

    def test_up_ptt_clicks_right_shift_at_start_and_again_at_release(self):
        with mock.patch.object(h.time, "sleep"):
            self.forwarder.on_event(self.event("voice.start"))
            self.forwarder.on_event(self.event("voice.end"))

        calls = self.forwarder.test_backend.calls
        # Two clicks: down+up when the microphone opens, down+up when it closes.
        self.assertEqual([c[0] for c in calls], ["down", "up", "down", "up"])
        for call in calls:
            self.assertEqual(call[1][0][1:], h.KEY_MAP["right-shift"])

    def test_down_enter_releases_stale_voice_then_injects_once(self):
        order = []
        self.forwarder.active = True
        with mock.patch.object(self.forwarder, "finish",
                               side_effect=lambda **_kw: order.append("release")), \
             mock.patch.object(self.forwarder.enter_injector, "click",
                               side_effect=lambda: order.append("enter")), \
             mock.patch.object(h.time, "monotonic", side_effect=[10.0, 10.1]), \
             mock.patch.object(h.time, "sleep"):
            self.forwarder.on_event(self.event("key.action", action="enter"))
            self.forwarder.on_event(self.event("key.action", action="enter"))

        self.assertEqual(order, ["release", "enter"])

    def test_down_enter_works_after_normal_voice_end(self):
        order = []
        with mock.patch.object(self.forwarder, "finish",
                               side_effect=lambda **_kw: order.append("reconcile")), \
             mock.patch.object(self.forwarder.enter_injector, "click",
                               side_effect=lambda: order.append("enter")), \
             mock.patch.object(h.time, "monotonic", return_value=20.0):
            self.forwarder.on_event(self.event("key.action", action="enter"))
        self.assertEqual(order, ["reconcile", "enter"])

    def test_retired_clear_and_agent_actions_never_inject_keys(self):
        with mock.patch.object(self.forwarder.enter_injector, "click") as click:
            self.forwarder.on_event(self.event("key.action", action="clear"))
            self.forwarder.on_event(self.event("agent.action", decision="approve"))
        click.assert_not_called()

    def test_audio_route_controls_readiness_and_start(self):
        fwd = self.forwarder
        self.assertFalse(fwd.pc_status()["audioReady"])
        fwd.sink = mock.Mock(enabled=False)
        with mock.patch.object(fwd.injector, "start") as start:
            fwd.on_event(self.event("voice.start"))
            start.assert_not_called()
        fwd.sink.enabled = True
        self.assertTrue(fwd.pc_status()["audioReady"])

    def test_drain_precedes_release_and_late_audio_is_ignored(self):
        fwd = self.forwarder
        fwd.sink = mock.Mock(enabled=True)
        order = []
        with mock.patch.object(fwd.injector, "start"), mock.patch.object(fwd.injector, "stop", side_effect=lambda: order.append('release')):
            fwd.sink.drain.side_effect = lambda: order.append('drain')
            fwd.on_event(self.event("voice.start"))
            fwd.on_audio(bytes([255,128])+bytes(804))
            fwd.on_audio(bytes([0,128])+bytes(804))
            fwd.on_audio(bytes([0,128])+bytes(804))  # Duplicate
            fwd.on_audio(bytes([255,128])+bytes(804))  # Late
            fwd.on_event(self.event("voice.end"))
            fwd.on_audio(bytes([1,128])+bytes(804))
        self.assertEqual(fwd.audio_blocks,2)
        self.assertEqual(order,['drain','release'])

    def test_lost_end_or_audio_failure_releases_even_with_live_transport(self):
        fwd = self.forwarder
        with mock.patch.object(fwd.injector,"start"), mock.patch.object(fwd.injector,"stop") as stop:
            fwd.on_event(self.event("voice.start"))
            fwd.check_health(fwd.last_audio_at + 3)
            self.assertFalse(fwd.active)
            stop.assert_called_once()
            fwd.sink = mock.Mock(enabled=True)
            fwd.on_event(self.event("voice.start"))
            fwd.sink.enabled = False
            fwd.check_health()
            self.assertFalse(fwd.active)
            self.assertEqual(stop.call_count,2)

    def test_malformed_events_ignored(self):
        for payload in (b'null', b'[]', b'garbage', b'"voice.start"'):
            self.forwarder.on_event(payload)
        self.assertFalse(self.forwarder.active)

    def test_no_audio_mode_discards_device_audio(self):
        self.forwarder.on_audio(b"\x01\x80payload")

    def test_disconnect_without_active_session_does_not_inject_shift(self):
        with mock.patch.object(h.time, "sleep"):
            self.forwarder.on_disconnect()
        self.assertEqual(self.forwarder.test_backend.calls, [])


if __name__ == "__main__":
    unittest.main()
