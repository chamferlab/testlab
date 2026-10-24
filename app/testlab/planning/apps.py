from django.apps import AppConfig


class PlanningConfig(AppConfig):
    """Lab planning: which test unit is booked by which project in which weeks (rough, never controlled)."""

    name = "testlab.planning"
    label = "planning"
