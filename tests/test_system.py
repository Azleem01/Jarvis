"""System control: the take-mute race, and the power confirmation.

Two things here are worth a test rather than a read-through.

**The volume race.** ``speaker_mute`` mutes the speakers while the mic is open
and restores them on release — on a daemon thread. So "mute" can arrive while a
restore is still in flight, and a restore landing after the tool would silently
undo the very command the user just gave. The fake endpoint below lets the final
device state be asserted in each interleaving, which is the only way to see it:
on real hardware the restore almost always wins the race and the bug hides.

**The power confirmation.** ``power_action`` must never act on a first request.
A misheard "shut down" that is obeyed is unrecoverable, so the test asserts what
did *not* happen as much as what did.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speaker_mute  # noqa: E402
from tools import system  # noqa: E402


class FakeEndpoint:
    """A pycaw endpoint that records what was asked of it.

    Modelled on the real one's quirks: ``GetMute`` reflects the last write, and
    the scalar is clamped to 0..1 exactly as the COM interface does.
    """

    def __init__(self, muted=0, level=0.5):
        self._muted = muted
        self._level = level
        self.calls = []

    def GetMute(self):
        return self._muted

    def SetMute(self, value, _ctx):
        self._muted = int(bool(value))
        self.calls.append(("SetMute", self._muted))

    def GetMasterVolumeLevelScalar(self):
        return self._level

    def SetMasterVolumeLevelScalar(self, value, _ctx):
        self._level = max(0.0, min(1.0, float(value)))
        self.calls.append(("SetVolume", self._level))


class VolumeTestCase(unittest.TestCase):
    def setUp(self):
        self.endpoint = FakeEndpoint()
        # Both modules resolve through speaker_mute's cache, so patching the
        # cache patches both — which is the coupling under test.
        patcher = mock.patch.object(speaker_mute, "_cached", self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)
        speaker_mute._prior_state = None
        self.addCleanup(setattr, speaker_mute, "_prior_state", None)


class TestVolume(VolumeTestCase):
    def test_set_reads_the_level_back_from_the_device(self):
        reply = system.control_volume("set", 30)
        self.assertIn("30%", reply)
        self.assertAlmostEqual(self.endpoint.GetMasterVolumeLevelScalar(), 0.30)

    def test_set_rejects_an_out_of_range_level(self):
        """A mistranscribed number must not be clamped into a plausible one."""
        for level in (-5, 300, 900):
            reply = system.control_volume("set", level)
            self.assertIn("between 0 and 100", reply)
        self.assertEqual(self.endpoint.calls, [])

    def test_up_and_down_step_from_the_current_level(self):
        system.control_volume("up")
        self.assertAlmostEqual(self.endpoint.GetMasterVolumeLevelScalar(), 0.60)
        system.control_volume("down")
        self.assertAlmostEqual(self.endpoint.GetMasterVolumeLevelScalar(), 0.50)

    def test_stepping_does_not_run_past_the_ends(self):
        self.endpoint._level = 0.95
        system.control_volume("up")
        self.assertAlmostEqual(self.endpoint.GetMasterVolumeLevelScalar(), 1.0)
        self.endpoint._level = 0.05
        system.control_volume("down")
        self.assertAlmostEqual(self.endpoint.GetMasterVolumeLevelScalar(), 0.0)

    def test_changing_the_volume_unmutes(self):
        """Turning it up on muted speakers is silence at a new level."""
        self.endpoint._muted = 1
        system.control_volume("up")
        self.assertEqual(self.endpoint.GetMute(), 0)

    def test_mute_and_unmute(self):
        system.control_volume("mute")
        self.assertEqual(self.endpoint.GetMute(), 1)
        system.control_volume("unmute")
        self.assertEqual(self.endpoint.GetMute(), 0)

    def test_an_unknown_verb_changes_nothing(self):
        reply = system.control_volume("frobnicate")
        self.assertIn("don't know how", reply)
        self.assertEqual(self.endpoint.calls, [])

    def test_a_dead_endpoint_is_reported_not_raised(self):
        with mock.patch.object(speaker_mute, "endpoint", return_value=None):
            reply = system.control_volume("mute")
        self.assertIn("couldn't reach the speakers", reply)


class TestTakeMuteRace(VolumeTestCase):
    """The interleavings of the take-mute restore and a user volume command.

    All three must end with the user's command winning. This is the test that
    would have caught "say mute, speakers unmute themselves half a second
    later" — which on real hardware reproduces roughly never.
    """

    def test_mute_survives_a_restore_that_has_not_run_yet(self):
        # The take muted speakers that were unmuted; the restore is pending.
        speaker_mute.mute()
        self.assertEqual(self.endpoint.GetMute(), 1)

        reply = system.control_volume("mute")

        self.assertIn("muted", reply.lower())
        self.assertEqual(self.endpoint.GetMute(), 1)
        # The pending restore is now spent, so nothing can undo the command.
        speaker_mute.restore()
        self.assertEqual(
            self.endpoint.GetMute(), 1,
            "a late take-restore undid the user's own mute",
        )

    def test_a_volume_change_does_not_leave_the_take_mute_in_place(self):
        """The reason 'just forget the prior state' would have been wrong.

        Clearing speaker_mute's memory instead of running it would leave the
        speakers muted from the take while reporting a new volume — audible
        silence at 30%.
        """
        speaker_mute.mute()
        system.control_volume("set", 30)
        self.assertEqual(
            self.endpoint.GetMute(), 0,
            "volume was set but the take-mute was never lifted",
        )

    def test_restore_already_done_is_a_no_op(self):
        speaker_mute.mute()
        speaker_mute.restore()
        system.control_volume("mute")
        self.assertEqual(self.endpoint.GetMute(), 1)

    def test_a_take_that_never_muted_leaves_a_user_mute_alone(self):
        """The user muted the speakers themselves before ever speaking."""
        self.endpoint._muted = 1
        speaker_mute.mute()      # records "already muted"
        system.control_volume("unmute")
        speaker_mute.restore()   # must not re-assert the old mute
        self.assertEqual(self.endpoint.GetMute(), 0)


class TestPowerConfirmation(unittest.TestCase):
    def setUp(self):
        system._reset_for_tests()
        self.addCleanup(system._reset_for_tests)
        # Nothing in this class may ever reach a real power command.
        patcher = mock.patch.object(system, "_run_power")
        self.run_power = patcher.start()
        self.run_power.return_value = "Shutting down."
        self.addCleanup(patcher.stop)

    def test_a_first_request_arms_and_does_not_act(self):
        for action in ("shutdown", "restart", "sleep"):
            system._reset_for_tests()
            reply = system.power_action(action)
            self.assertIn(f"confirm {action}", reply.lower())
            self.run_power.assert_not_called()

    def test_confirming_an_armed_action_runs_it(self):
        system.power_action("shutdown")
        reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_called_once_with("shutdown")
        self.assertEqual(reply, "Shutting down.")

    def test_confirming_nothing_does_nothing(self):
        """A stray 'confirm shutdown' out of the blue must be refused.

        This is what makes the fast-path rule safe: matching the phrase locally
        cannot cause a shutdown, because the tool has nothing armed.
        """
        reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_not_called()
        self.assertIn("nothing was waiting", reply.lower())

    def test_an_expired_arm_is_refused(self):
        system.power_action("shutdown")
        with mock.patch.object(
            system.time, "monotonic",
            return_value=time.monotonic() + system._CONFIRM_WINDOW_S + 1,
        ):
            reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_not_called()
        self.assertIn("expired", reply.lower())

    def test_confirming_a_different_action_is_refused(self):
        """Arming sleep and hearing 'confirm shutdown' is a mishearing."""
        system.power_action("sleep")
        reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_not_called()
        self.assertIn("waiting to confirm sleep", reply.lower())

    def test_an_arm_is_single_use(self):
        system.power_action("shutdown")
        system.power_action("shutdown", confirm=True)
        self.run_power.reset_mock()
        reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_not_called()
        self.assertIn("nothing was waiting", reply.lower())

    def test_locking_clears_a_pending_arm(self):
        """Lock is a real command, not a confirmation of the armed one."""
        system.power_action("shutdown")
        with mock.patch.object(system, "_lock_screen", return_value="Screen locked."):
            system.power_action("lock")
        reply = system.power_action("shutdown", confirm=True)
        self.run_power.assert_not_called()
        self.assertIn("nothing was waiting", reply.lower())

    def test_lock_needs_no_confirmation(self):
        with mock.patch.object(system, "_lock_screen", return_value="Screen locked."):
            self.assertEqual(system.power_action("lock"), "Screen locked.")

    def test_an_unknown_action_is_refused(self):
        reply = system.power_action("explode")
        self.run_power.assert_not_called()
        self.assertIn("don't know how", reply)


class TestBrightness(unittest.TestCase):
    def _run(self, returncode, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

    def test_a_rejecting_display_is_reported_as_a_failure(self):
        """gotcha 22: a process exiting is not the action succeeding.

        External monitors reject WmiSetBrightness. Reporting "brightness set"
        because PowerShell ran is exactly the false success that made
        open_application claim launches that never happened.
        """
        with mock.patch.object(system.subprocess, "run", return_value=self._run(1)):
            reply = system.set_brightness(40)
        self.assertNotIn("Brightness set", reply)
        self.assertIn("doesn't accept", reply)

    def test_a_working_display_reports_the_level(self):
        with mock.patch.object(system.subprocess, "run", return_value=self._run(0)):
            self.assertIn("40%", system.set_brightness(40))

    def test_out_of_range_is_refused_without_running_anything(self):
        with mock.patch.object(system.subprocess, "run") as run:
            reply = system.set_brightness(150)
        run.assert_not_called()
        self.assertIn("between 0 and 100", reply)

    def test_a_hung_powershell_does_not_wedge_the_worker(self):
        """Without a timeout this would block the command pipeline forever."""
        with mock.patch.object(
            system.subprocess, "run",
            side_effect=system.subprocess.TimeoutExpired("powershell", 8),
        ):
            reply = system.set_brightness(40)
        self.assertIn("stopped responding", reply)

    def test_the_subprocess_call_carries_a_timeout(self):
        with mock.patch.object(system.subprocess, "run",
                               return_value=self._run(0)) as run:
            system.set_brightness(40)
        self.assertIn("timeout", run.call_args.kwargs)


class TestSystemStatus(VolumeTestCase):
    def test_battery_and_volume_are_reported(self):
        with mock.patch.object(system, "_battery", return_value=(72, False)), \
             mock.patch.object(system, "_brightness_percent", return_value=None):
            reply = system.system_status()
        self.assertIn("72%", reply)
        self.assertIn("50%", reply)

    def test_charging_is_mentioned(self):
        with mock.patch.object(system, "_battery", return_value=(72, True)), \
             mock.patch.object(system, "_brightness_percent", return_value=None):
            self.assertIn("charging", system.system_status())

    def test_a_desktop_without_a_battery_still_answers(self):
        with mock.patch.object(system, "_battery", return_value=(None, True)), \
             mock.patch.object(system, "_brightness_percent", return_value=None):
            reply = system.system_status()
        self.assertIn("mains power", reply)

    def test_a_muted_device_says_muted_rather_than_a_level(self):
        self.endpoint._muted = 1
        with mock.patch.object(system, "_battery", return_value=(50, False)), \
             mock.patch.object(system, "_brightness_percent", return_value=None):
            self.assertIn("muted", system.system_status())


if __name__ == "__main__":
    unittest.main()
