"""Acceptance requirements in force for a run: the Test Program with the TCN's changes applied.

    Test Program requirement (planned, baseline)        P1, P2, …
      └─ TCN change: replaces it, or makes it not applicable to this run, or adds a requirement
            └─ frozen with the TCN revision when it is issued (TcnRequirement)

The flow is one way: Program → TCN → Result. Issuing a TCN revision freezes
the list in force (freeze); a later edit of the Test Program reaches a run
only when a new revision of its TCN is issued (outdated says which issued
TCNs still hold the earlier list). The Result takes its list from the issued
TCN, or a provisional one while the TCN is not issued yet
(results/requirements.py).
"""

from dataclasses import dataclass

from django.utils import timezone

from testlab.documents.tcn_context import format_date
from testlab.projects.models import TestProgram

from .models import TcnRequirement, TcnRequirementChange, TcnRevision

Action = TcnRequirementChange.Action
VALUE_FIELDS = TcnRequirement.REQUIREMENT_FIELDS


@dataclass
class Effective:
    """A requirement in force for the run, with where it comes from."""

    values: dict  # AcceptanceRequirementFields values
    source: str
    program_requirement_id: int | None = None
    tcn_change_id: int | None = None
    not_applicable: bool = False
    reason: str = ""  # of a TCN change

    def same_as(self, other):
        return (self.values, self.program_requirement_id, self.tcn_change_id, self.not_applicable, self.reason) == (
            other.values, other.program_requirement_id, other.tcn_change_id, other.not_applicable, other.reason)


def program_of(project):
    return TestProgram.objects.filter(project=project).prefetch_related("requirements").first()


def in_force(tcn, issued_at=None):
    """The Test Program as it is now with the changes of `tcn`: what issuing it at `issued_at` freezes. Without
    `issued_at` the list is provisional: the sources say that the TCN is not issued yet."""
    program = program_of(tcn.test_run.project)
    changes = list(tcn.requirement_changes.all())
    by_target = {c.program_requirement_id: c for c in changes if c.program_requirement_id}
    if issued_at is not None:
        label, note = f"{tcn}, issued {format_date(timezone.localtime(issued_at))}", ""
    else:
        label, note = f"{tcn} (not issued yet)", " (TCN not issued yet)"
    found = []
    for requirement in program.requirements.all() if program else []:
        base = f"{program.reference} {requirement.ref}"
        change = by_target.get(requirement.pk)
        if change is None:
            found.append(Effective(requirement.requirement_values(), base + note, requirement.pk))
        elif change.action == Action.REPLACE:
            found.append(Effective(change.requirement_values(), f"{label}: replaces {base}", requirement.pk,
                                   change.pk, reason=change.reason))  # fmt: skip
        else:
            found.append(Effective(requirement.requirement_values(), f"{base}; not applicable to this run per {label}",
                                   requirement.pk, change.pk, not_applicable=True, reason=change.reason))  # fmt: skip
    for change in changes:
        if change.action == Action.ADD:
            found.append(Effective(change.requirement_values(), f"{label}: additional requirement", None, change.pk,
                                   reason=change.reason))  # fmt: skip
    return found


def freeze(tcn, issued_at):
    """Store the list in force with the TCN revision being issued (before its status changes: then it is locked)."""
    for order, effective in enumerate(in_force(tcn, issued_at), start=1):
        TcnRequirement.objects.create(
            tcn=tcn, order=order, program_requirement_id=effective.program_requirement_id,
            tcn_change_id=effective.tcn_change_id, source=effective.source[:300],
            not_applicable=effective.not_applicable, reason=effective.reason, **effective.values,
        )  # fmt: skip


def frozen(tcn):
    """The list frozen with an issued TCN revision."""
    return [Effective(row.requirement_values(), row.source, row.program_requirement_id, row.tcn_change_id,
                      row.not_applicable, row.reason) for row in tcn.requirements_in_force.all()]  # fmt: skip


def outdated(project):
    """The current issued TCN revisions of the project whose frozen list differs from the Test Program as it is
    now (with their own changes): issuing a new revision brings the run in step."""
    found = []
    revisions = TcnRevision.objects.filter(test_run__project=project, status=TcnRevision.Status.ISSUED)
    for tcn in revisions.select_related("test_run").order_by("test_run__run_no"):
        now, then = in_force(tcn, tcn.issued_at), frozen(tcn)
        if len(now) != len(then) or not all(a.same_as(b) for a, b in zip(now, then)):
            found.append(tcn)
    return found
