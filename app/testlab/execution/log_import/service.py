"""The manual-log import service (M2F): one path for every workbook source.

    upload()          store the workbook unchanged, read it, propose a mapping       status Mapping
    confirm_mapping() keep a copy of the confirmed mapping (and the layout map),
                      interpret and classify every row, suggest events, check the
                      header against the run                                          status Preview
    save_choices()    the engineer's decisions per row (Import/Skip, category,
                      separate events, collection period) and acknowledgment
    commit()          one transaction: create the entries, or nothing                status Imported
    undo()            one transaction: remove the batch's entries, keep the record   status Undone
    discard()         a batch never imported: remove it and its stored file

Every write is refused while the run's evidence is closed (a final Result;
in production a TCN not yet issued): locks.result_refusal, with the
model guards of results.models as the backstop.
"""

import hashlib
import re
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from testlab import locks
from testlab.tcn.permissions import can_undo_log_import

from ..models import ManualLogEntry, ManualLogImport, ManualLogImportRow, ManualMeasurement
from . import events, interpret, mapping, matching, xlsx

Status = ManualLogImport.Status
Classification = ManualLogImportRow.Classification
Decision = ManualLogImportRow.Decision


class LogImportError(Exception):
    """The import cannot go on; nothing was changed. `problems`: what to correct."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class StaleImport(LogImportError):
    """The manual log changed since the preview: the rows were classified again, nothing was imported."""


def _refusal(test_run):
    return locks.result_refusal(test_run)  # the lock and policy of execution evidence


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "log.xlsx")[:120]


def import_dir(test_run):
    return f"projects/{test_run.project.project_no}/runs/{test_run.display_no}/log_imports"


def columns_with_values(batch):
    letters = set()
    for row in batch.rows.all():
        letters.update(letter for letter, cell in row.cells.items() if cell["k"] != "empty")
    return letters


# --- upload ------------------------------------------------------------------------------------


def upload(test_run, uploaded, user, sheet=None):
    """A new batch in status Mapping for an uploaded workbook; raises LogImportError (nothing stored)."""
    if refusal := _refusal(test_run):
        raise LogImportError(refusal)
    max_bytes = settings.TESTLAB_LOG_IMPORT_MAX_MB * 1024 * 1024
    if uploaded.size > max_bytes:
        raise LogImportError(f"The file is larger than {settings.TESTLAB_LOG_IMPORT_MAX_MB} MB.")
    content = uploaded.read()
    try:
        source = xlsx.read_sheet(content, sheet)
    except xlsx.WorkbookError as error:
        raise LogImportError(f"{uploaded.name}: {error}") from error
    storage = ManualLogImport._meta.get_field("raw_file").storage
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    raw_name = storage.save(f"{import_dir(test_run)}/{stamp}-{_safe_name(uploaded.name)}", ContentFile(content))
    try:
        with storage.open(raw_name, "rb") as stored:
            if stored.read() != content:
                raise LogImportError(f"{uploaded.name} was not stored correctly; nothing was imported.")
        with transaction.atomic():
            batch = ManualLogImport.objects.create(
                test_run=test_run,
                raw_file=raw_name,
                original_name=uploaded.name[:255],
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                uploaded_by=user,
                **_sheet_fields(source),
            )
            _create_rows(batch, source)
            _propose(batch)
            return batch
    except BaseException:
        storage.delete(raw_name)
        raise


def _sheet_fields(source):
    columns = [c.as_dict() for c in source.columns]
    return {
        "sheet_name": source.name,
        "sheet_names": source.sheet_names,
        "header_row": source.header_row,
        "group_row": source.group_row,
        "columns": columns,
        "ignored_rows": source.ignored_rows,
        "warnings": source.warnings,
        "layout_fingerprint": mapping.fingerprint(columns),
    }


def _create_rows(batch, source):
    ManualLogImportRow.objects.bulk_create(
        ManualLogImportRow(batch=batch, sheet_row=number, cells=cells) for number, cells in source.rows
    )


def _propose(batch):
    proposals = mapping.propose(batch.test_run, batch.columns, columns_with_values(batch))
    batch.layout_map = mapping.find_layout_map(batch.test_run, batch.columns)
    batch.mapping = [p.as_dict() for p in proposals]
    batch.save(update_fields=["layout_map", "mapping"])


def change_sheet(batch, sheet, user):
    """Read another sheet of the same workbook (status Mapping or Preview: back to Mapping)."""
    _check_open(batch)
    with batch.raw_file.open("rb") as stored:
        content = stored.read()
    try:
        source = xlsx.read_sheet(content, sheet)
    except xlsx.WorkbookError as error:
        raise LogImportError(str(error)) from error
    with transaction.atomic():
        batch.rows.all().delete()
        for name, value in _sheet_fields(source).items():
            setattr(batch, name, value)
        batch.status = Status.MAPPING
        batch.mapping_confirmed_by = batch.mapping_confirmed_at = None
        batch.save()
        _create_rows(batch, source)
        _propose(batch)
    return batch


def _check_open(batch):
    if refusal := _refusal(batch.test_run):
        raise LogImportError(refusal)
    if not batch.is_open:
        raise LogImportError(f"This import is {batch.get_status_display().lower()}: it can no longer change.")


# --- mapping → preview -------------------------------------------------------------------------


def proposals_of(batch):
    return [mapping.Proposal(m["letter"], m["target"], m["status"], m.get("reason", "")) for m in batch.mapping]


def confirm_mapping(batch, chosen, reviewed, user, save_layout=True):
    """Confirm the mapping ({letter: target}) and build the preview; raises LogImportError with the problems."""
    _check_open(batch)
    proposals = proposals_of(batch)
    problems = mapping.validate(batch.test_run, batch.columns, chosen, reviewed, proposals)
    if problems:
        raise LogImportError(problems)
    with transaction.atomic():
        confirmed = []
        for proposal in proposals:
            target = chosen.get(proposal.letter, mapping.IGNORE)
            changed = target != proposal.target
            confirmed.append({**proposal.as_dict(), "proposed": proposal.target, "target": target,
                              "reviewed": proposal.status == mapping.REVIEW and not changed
                              and proposal.letter in reviewed, "changed": changed})  # fmt: skip
        batch.mapping = confirmed
        batch.mapping_confirmed_by, batch.mapping_confirmed_at = user, timezone.now()
        if save_layout:
            batch.layout_map = mapping.save_layout_map(batch, batch.columns, chosen, user)
        batch.status = Status.PREVIEW
        batch.save()
        stage(batch)
    return batch


def chosen_mapping(batch):
    return {m["letter"]: m["target"] for m in batch.mapping}


def stage(batch):
    """Interpret, classify and suggest for every row; check the header. Resets earlier decisions."""
    codes = mapping.catalogue(batch.test_run)
    chosen = {letter: target for letter, target in chosen_mapping(batch).items()
              if target not in (mapping.DERIVED, mapping.IGNORE)}  # fmt: skip
    rows = list(batch.rows.all())
    for row in rows:
        row.interpreted = interpret.interpret(row.cells, row.sheet_row, chosen, codes)
        row.source_operator = row.interpreted["operator"][:200]
    _suggest(rows)
    _classify(batch, rows, codes)
    for row in rows:
        row.decision = matching.DEFAULT_DECISION[row.classification]
        row.choices = {}
    ManualLogImportRow.objects.bulk_update(
        rows, ["interpreted", "source_operator", "suggestions", "classification", "matched_entry", "match_details",
               "decision", "choices"],  # fmt: skip
    )


def _local(row):
    at = row.interpreted.get("observed_at")
    return timezone.localtime(datetime.fromisoformat(at)) if at else None


def _suggest(rows):
    timed = [row for row in rows if _local(row)]
    following = {row.pk: (timed[i + 1] if i + 1 < len(timed) else None) for i, row in enumerate(timed)}
    for row in rows:
        at = _local(row)
        nxt = following.get(row.pk)
        row.suggestions = events.suggest(row.interpreted["note"], at, _local(nxt) if nxt else None) if at else {}


def _classify(batch, rows, codes):
    labels = {code: m.full_label for code, m in codes.items()}
    found = matching.classify(batch.test_run, rows, labels, exclude_batch=batch)
    for row in rows:
        row.classification, row.matched_entry, row.match_details = found[row.pk]


# --- the engineer's choices --------------------------------------------------------------------


def save_choices(batch, data, user):
    """Apply the preview form (field → value, see log_import_preview.html) to the rows."""
    _check_open(batch)
    if batch.status != Status.PREVIEW:
        raise LogImportError("Confirm the mapping first.")
    categories = {value for value, _ in ManualLogEntry.Category.choices}
    rows = list(batch.rows.all())
    for row in rows:
        key = f"r{row.pk}"
        decision = data.get(f"{key}_decision", row.decision)
        row.decision = decision if decision in (Decision.IMPORT, Decision.SKIP) else ""
        choices = {}
        category = data.get(f"{key}_category", "")
        choices["category"] = category if category in categories else ""
        if row.interpreted.get("period_options"):
            period = data.get(f"{key}_period")
            choices["period"] = period if period in row.interpreted["period_options"] else None
        chosen_events = []
        for index, suggestion in enumerate(row.suggestions.get("events", [])):
            prefix = f"{key}_e{index}"
            category = data.get(f"{prefix}_category", suggestion["category"])
            chosen_events.append({
                "accepted": bool(data.get(f"{prefix}_accept")),
                "time": (data.get(f"{prefix}_time") or "").strip(),
                "category": category if category in categories else "",
            })  # fmt: skip
        choices["events"] = chosen_events
        row.choices = choices
    ManualLogImportRow.objects.bulk_update(rows, ["decision", "choices"])


def _event_time(row, choice):
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", choice["time"])
    if not match:
        return None
    day = _local(row).date()
    return interpret.aware(datetime(day.year, day.month, day.day, int(match.group(1)), int(match.group(2))))


def problems(batch):
    """What stops the import as chosen now; [] when it can be confirmed."""
    found = []
    rows = list(batch.rows.all())
    importing = [row for row in rows if row.decision == Decision.IMPORT]
    for row in rows:
        if row.classification == Classification.CONFLICT and not row.decision:
            found.append(f"Row {row.sheet_row}: conflicts with an existing reading — choose Import (as an "
                         "additional entry) or Skip.")  # fmt: skip
    for row in importing:
        if row.classification in (Classification.ERROR, Classification.EMPTY):
            found.append(f"Row {row.sheet_row}: {row.get_classification_display().lower()} — it can only be skipped.")
            continue
        if row.interpreted.get("period_options") and row.choices.get("period") is None:
            found.append(f"Row {row.sheet_row}: choose the collection period of its leakage.")
        for index, choice in enumerate(row.choices.get("events", [])):
            if choice["accepted"]:
                if not choice["category"]:
                    found.append(f"Row {row.sheet_row}: choose the category of the separate event.")
                try:
                    at = _event_time(row, choice)
                except interpret.CellError as error:
                    found.append(f"Row {row.sheet_row}: {error}.")
                    continue
                if at is None:
                    found.append(f"Row {row.sheet_row}: enter the time of the separate event as HH:MM.")
                elif batch.test_run.manual_log_entries.filter(
                    observed_at__gte=matching.minute(at), observed_at__lt=matching.minute(at) + timedelta(minutes=1),
                    note=row.suggestions["events"][index]["note"],
                ).exists():
                    found.append(f"Row {row.sheet_row}: the separate event at {choice['time']} is already in the "
                                 "manual log.")  # fmt: skip
    if not importing:
        found.append("No row is selected for import.")
    return found


# --- commit ------------------------------------------------------------------------------------


def commit(batch, user):
    """Create the batch's entries in one transaction; returns the number of entries created.

    Raises StaleImport when the manual log changed since the preview (the rows are classified again and
    the changed rows need a new decision), LogImportError for anything else. Either way nothing is created.
    """
    stale = None
    with transaction.atomic():
        batch = ManualLogImport.objects.select_for_update().get(pk=batch.pk)
        _check_open(batch)
        if batch.status != Status.PREVIEW:
            raise LogImportError("Confirm the mapping first.")
        rows = list(batch.rows.all())
        codes = mapping.catalogue(batch.test_run)
        before = {row.pk: (row.classification, row.matched_entry_id) for row in rows}
        _classify(batch, rows, codes)
        changed = [row for row in rows if (row.classification, row.matched_entry_id) != before[row.pk]]
        if changed:
            for row in changed:
                row.decision = matching.DEFAULT_DECISION[row.classification]
            ManualLogImportRow.objects.bulk_update(changed, ["classification", "matched_entry", "match_details",
                                                             "decision"])  # fmt: skip
            stale = StaleImport([f"The manual log changed since the preview: row {row.sheet_row} is now "
                                 f"{row.get_classification_display().lower()}." for row in changed])  # fmt: skip
        else:
            found = problems(batch)
            if found:
                raise LogImportError(found)
            created = _create_entries(batch, rows, codes, user)
            batch.status = Status.IMPORTED
            batch.imported_by, batch.imported_at = user, timezone.now()
            batch.save()
            return created
    raise stale  # outside the transaction: the new classification is kept for the preview


def _create_entries(batch, rows, codes, user):
    created = 0
    for row in rows:
        if row.decision != Decision.IMPORT:
            continue
        data, choices = row.interpreted, row.choices
        period = choices.get("period") if data.get("period_options") else data.get("leakage_hours")
        entry = ManualLogEntry.objects.create(
            test_run=batch.test_run,
            observed_at=datetime.fromisoformat(data["observed_at"]),
            note=data["note"],
            category=choices.get("category", ""),
            leakage_hours=interpret.Decimal(period) if period else None,
            entered_by=user,
            updated_by=user,
            source_row=row,
        )
        for code, value in data["values"].items():
            ManualMeasurement.objects.create(entry=entry, code=code, value=interpret.Decimal(value),
                                             unit=codes[code].unit)  # fmt: skip
        ids = [entry.pk]
        for suggestion, choice in zip(row.suggestions.get("events", []), choices.get("events", [])):
            if not choice["accepted"]:
                continue
            event = ManualLogEntry.objects.create(
                test_run=batch.test_run, observed_at=_event_time(row, choice), note=suggestion["note"],
                category=choice["category"], entered_by=user, updated_by=user, source_row=row,
            )  # fmt: skip
            ids.append(event.pk)
        row.outcome = {"entries": ids}
        row.save(update_fields=["outcome"])
        created += len(ids)
    return created


# --- undo and discard --------------------------------------------------------------------------


def edited_after_import(batch):
    """Entries of the batch changed in TestLab after the import (they are removed by an undo too)."""
    margin = timedelta(seconds=2)
    return [entry for entry in ManualLogEntry.objects.filter(source_row__batch=batch).order_by("observed_at")
            if entry.updated_at > batch.imported_at + margin or entry.updated_by_id != batch.imported_by_id]


def undo(batch, user, reason, include_edited=False):
    """Remove every entry the batch created; the batch, its rows and its file stay as the record."""
    if not can_undo_log_import(user):
        raise LogImportError("You are not allowed to undo an import.")
    if refusal := _refusal(batch.test_run):
        raise LogImportError(refusal)
    if not reason.strip():
        raise LogImportError("Give the reason for undoing the import.")
    with transaction.atomic():
        batch = ManualLogImport.objects.select_for_update().get(pk=batch.pk)
        if batch.status != Status.IMPORTED:
            raise LogImportError(f"This import is {batch.get_status_display().lower()}: only an imported batch can "
                               "be undone.")  # fmt: skip
        edited = edited_after_import(batch)
        if edited and not include_edited:
            raise LogImportError(f"{len(edited)} imported entr{'y was' if len(edited) == 1 else 'ies were'} edited after "
                               "the import: confirm that the edits are removed too.")  # fmt: skip
        entries = list(ManualLogEntry.objects.filter(source_row__batch=batch))
        details = {
            "removed": [{"entry": e.pk, "observed_at": e.observed_at.isoformat(), "row": e.source_row.sheet_row}
                        for e in entries],  # fmt: skip
            "edited": [{"entry": e.pk, "observed_at": e.observed_at.isoformat(),
                        "updated_by": e.updated_by.get_full_name() or e.updated_by.username,
                        "updated_at": e.updated_at.isoformat()} for e in edited],  # fmt: skip
        }
        for entry in entries:
            entry.delete()
        batch.status = Status.UNDONE
        batch.undone_by, batch.undone_at, batch.undo_reason = user, timezone.now(), reason.strip()
        batch.undo_details = details
        batch.save()
    return len(entries)


def discard(batch):
    """Remove a batch that was never imported, with its stored workbook."""
    _check_open(batch)
    name, storage = batch.raw_file.name, batch.raw_file.storage
    with transaction.atomic():
        batch.delete()
    storage.delete(name)

