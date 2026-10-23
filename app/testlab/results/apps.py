from django.apps import AppConfig


class ResultsConfig(AppConfig):
    """Test Result of a TestRun (M2B): inspection evidence and engineering assessment."""

    name = "testlab.results"
    label = "results"

    def ready(self):
        from testlab import locks

        from . import requirements  # noqa: F401  (receives tcn_issued)

        locks.register()  # the backstops of every lock (they span the TCN, execution and Result apps)
