"""Who may change the lab planning. Every active user can view it.

Initially every active, logged-in user may edit bookings and unit blocks;
a later distinction changes this module only.
"""


def can_edit_planning(user):
    return user.is_authenticated and user.is_active
