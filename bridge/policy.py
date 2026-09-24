"""Pure recovery decisions for the Android browser bridge."""

import re


def may_foreground_chrome(activities, maintenance, display_off, foreground_packages):
    """Allow recovery only when the panel is off or Chrome/launcher owns the UI.

    An unknown display state, ambiguous activity, or owner maintenance fails closed.
    The caller must also verify the unlocked keyguard and hold the owner lease lock.
    """
    if maintenance or display_off is None:
        return False
    resumed = re.findall(r"^\s*topResumedActivity\s*=(.*)$", activities, re.MULTILINE)
    if len(resumed) != 1:
        return False
    record = re.fullmatch(
        r"ActivityRecord\{[0-9a-fA-F]+ u0 ([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+ t\d+[^}\n]*\}",
        resumed[0].strip(),
    )
    if record is None:
        return False
    return display_off is True or record[1] in foreground_packages
