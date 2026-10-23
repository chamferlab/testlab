"""The Result's acceptance items, taken from the requirements in force for the run (M2E).

    Test Program → TCN (frozen at issue, tcn/requirements.py) → AcceptanceItem of the Result
                                                                (actual, assessment, remark; source recorded)

One way: the Result takes the list of the run's issued TCN revision, never
the live Test Program. It does so at set moments only (sync_requirements):
when the Result is created, when a new revision of the run's TCN is issued
(while the Result is a Draft), and when a revision of a final Result is
opened. Opening a page, a preview or finalizing changes nothing.

Before the run's TCN is issued (allowed during development and validation),
the list is provisional: the Test Program as it is now with the draft's
changes, its sources saying that the TCN is not issued yet. Issuing the TCN
replaces it with the frozen list. Once a TCN revision is issued, the changes
of a newer draft revision are pending (shown, not applied).

A requirement on leakage or wear without a seal applies to every seal of the
arrangement under test (one item per seal, or per seal face present in the
Result). Items entered on the Result itself have no source and are left
alone. Taking a list creates the items of new requirements, updates the
requirement of existing ones (never the engineer's assessment or remark),
and removes the items of requirements no longer in force — or, when the
engineer already assessed or remarked on one, keeps it marked as no longer
in force for the engineer to decide. Finalization freezes the items with
their sources in the report snapshot.
"""

from django.db import transaction
from django.db.models import Max
from django.dispatch import receiver

from testlab.projects.arrangements import SealRole, seal_under_test_roles
from testlab.projects.models import RequirementBasis
from testlab.tcn import requirements as tcn_requirements
from testlab.tcn.models import TcnRevision
from testlab.tcn.signals import tcn_issued

from .inspection import FACES
from .models import WITHDRAWN_PREFIX, AcceptanceItem, Assessment, TestResult

WITHDRAWN = WITHDRAWN_PREFIX
program_of = tcn_requirements.program_of


def approved_tcn(test_run):
    """The run's TCN revision whose requirements are in force: the current issued one, or None."""
    current = test_run.current_revision()
    return current if current is not None and current.status == TcnRevision.Status.ISSUED else None


def draft_tcn(test_run):
    return test_run.revisions.filter(status=TcnRevision.Status.DRAFT).first()


def pending_changes(test_run):
    """Requirement changes of a draft revision after an issued one: not in force until it is issued."""
    draft = draft_tcn(test_run)
    if draft is None or approved_tcn(test_run) is None:
        return []
    return list(draft.requirement_changes.select_related("program_requirement"))


def effective_requirements(test_run):
    """The list frozen with the run's issued TCN; before it is issued, the provisional list."""
    tcn = approved_tcn(test_run)
    if tcn is not None:
        return tcn_requirements.frozen(tcn)
    draft = draft_tcn(test_run)
    return tcn_requirements.in_force(draft) if draft is not None else []


def is_provisional(test_run):
    return approved_tcn(test_run) is None


def _criterion(values):
    text = values["criterion"] or values["requirement_text"] or RequirementBasis(values["basis"]).label
    return text[:120]


def expand(effective, test_run, components):
    """(position, component) targets of one requirement for this Result."""
    values = effective.values
    basis = values["basis"]
    roles = [values["position"]] if values["position"] else seal_under_test_roles(test_run.current_revision().setup.arrangement)
    if basis == RequirementBasis.LEAKAGE_RATE:
        return [(role, None) for role in roles]
    if basis == RequirementBasis.WEAR_RATE:
        faces = [values["face"]] if values["face"] else list(FACES)
        targets = [("", c) for c in components if c.position in roles and c.component in faces]
        return targets or [("", None)]  # no such face in the Result: the item says so
    return [("", None)]


def _key(item_or_values):
    get = item_or_values.get if isinstance(item_or_values, dict) else lambda name: getattr(item_or_values, name)
    return (get("program_requirement_id"), get("tcn_change_id"), get("position"), get("component_id"))


def desired_items(test_run, result):
    components = list(result.components.all())
    desired = []
    for effective in effective_requirements(test_run):
        for position, component in expand(effective, test_run, components):
            values = effective.values
            desired.append({
                "program_requirement_id": effective.program_requirement_id,
                "tcn_change_id": effective.tcn_change_id,
                "position": position,
                "component_id": component.pk if component else None,
                "criterion": _criterion(values),
                "basis": values["basis"],
                "comparison": values["comparison"],
                "limit": values["limit"],
                "unit": values["unit"],
                "requirement_text": values["requirement_text"],
                "source": effective.source[:300],
                "not_applicable": effective.not_applicable,
                "reason": effective.reason,
            })  # fmt: skip
    return desired


REQUIREMENT_FIELDS = ["criterion", "basis", "position", "component_id", "comparison", "limit", "unit",
                      "requirement_text", "source"]  # fmt: skip


def sync_requirements(result, user):
    """Take the requirements in force into the Result's items (Draft only; at the moments listed in the module
    docstring). Returns notices."""
    if result.is_final:
        return []
    notices = []
    with transaction.atomic():
        desired = desired_items(result.test_run, result)
        # From a requirement (its source may have been deleted since); not own items, not withdrawn ones.
        linked = [i for i in result.acceptance_items.all() if i.from_requirement]
        by_key = {_key(i): i for i in linked}
        order = (result.acceptance_items.aggregate(m=Max("order"))["m"] or 0) + 1
        wanted = set()
        for values in desired:
            key = _key(values)
            wanted.add(key)
            item = by_key.get(key)
            if item is None:
                item = AcceptanceItem(result=result, order=order, updated_by=user,
                                      program_requirement_id=key[0], tcn_change_id=key[1])  # fmt: skip
                order += 1
            changed = item.pk is None
            for name in REQUIREMENT_FIELDS:
                if getattr(item, name) != values[name]:
                    setattr(item, name, values[name])
                    changed = True
            if values["not_applicable"] and not item.assessment:
                item.assessment = Assessment.NOT_APPLICABLE
                item.remark = item.remark or f"Not applicable to this run: {values['reason']}"
                changed = True
            if changed:
                item.updated_by = user
                item.save()
        for item in linked:
            if _key(item) in wanted:
                continue
            if not item.assessment and not item.remark.strip():
                item.delete()
            else:
                item.program_requirement = item.tcn_change = None
                if not item.source.startswith(WITHDRAWN):
                    item.source = (WITHDRAWN + item.source)[:300]
                item.updated_by = user
                item.save()
                notices.append(f"“{item.criterion}” is no longer a requirement in force; it was kept because it "
                               "has an assessment or remark. Delete it or keep it as an own criterion.")  # fmt: skip
    return notices


@receiver(tcn_issued, dispatch_uid="result_takes_issued_requirements")
def _take_issued_requirements(sender, tcn, user, **kwargs):
    result = TestResult.objects.filter(test_run_id=tcn.test_run_id, status=TestResult.Status.DRAFT).first()
    if result is not None:
        sync_requirements(result, user)


def withdrawn(items):
    return [i for i in items if i.withdrawn]


def seal_label(position):
    return SealRole(position).label if position else ""
