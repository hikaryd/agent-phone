import unittest

from watchdog import request_due


class WatchdogTests(unittest.TestCase):
    def test_recovery_waits_for_a_real_outage_and_has_a_cooldown(self):
        self.assertFalse(request_due(100, None, float("-inf")))
        self.assertFalse(request_due(119, 100, float("-inf")))
        self.assertTrue(request_due(120, 100, float("-inf")))
        self.assertFalse(request_due(179, 100, 120))
        self.assertTrue(request_due(180, 100, 120))


if __name__ == "__main__":
    unittest.main()
