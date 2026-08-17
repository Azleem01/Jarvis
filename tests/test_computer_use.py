"""The computer-use loop's adaptive reasoning.

The steady state is mechanical (no thinking budget) for speed; but once a step
visibly stalls (the oscillation guard trips at _REPEAT_WARN), the loop escalates
to a real thinking budget so the model can reason its way out instead of
repeating a dead end until the abort cap.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tools import computer_use  # noqa: E402


class _FakeImage:
    size = (1000, 1000)


def _budget(gen_config):
    """thinking_budget out of a GenerateContentConfig, or None."""
    tc = getattr(gen_config, "thinking_config", None)
    return getattr(tc, "thinking_budget", None) if tc is not None else None


class AdaptiveReasoningTests(unittest.TestCase):
    def test_a_stalled_step_escalates_to_a_thinking_budget(self):
        seen = []

        def fake_vision(contents, gen_config, needs_pointing=False):
            seen.append(gen_config)
            # Always the same click -> the screen never changes -> a stall.
            return mock.Mock(text='{"action":"click","box":[10,10,20,20],"reason":"x"}')

        with mock.patch.object(computer_use, "providers", mock.Mock(vision_generate=fake_vision)), \
                mock.patch.object(computer_use.cancellation, "cancelled", return_value=False), \
                mock.patch.object(computer_use.screen, "capture_screen",
                                  side_effect=lambda: (_FakeImage(), b"j")), \
                mock.patch.object(computer_use.screen, "screens_differ", return_value=False), \
                mock.patch.object(computer_use.screen, "wait_for_change",
                                  return_value=(False, _FakeImage(), b"j")), \
                mock.patch.object(computer_use, "_execute", return_value="clicked"), \
                mock.patch.object(config, "AGENT_STALL_THINKING_BUDGET", 1024):
            computer_use.perform_computer_task("do a thing")

        budgets = [_budget(g) for g in seen]
        # Early steps are mechanical (budget 0); after the stall the loop escalates.
        self.assertEqual(budgets[0], config.AGENT_THINKING_BUDGET)
        self.assertEqual(
            budgets[-1], 1024,
            "after a step stalls, the loop must raise the thinking budget",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
