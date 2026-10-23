"""Signals of the TCN app for the apps built on it (they may not be imported here)."""

from django.dispatch import Signal

# Sent inside the issuing transaction once a TCN revision is issued, with `tcn` and `user`. The Result of the
# run takes the requirements frozen with it (results/requirements.py); a receiver's error undoes the issue.
tcn_issued = Signal()
