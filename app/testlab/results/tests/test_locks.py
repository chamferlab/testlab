"""testlab/locks.py: the page and its backstop refuse with the same text."""

import pytest
from django.db import transaction

from testlab import locks
from testlab.execution.models import ManualLogEntry
from testlab.results import finalization
from testlab.results.models import FinalResultError
from testlab.tcn.models import ImmutableTcnError

from .test_finalization import complete_result

pytestmark = pytest.mark.django_db


def test_final_result_locks_the_result_and_the_evidence_with_one_message(user):
    run, result = complete_result(user)
    assert locks.result_lock(run) == ""
    finalization.finalize(result, user)
    message = locks.result_lock(run)
    assert message == (f"The Result of Run {run.display_no} is final: it and the run's manual log and DCS data "
                       "cannot change. Open a revision to correct it.")  # fmt: skip
    assert locks.result_refusal(run) == message  # what the pages show
    entry = ManualLogEntry.objects.filter(test_run=run).first()
    component = result.components.first()
    for change in (entry.save, entry.delete, component.save):
        with pytest.raises(FinalResultError) as refused, transaction.atomic():
            change()
        assert str(refused.value) == message
        assert isinstance(refused.value, locks.Locked)


def test_issued_tcn_locks_its_setup(user):
    from testlab.tcn.tests.factories import make_project
    from testlab.tcn.tests.test_issue import issue, ready_tcn

    tcn = ready_tcn(make_project(user), user)
    assert locks.tcn_lock(tcn) == ""
    tcn = issue(tcn, user)
    assert locks.tcn_lock(tcn) == f"{tcn} is issued and cannot be edited."
    with pytest.raises(ImmutableTcnError, match="which has been issued") as refused, transaction.atomic():
        tcn.setup.positions.first().save()
    assert isinstance(refused.value, locks.Locked)
