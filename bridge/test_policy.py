import unittest

from policy import may_foreground_chrome


def activity(package):
    return f"topResumedActivity=ActivityRecord{{86993702 u0 {package}/.Main t152}}"


class RecoveryPolicyTests(unittest.TestCase):
    packages = ("com.android.chrome", "com.example.launcher")

    def test_background_chrome_can_recover_behind_termux_when_panel_off(self):
        self.assertTrue(may_foreground_chrome(activity("com.termux"), False, True, self.packages))

    def test_foreground_app_is_preserved_while_panel_on(self):
        self.assertFalse(may_foreground_chrome(activity("com.termux"), False, False, self.packages))

    def test_chrome_and_launcher_can_recover_while_panel_on(self):
        for package in self.packages:
            self.assertTrue(may_foreground_chrome(activity(package), False, False, self.packages))

    def test_unspecified_launcher_is_not_eligible(self):
        self.assertFalse(may_foreground_chrome(activity("com.other.launcher"), False, False, self.packages))

    def test_unknown_or_ambiguous_state_fails_closed(self):
        self.assertFalse(may_foreground_chrome(activity("com.termux"), False, None, self.packages))
        self.assertFalse(may_foreground_chrome("", False, True, self.packages))
        self.assertFalse(may_foreground_chrome(activity("com.termux") * 2, False, True, self.packages))

    def test_maintenance_blocks_recovery(self):
        self.assertFalse(may_foreground_chrome(activity("com.termux"), True, True, self.packages))


if __name__ == "__main__":
    unittest.main()
