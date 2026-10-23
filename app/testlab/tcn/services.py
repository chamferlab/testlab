"""TCN workflows (M1 spec §4, §7, §9.4): create from project defaults or by
copy, save a draft with optimistic locking (V10), issue, new revision,
cancel and delete a draft.

The domain rules themselves (snapshot ownership, same-project copies,
immutability) live in models.py; these functions only combine them into
the user's actions, each in one transaction.
"""

import hashlib

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import F, ProtectedError
from django.utils import timezone

from testlab.documents.pdf import PdfConversionError
from testlab.documents.render import tcn_template
from testlab.documents.tcn_context import build_tcn_context, tcn_file_name
from testlab.projects.arrangements import SealRole

from . import requirements
from .models import ImmutableTcnError, TcnRevision, TcnRuleError, TestRun
from .signals import tcn_issued

RUN_NUMBER_USED = "Run number already used!"


class StaleDraftError(TcnRuleError):
    """The draft was saved by someone else since it was opened (V10)."""


class IssueError(TcnRuleError):
    """The TCN cannot be issued; `errors` lists the reasons (V11, V15)."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def initials(user):
    """Default "Prepared by": first letters of first and last name, e.g. Maria Geerts → MG."""
    names = [user.first_name, user.last_name]
    return "".join(name.strip()[0] for name in names if name.strip()).upper()[:10]


def default_copy_source(project):
    """Current revision of the highest run of the project, or None."""
    run = TestRun.objects.filter(project=project).order_by("-run_no").first()
    return run.current_revision() if run else None


def create_tcn(project, run_no, user, source=None):
    """Create TestRun `run_no` and its draft rev 0.

    Without `source` the run setup is a copy of the project defaults;
    otherwise a copy of `source` (a TCN revision of the same project).
    The project description always comes from the project as it is now.
    """
    if TestRun.objects.filter(project=project, run_no=run_no).exists():
        raise ValidationError({"run_no": RUN_NUMBER_USED})
    try:
        with transaction.atomic():
            run = TestRun.objects.create(project=project, run_no=run_no, created_by=user)
            return TcnRevision.objects.create(
                test_run=run,
                revision=0,
                setup=(source.setup if source else project.default_setup).clone(),
                project_description=project.description,
                prepared_by=initials(user),
                source_kind=TcnRevision.SourceKind.TCN_COPY if source else TcnRevision.SourceKind.PROJECT_DEFAULTS,
                source_tcn=source,
                created_by=user,
                updated_by=user,
            )
    except IntegrityError:
        # Another user took the number between the check and the insert.
        if TestRun.objects.filter(project=project, run_no=run_no).exists():
            raise ValidationError({"run_no": RUN_NUMBER_USED})
        raise


def save_draft(tcn, header_form, editor, expected_row_version, user):
    """Save the header fields and run setup of a draft (S7).

    Refused when the draft was saved by someone else after `expected_row_version`
    was read, or when it is no longer a draft.
    """
    with transaction.atomic():
        _claim_draft(tcn, expected_row_version, user)
        editor.save()
        tcn = header_form.save(commit=False)
        tcn.row_version = expected_row_version + 1
        tcn.updated_by = user
        tcn.save()
    return tcn


def _claim_draft(tcn, expected_row_version, user):
    """Compare-and-set on row_version: claims the next version atomically on every database.

    Refused when the draft was saved by someone else after `expected_row_version`
    was read, or when it is no longer a draft.
    """
    claimed = TcnRevision.objects.filter(
        pk=tcn.pk, status=TcnRevision.Status.DRAFT, row_version=expected_row_version
    ).update(row_version=F("row_version") + 1, updated_by=user, updated_at=timezone.now())
    if not claimed:
        current = TcnRevision.objects.select_related("updated_by").get(pk=tcn.pk)
        if current.is_locked:
            raise ImmutableTcnError(f"{current} has been issued and can no longer be edited.")
        who = current.updated_by.get_full_name() or current.updated_by.username
        when = timezone.localtime(current.updated_at).strftime("%d-%b-%Y %H:%M")
        raise StaleDraftError(f"{current} was changed by {who} at {when} — reload it to see the changes.")


# --- Issue (§4, §9.4) -----------------------------------------------------------------


def issue_errors(tcn):
    """V11: what must be filled in before `tcn` can be issued (empty list: ready)."""
    setup = tcn.setup
    errors = []
    required = [
        (tcn.project_description, "Project description"),
        (tcn.prepared_by, "Prepared by"),
        (setup.run_description, "Test Run description"),
        (setup.test_unit, "Test unit"),
    ]
    if setup.plug_seal_present:  # the Plug Seal establishes the process conditions
        required.append((setup.process_medium, "Process medium"))
    if tcn.revision >= 1:
        required.append((tcn.revision_reason, "Reason for revision"))
    errors += [f"{label} is required." for value, label in required if not value]
    positions = {p.role: p for p in setup.positions.all()}
    for role in setup.applicable_roles():
        if not getattr(positions.get(role), "seal_type_size", ""):
            errors.append(f"{SealRole(role).label}: seal type and size is required.")
    try:
        setup.validate_positions()
    except ValidationError as error:
        errors += error.messages
    return errors


def issued_document_name(tcn):
    """Storage path of the issued PDF: projects/<project_no>/tcn/<file name> (§9.4)."""
    return f"projects/{tcn.test_run.project.project_no}/tcn/{tcn_file_name(tcn)}"


def issue_tcn(tcn, expected_row_version, user):
    """Issue a draft: the stored PDF and snapshot become the record of the instruction.

    In one transaction: V11 check on the saved draft, render the PDF without
    the draft marking (from the TCN document template), store it
    (refused if the file exists, V15), store the SHA-256 of the stored PDF
    and of the template and the render context, freeze the acceptance
    requirements in force (requirements.freeze), set issued_at/issued_by (the
    user performing the issue), supersede the previously issued revision
    of the run and pass the requirements on to the run's Draft Result
    (signals.tcn_issued). On any failure — including a failed PDF conversion — nothing
    is kept: the TCN stays a draft and a file already written is removed.
    """
    storage = TcnRevision._meta.get_field("issued_document").storage
    written = None
    try:
        with transaction.atomic():
            _claim_draft(tcn, expected_row_version, user)  # also: nobody changed it since review
            tcn = TcnRevision.objects.select_related("test_run__project", "setup").get(pk=tcn.pk)
            errors = issue_errors(tcn)
            if errors:
                raise IssueError(errors)
            if tcn.test_run.revisions.filter(status=TcnRevision.Status.CANCELLED).exists():
                raise IssueError([f"TCN{tcn.test_run.display_no} has been cancelled; nothing was issued."])

            context = build_tcn_context(tcn)
            context["is_draft"] = False
            try:
                template = tcn_template()  # read once: the PDF is rendered from what template_sha256 describes
                content = template.render_pdf(context)
            except PdfConversionError as error:
                raise IssueError([f"The PDF could not be generated; nothing was issued. {error}"]) from error

            name = issued_document_name(tcn)
            if storage.exists(name):
                raise IssueError([f"The file {name} already exists; nothing was issued."])
            written = storage.save(name, ContentFile(content))
            if written != name:  # created concurrently: the storage picked another name
                raise IssueError([f"The file {name} already exists; nothing was issued."])
            with storage.open(written, "rb") as stored:  # the hash must describe the stored file
                if stored.read() != content:
                    raise IssueError([f"The file {name} was not stored correctly; nothing was issued."])

            previous = tcn.test_run.revisions.filter(status=TcnRevision.Status.ISSUED).exclude(pk=tcn.pk)
            for revision in previous:  # at most one (database constraint)
                revision.status = TcnRevision.Status.SUPERSEDED
                revision.updated_by = user
                revision.save()

            issued_at = timezone.now()
            requirements.freeze(tcn, issued_at)  # while still a draft: the requirements are locked with it
            tcn.status = TcnRevision.Status.ISSUED
            tcn.issued_at = issued_at
            tcn.issued_by = user
            tcn.issued_document.name = written
            tcn.issued_document_sha256 = hashlib.sha256(content).hexdigest()
            tcn.template_sha256 = template.sha256
            tcn.issued_snapshot = context
            tcn.updated_by = user
            tcn.save()
            tcn_issued.send(sender=TcnRevision, tcn=tcn, user=user)
    except BaseException:
        if written is not None:
            storage.delete(written)
        raise
    return tcn


# --- New revision, cancel, delete draft (§4, V13, V14) ----------------------------------------


def new_revision_error(tcn):
    """V13: why no new revision can be made from `tcn` (None: allowed)."""
    run = tcn.test_run
    if run.revisions.filter(status=TcnRevision.Status.CANCELLED).exists():
        return f"TCN{run.display_no} has been cancelled; it cannot be revised."
    if tcn.status != TcnRevision.Status.ISSUED:
        return f"{tcn} is not the current issued revision; only that one can be revised."
    draft = run.revisions.filter(status=TcnRevision.Status.DRAFT).first()
    if draft:
        return f"{draft} is already in preparation as a draft."
    return None


def create_revision(tcn, user):
    """New revision of the current issued revision `tcn`: draft rev n+1, an independent copy.

    Copies the run setup and project description of `tcn`; prepared by is the
    current user. `tcn` itself is not changed until the new revision is issued.
    """
    try:
        with transaction.atomic():
            # Lock the source so concurrent revise/cancel requests are serialised.
            tcn = TcnRevision.objects.select_for_update().select_related("test_run", "setup").get(pk=tcn.pk)
            error = new_revision_error(tcn)
            if error:
                raise TcnRuleError(error)
            revision = TcnRevision.objects.create(
                test_run=tcn.test_run,
                revision=tcn.test_run.next_revision_no(),
                setup=tcn.setup.clone(),
                project_description=tcn.project_description,
                prepared_by=initials(user),
                source_kind=TcnRevision.SourceKind.NEW_REVISION,
                source_tcn=tcn,
                created_by=user,
                updated_by=user,
            )
            for change in tcn.requirement_changes.all():  # the run's requirement changes stay in force
                change.clone_to(revision)
            return revision
    except IntegrityError:
        # Another user created the draft revision at the same time (one draft per run).
        draft = tcn.test_run.revisions.filter(status=TcnRevision.Status.DRAFT).first()
        if draft:
            raise TcnRuleError(f"{draft} is already in preparation as a draft.")
        raise


def cancel_error(tcn):
    """V14 (and no pending draft): why `tcn` cannot be cancelled (None: allowed)."""
    if tcn.status != TcnRevision.Status.ISSUED:
        return f"{tcn} is not the current issued revision; only that one can be cancelled."
    draft = tcn.test_run.revisions.filter(status=TcnRevision.Status.DRAFT).first()
    if draft:
        return f"{draft} is in preparation; delete that draft before cancelling {tcn}."
    return None


def cancel_tcn(tcn, reason, user):
    """Cancel the current issued revision (S10). Its record, document and snapshot are kept."""
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": "A reason for cancelling is required."})
    with transaction.atomic():
        tcn = TcnRevision.objects.select_for_update().select_related("test_run").get(pk=tcn.pk)
        error = cancel_error(tcn)
        if error:
            raise TcnRuleError(error)
        tcn.status = TcnRevision.Status.CANCELLED
        tcn.cancelled_at = timezone.now()
        tcn.cancelled_by = user
        tcn.cancel_reason = reason
        tcn.updated_by = user
        tcn.save()
    return tcn


def delete_draft(tcn, expected_row_version, user):
    """Delete a draft without trace (§4). Rev 0 takes its TestRun with it.

    Refused when the draft was saved by someone else since it was reviewed
    (V10), when it is no longer a draft, or when another TCN was copied from it.
    """
    with transaction.atomic():
        _claim_draft(tcn, expected_row_version, user)
        tcn = TcnRevision.objects.select_related("test_run").get(pk=tcn.pk)
        derived = list(tcn.derived_revisions.all())
        if derived:
            names = ", ".join(str(d) for d in derived)
            verb = "was" if len(derived) == 1 else "were"
            raise TcnRuleError(f"{tcn} cannot be deleted: {names} {verb} copied from it.")
        run = tcn.test_run
        if tcn.revision == 0 and (run.manual_log_entries.exists() or run.dcs_datasets.exists()):
            # Deleting rev 0 deletes the run; test evidence is never removed as a side effect.
            raise TcnRuleError(f"{tcn} cannot be deleted: Run {run.display_no} has manual log readings or DCS data.")
        if tcn.revision == 0 and hasattr(run, "result"):
            raise TcnRuleError(f"{tcn} cannot be deleted: Run {run.display_no} has a Test Result.")
        try:
            tcn.delete()  # the model removes its setup and, for rev 0, the TestRun
        except ProtectedError as error:  # a copy made concurrently
            raise TcnRuleError(f"{tcn} cannot be deleted: another TCN was copied from it.") from error
