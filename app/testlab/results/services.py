"""Creating and changing a Test Result (M2B)."""

from django.db import transaction
from django.db.models import Max

from testlab.execution import manual_log

from . import inspection, requirements
from .inspection import Component
from .models import AcceptanceItem, ResultComponent, TestResult


class ResultRuleError(Exception):
    """A change the Result rules do not allow; the message says why."""


def run_configuration(test_run):
    """Arrangement and Plug Seal of the run as instructed (its current TCN revision)."""
    return manual_log.run_setup(test_run)


def instructed_material(setup, position, component):
    """The face material the TCN names for a seal position, if any."""
    if not position or component not in (Component.STATOR, Component.ROTOR):
        return ""
    seal = setup.positions.filter(role=position).first()
    if seal is None:
        return ""
    return seal.rotating_face if component == Component.ROTOR else seal.stationary_face


def create_result(test_run, user):
    """Start the Result of a run with the faces of the seal under test.

    The components and their materials are copied from the run's TCN as it
    is now; later changes of the TCN or of the project defaults do not
    change the Result.
    """
    with transaction.atomic():
        if TestResult.objects.filter(test_run=test_run).exists():
            raise ResultRuleError(f"Run {test_run.display_no} already has a Result.")
        result = TestResult.objects.create(test_run=test_run, created_by=user, updated_by=user,
                                           responsible_engineer=user)  # fmt: skip
        setup = run_configuration(test_run)
        for position, component in inspection.default_components(setup.arrangement):
            ResultComponent.objects.create(
                result=result, position=position, component=component,
                material=instructed_material(setup, position, component),
            )
        requirements.sync_requirements(result, user)  # the requirements in force (provisional before the TCN is issued)
    return result


def sorted_components(result):
    return sorted(result.components.all(), key=lambda c: c.sort_key)


def addable_components(result):
    """Components offered by the run's configuration that the Result does not have yet."""
    setup = run_configuration(result.test_run)
    present = {(c.position, c.component) for c in result.components.all()}
    return [pair for pair in inspection.offered_components(setup.arrangement, setup.plug_seal_present)
            if pair not in present]  # fmt: skip


def add_component(result, position, component, user):
    if (position, component) not in addable_components(result):
        raise ResultRuleError(f"{inspection.identity_label(position, component)} cannot be added to this Result.")
    setup = run_configuration(result.test_run)
    with transaction.atomic():
        added = ResultComponent.objects.create(
            result=result, position=position, component=component,
            material=instructed_material(setup, position, component),
        )
        touch(result, user)
    return added


def remove_component(component, user):
    if not component.is_empty():
        raise ResultRuleError(f"{component} has measurements or pictures and cannot be removed.")
    if AcceptanceItem.objects.filter(component=component).exists():
        raise ResultRuleError(f"{component} is used by an acceptance criterion and cannot be removed.")
    with transaction.atomic():
        component.delete()
        touch(component.result, user)


def touch(result, user):
    result.updated_by = user
    result.save(update_fields=["updated_by", "updated_at"])


def next_acceptance_order(result):
    highest = result.acceptance_items.aggregate(m=Max("order"))["m"]
    return 0 if highest is None else highest + 1
