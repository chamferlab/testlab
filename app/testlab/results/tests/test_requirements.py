"""Acceptance requirements: Test Program baseline, approved TCN changes, Result items with their source (M2E)."""

from decimal import Decimal

import pytest
from django.urls import reverse

from testlab.documents.render import ChromiumTemplate
from testlab.documents.tcn_context import build_tcn_context
from testlab.documents.tests.html_text import html_lines
from testlab.projects.models import ProgramRequirement, TestProgram
from testlab.results import requirements, services
from testlab.results.models import AcceptanceItem, Assessment
from testlab.tcn.models import ImmutableTcnError, TcnRequirementChange
from testlab.tcn.services import create_revision
from testlab.tcn import requirements as tcn_requirements
from testlab.tcn.tests.factories import issue, supersede

from .helpers import make_run

pytestmark = pytest.mark.django_db


@pytest.fixture
def run(user):
    return make_run(user, "dual_pressurized")


@pytest.fixture
def program(run, user):
    program = TestProgram.objects.create(project=run.project, document_no="TP-7", revision="A",
                                         title="Qualification", objective="Prove it.", updated_by=user)  # fmt: skip
    ProgramRequirement.objects.create(program=program, number=1, criterion="Leakage", basis="leakage_rate",
                                      comparison="<=", limit=Decimal("10"), unit="ml/h", updated_by=user)  # fmt: skip
    ProgramRequirement.objects.create(program=program, number=2, criterion="Test duration", basis="running_hours",
                                      comparison=">=", limit=Decimal("100"), unit="h", updated_by=user)  # fmt: skip
    ProgramRequirement.objects.create(program=program, number=3, criterion="Face wear", basis="wear_rate",
                                      face="rotor", comparison="<=", limit=Decimal("0.000003"), unit="in/h",
                                      updated_by=user)  # fmt: skip
    return program


def p(program, number):
    return program.requirements.get(number=number)


def change(tcn, action, target=None, **fields):
    return TcnRequirementChange.objects.create(tcn=tcn, action=action, program_requirement=target,
                                               reason=fields.pop("reason", "Customer agreement"), **fields)  # fmt: skip


def items(result):
    return list(result.acceptance_items.order_by("order", "pk").select_related("component"))


PROVISIONAL = " (TCN not issued yet)"


def test_program_requirements_become_items_per_seal_and_per_face(run, program, user):
    result = services.create_result(run, user)  # taken when the Result is created: provisional
    found = [(i.criterion, i.position, i.component.label if i.component else "", i.source) for i in items(result)]
    assert found == [
        ("Leakage", "inboard", "", "Test Program TP-7 rev A P1" + PROVISIONAL),
        ("Leakage", "outboard", "", "Test Program TP-7 rev A P1" + PROVISIONAL),
        ("Test duration", "", "", "Test Program TP-7 rev A P2" + PROVISIONAL),
        ("Face wear", "", "IB Rotor", "Test Program TP-7 rev A P3" + PROVISIONAL),  # rotors only (face = rotor)
        ("Face wear", "", "OB Rotor", "Test Program TP-7 rev A P3" + PROVISIONAL),
    ]
    assert all(i.assessment == "" for i in items(result))  # missing until assessed
    issue(run.current_revision(), user)  # the frozen list replaces the provisional one, item by item
    assert [i.source for i in items(result)] == [source.removesuffix(PROVISIONAL) for *_, source in found]
    assert len({i.pk for i in items(result)}) == 5 and {i.pk for i in items(result)} == {i.pk for i in result.acceptance_items.all()}


def test_draft_tcn_changes_apply_provisionally_and_are_pending_after_an_issue(run, program, user):
    tcn = run.current_revision()
    change(tcn, "replace", p(program, 1), criterion="Leakage", basis="leakage_rate", comparison="<=",
           limit=Decimal("20"), unit="ml/h")  # fmt: skip
    result = services.create_result(run, user)
    leakage = [i for i in items(result) if i.basis == "leakage_rate"]
    assert {i.limit for i in leakage} == {Decimal("20")}
    assert leakage[0].source == "TCN001 (not issued yet): replaces Test Program TP-7 rev A P1"
    assert requirements.pending_changes(run) == []  # nothing issued yet: the draft is the provisional source
    issue(tcn, user)
    revision = create_revision(tcn, user)
    revision.requirement_changes.update(limit=Decimal("30"))
    assert len(requirements.pending_changes(run)) == 1
    assert {i.limit for i in items(result) if i.basis == "leakage_rate"} == {Decimal("20")}  # not applied


def test_an_issued_tcn_change_overrides_the_program_for_the_run(run, program, user):
    tcn = run.current_revision()
    change(tcn, "replace", p(program, 1), criterion="Leakage, run-in", basis="leakage_rate", comparison="<=",
           limit=Decimal("20"), unit="ml/h", reason="Run-in test")  # fmt: skip
    change(tcn, "not_applicable", p(program, 3), reason="SiC rotors: no wear expected")
    change(tcn, "add", criterion="Torque", basis="manual", requirement_text="Breakaway torque below 50 Nm")
    issue(tcn, user)
    result = services.create_result(run, user)
    by_criterion = {}
    for i in items(result):
        by_criterion.setdefault(i.criterion, []).append(i)
    leakage = by_criterion["Leakage, run-in"]
    assert [i.limit for i in leakage] == [Decimal("20"), Decimal("20")] and "Leakage" not in by_criterion
    assert leakage[0].source.startswith("TCN001, issued") and leakage[0].source.endswith("replaces Test Program TP-7 rev A P1")
    wear = by_criterion["Face wear"]
    assert all(i.assessment == Assessment.NOT_APPLICABLE for i in wear)  # explicit, not missing
    assert "not applicable to this run per TCN001" in wear[0].source and "SiC rotors" in wear[0].remark
    (torque,) = by_criterion["Torque"]
    assert torque.source.endswith("additional requirement") and torque.tcn_change is not None
    assert by_criterion["Test duration"][0].source == "Test Program TP-7 rev A P2"  # unchanged


def test_program_edits_reach_a_run_only_with_a_new_tcn_revision(run, program, user):
    tcn = issue(run.current_revision(), user)
    result = services.create_result(run, user)
    duration = next(i for i in items(result) if i.criterion == "Test duration")
    duration.assessment, duration.remark = Assessment.NOT_EVALUATED, "Stopped early by the customer"
    duration.save()
    requirement = p(program, 2)
    requirement.limit = Decimal("150")
    requirement.save()
    assert tcn_requirements.outdated(run.project) == [tcn]
    assert requirements.sync_requirements(result, user) == [] and duration.limit == Decimal("100")
    duration.refresh_from_db()
    assert duration.limit == Decimal("100")  # the run keeps the requirements of its issued TCN
    revision = create_revision(tcn, user)
    supersede(tcn)
    issue(revision, user)
    duration.refresh_from_db()
    assert duration.limit == Decimal("150")  # taken from the new revision
    assert (duration.assessment, duration.remark) == (Assessment.NOT_EVALUATED, "Stopped early by the customer")
    assert tcn_requirements.outdated(run.project) == []


def test_withdrawn_requirement_is_removed_or_kept_when_assessed(run, program, user):
    tcn = issue(run.current_revision(), user)
    result = services.create_result(run, user)
    wear = [i for i in items(result) if i.criterion == "Face wear"]
    wear[0].remark = "Checked"
    wear[0].save()
    p(program, 3).delete()
    assert len([i for i in items(result) if i.criterion == "Face wear"]) == 2  # still in force for the run
    revision = create_revision(tcn, user)
    supersede(tcn)
    issue(revision, user)
    left = [i for i in items(result) if i.criterion == "Face wear"]
    assert len(left) == 1 and left[0].source.startswith(requirements.WITHDRAWN)


def test_requirements_in_force_are_frozen_with_the_issued_tcn(run, program, user):
    tcn = issue(run.current_revision(), user)
    frozen = list(tcn.requirements_in_force.all())
    assert [(r.order, r.source) for r in frozen] == [(1, "Test Program TP-7 rev A P1"), (2, "Test Program TP-7 rev A P2"),
                                                     (3, "Test Program TP-7 rev A P3")]  # fmt: skip
    frozen[0].limit = Decimal("1")
    with pytest.raises(ImmutableTcnError, match="frozen with the TCN"):
        frozen[0].save()
    p(program, 1).delete()  # the Program goes on; the frozen requirement keeps its wording
    frozen[0].refresh_from_db()
    assert frozen[0].program_requirement is None and frozen[0].limit == Decimal("10")


def test_opening_a_page_does_not_change_the_items(client, run, program, user):
    result = services.create_result(run, user)
    ProgramRequirement.objects.create(program=program, number=4, criterion="Noise", basis="manual",
                                      requirement_text="No abnormal noise", updated_by=user)  # fmt: skip
    client.force_login(user)
    client.get(reverse("results:result", args=[run.pk]))
    assert result.acceptance_items.count() == 5


def test_tcn_change_is_frozen_when_issued_and_carried_into_a_new_revision(run, program, user):
    tcn = run.current_revision()
    original = change(tcn, "not_applicable", p(program, 3), reason="SiC rotors")
    issue(tcn, user)
    original.reason = "changed afterwards"
    with pytest.raises(ImmutableTcnError):
        original.save()
    with pytest.raises(ImmutableTcnError):
        original.delete()
    revision = create_revision(tcn, user)
    (copied,) = revision.requirement_changes.all()
    assert copied.pk != original.pk and copied.program_requirement == p(program, 3) and copied.reason == "SiC rotors"


def test_tcn_prints_its_requirement_changes(run, program, user):
    tcn = run.current_revision()
    change(tcn, "replace", p(program, 1), criterion="Leakage", basis="leakage_rate", comparison="<=",
           limit=Decimal("20"), unit="ml/h", reason="Run-in test")  # fmt: skip
    lines = html_lines(ChromiumTemplate.load("tcn").render_html(build_tcn_context(tcn)))
    text = " ".join(lines)
    assert "Acceptance Requirement Changes:" in lines
    assert "P1 Leakage: Replaces Program requirement: ≤ 20 ml/h (Test Program: ≤ 10 ml/h). Reason: Run-in test" in text


def test_tcn_without_changes_prints_no_section(run):
    lines = html_lines(ChromiumTemplate.load("tcn").render_html(build_tcn_context(run.current_revision())))
    assert "Acceptance Requirement Changes:" not in lines


# --- pages ------------------------------------------------------------------------------------


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def test_program_page_saves_title_and_numbered_requirements(logged_in, run, user):
    url = reverse("projects:program", args=[run.project.pk])
    response = logged_in.post(url, {"document_no": "TP-9", "revision": "0", "title": "Endurance", "objective": "Run."})
    assert response.status_code == 302
    add = reverse("projects:program_requirement_add", args=[run.project.pk])
    data = {"criterion": "Leakage", "basis": "leakage_rate", "position": "", "face": "", "comparison": "<=",
            "limit": "12,5", "unit": "", "requirement_text": ""}  # fmt: skip
    assert logged_in.post(add, data).status_code == 302
    assert logged_in.post(add, {**data, "criterion": "Wear", "basis": "wear_rate", "limit": "3E-6"}).status_code == 302
    first, second = TestProgram.objects.get().requirements.all()
    assert (first.ref, first.limit, first.unit) == ("P1", Decimal("12.5"), "ml/h")  # unit of the calculated actual
    assert (second.ref, second.limit, second.unit) == ("P2", Decimal("0.000003"), "in/h")
    bad = logged_in.post(add, {**data, "comparison": "", "limit": ""})
    assert bad.status_code == 200 and "Enter the requirement" in bad.content.decode()
    page = logged_in.get(url).content.decode()
    assert "Endurance" in page and "P2" in page


def test_tcn_page_adds_a_change_only_while_draft(logged_in, run, program, user):
    tcn = run.current_revision()
    url = reverse("tcn:requirement_change_add", args=[tcn.pk])
    data = {"action": "not_applicable", "program_requirement": p(program, 3).pk, "reason": "SiC rotors",
            "criterion": "", "basis": "manual", "position": "", "face": "", "comparison": "", "limit": "",
            "unit": "", "requirement_text": ""}  # fmt: skip
    assert logged_in.post(url, data).status_code == 302
    assert logged_in.post(url, data).status_code == 200  # the same requirement twice: refused
    (saved,) = tcn.requirement_changes.all()
    assert saved.action == "not_applicable" and saved.basis == "manual"
    issue(tcn, user)
    response = logged_in.post(url, {**data, "program_requirement": p(program, 1).pk})
    assert response.status_code == 302 and tcn.requirement_changes.count() == 1


def test_result_page_shows_sources_and_missing_assessments(logged_in, run, program, user):
    services.create_result(run, user)
    page = logged_in.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Test Program TP-7 rev A P1 (TCN not issued yet)" in page and "Not assessed" in page
    assert AcceptanceItem.objects.count() == 5  # created with the Result


def test_program_page_names_tcns_issued_with_earlier_requirements(logged_in, run, program, user):
    url = reverse("projects:program", args=[run.project.pk])
    issue(run.current_revision(), user)
    assert "Issued with the earlier requirements" not in logged_in.get(url).content.decode()
    requirement = p(program, 1)
    requirement.limit = Decimal("5")
    requirement.save()
    page = logged_in.get(url).content.decode()
    assert "Issued with the earlier requirements: <a" in page and ">TCN001</a>" in page


def test_issuing_a_tcn_freezes_its_requirements_and_updates_the_draft_result(user):
    from testlab.tcn.tests.factories import make_project
    from testlab.tcn.tests.test_issue import issue as issue_with_pdf
    from testlab.tcn.tests.test_issue import ready_tcn

    tcn = ready_tcn(make_project(user), user)
    program = TestProgram.objects.create(project=tcn.test_run.project, document_no="TP-7", updated_by=user)
    ProgramRequirement.objects.create(program=program, number=1, criterion="Test duration", basis="running_hours",
                                      comparison=">=", limit=Decimal("100"), unit="h", updated_by=user)  # fmt: skip
    result = services.create_result(tcn.test_run, user)
    assert [i.source for i in items(result)] == ["Test Program TP-7 P1" + PROVISIONAL]
    tcn = issue_with_pdf(tcn, user)
    assert [r.source for r in tcn.requirements_in_force.all()] == ["Test Program TP-7 P1"]
    assert [i.source for i in items(result)] == ["Test Program TP-7 P1"]
