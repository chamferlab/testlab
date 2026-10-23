"""Manual log → Import Excel → Mapping → Preview → Confirm (M2F); the import record and its undo."""

from datetime import datetime

from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from testlab import locks
from testlab.tcn.permissions import can_undo_log_import

from .forms import LogImportUploadForm
from .log_import import mapping, service
from .models import ManualLogEntry, ManualLogImport
from .views import get_run, run_context

Status = ManualLogImport.Status


def get_batch(pk):
    return get_object_or_404(ManualLogImport.objects.select_related("test_run__project"), pk=pk)


def step_url(batch):
    if batch.status == Status.MAPPING:
        return "execution:log_import_mapping"
    if batch.status == Status.PREVIEW:
        return "execution:log_import_preview"
    return "execution:log_import_detail"


def batch_context(batch, step):
    return {**run_context(batch.test_run, "log"), "batch": batch, "step": step}


class LogImportView(View):
    """Upload a workbook; the run's imports."""

    template_name = "execution/log_import_upload.html"

    def get(self, request, run_pk):
        return self.render(request, get_run(run_pk), LogImportUploadForm())

    def post(self, request, run_pk):
        test_run = get_run(run_pk)
        form = LogImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = service.upload(test_run, form.cleaned_data["file"], request.user)
            except service.LogImportError as error:
                for problem in error.problems:
                    form.add_error("file", problem)
            else:
                return redirect("execution:log_import_mapping", batch.pk)
        return self.render(request, test_run, form)

    def render(self, request, test_run, form):
        imports = test_run.log_imports.select_related("uploaded_by", "imported_by", "undone_by")
        return render(request, self.template_name,
                      {**run_context(test_run, "log"), "form": form, "imports": imports})  # fmt: skip


class LogImportMappingView(View):
    template_name = "execution/log_import_mapping.html"

    def get(self, request, pk):
        batch = get_batch(pk)
        if not batch.is_open:
            return redirect("execution:log_import_detail", batch.pk)
        return self.render(request, batch)

    def post(self, request, pk):
        batch = get_batch(pk)
        try:
            if request.POST.get("action") == "sheet":
                service.change_sheet(batch, request.POST.get("sheet", ""), request.user)
                messages.success(request, f"Sheet “{batch.sheet_name}” read.")
                return redirect("execution:log_import_mapping", batch.pk)
            chosen = {c["letter"]: request.POST.get(f"target_{c['letter']}", mapping.IGNORE) for c in batch.columns}
            reviewed = {c["letter"] for c in batch.columns if request.POST.get(f"reviewed_{c['letter']}")}
            service.confirm_mapping(batch, chosen, reviewed, request.user,
                                    save_layout=bool(request.POST.get("save_layout")))  # fmt: skip
        except service.LogImportError as error:
            return self.render(request, batch, error.problems, request.POST)
        return redirect("execution:log_import_preview", batch.pk)

    def render(self, request, batch, problems=(), data=None):
        test_run = batch.test_run
        rows = list(batch.rows.all()[:200])
        samples = {c["letter"]: [] for c in batch.columns}
        for row in rows:
            for letter, values in samples.items():
                cell = row.cells.get(letter)
                if cell and cell["k"] != "empty" and len(values) < 3:
                    values.append(cell["v"] if "f" not in cell else f"{cell['v'] or '—'} (formula)")
        proposals = {m["letter"]: m for m in batch.mapping}
        columns = []
        for column in batch.columns:
            proposal = proposals.get(column["letter"], {"target": mapping.IGNORE, "status": mapping.REVIEW})
            target = data.get(f"target_{column['letter']}", proposal["target"]) if data else proposal["target"]
            reviewed = bool(data.get(f"reviewed_{column['letter']}")) if data else False
            columns.append({**column, "proposal": proposal, "target": target, "reviewed": reviewed,
                            "status_label": mapping.STATUS_LABELS.get(proposal["status"], ""),
                            "samples": samples[column["letter"]]})  # fmt: skip
        layout = batch.layout_map
        return render(request, self.template_name, {
            **batch_context(batch, "mapping"),
            "columns": columns,
            "choices": mapping.target_choices(test_run),
            "problems": problems,
            "layout": layout,
            "missing": [m for code, m in mapping.catalogue(test_run).items()
                        if code not in {c["target"] for c in columns}],
            "save_layout": True if data is None else bool(data.get("save_layout")),
        })  # fmt: skip


class LogImportPreviewView(View):
    template_name = "execution/log_import_preview.html"

    def get(self, request, pk):
        batch = get_batch(pk)
        if batch.status != Status.PREVIEW:
            return redirect(step_url(batch), batch.pk)
        return self.render(request, batch)

    def post(self, request, pk):
        batch = get_batch(pk)
        try:
            service.save_choices(batch, request.POST, request.user)
            if request.POST.get("action") != "import":
                return redirect("execution:log_import_preview", batch.pk)
            created = service.commit(batch, request.user)
        except service.StaleImport as error:
            return self.render(request, get_batch(pk), error.problems)
        except service.LogImportError as error:
            return self.render(request, get_batch(pk), error.problems)
        messages.success(request, f"{created} entr{'y' if created == 1 else 'ies'} imported from "
                                  f"{batch.original_name}.")  # fmt: skip
        return redirect("execution:log", batch.test_run.pk)

    def render(self, request, batch, problems=()):
        context = preview_context(batch)
        return render(request, self.template_name, {**batch_context(batch, "preview"), **context,
                                                    "problems": problems})  # fmt: skip


def preview_context(batch):
    """Rows with their values in the log's columns, for the preview and the import record."""
    codes = mapping.catalogue(batch.test_run)
    chosen = service.chosen_mapping(batch)
    mapped = {target for target in chosen.values()}
    columns = [m for code, m in codes.items() if code in mapped]
    letters = {target: letter for letter, target in chosen.items()}
    rows = []
    counts = {}
    for row in batch.rows.select_related("matched_entry"):
        counts[row.classification] = counts.get(row.classification, 0) + 1
        data = row.interpreted
        cells = [{"code": m.code, "value": data.get("values", {}).get(m.code),
                  "ref": f"{letters.get(m.code, '')}{row.sheet_row}"} for m in columns]  # fmt: skip
        events = []
        for index, suggestion in enumerate(row.suggestions.get("events", [])):
            chosen = row.choices.get("events") or []
            choice = chosen[index] if index < len(chosen) else {}
            events.append({"index": index, **suggestion, "accepted": choice.get("accepted", False),
                           "chosen_time": choice.get("time", suggestion["time"]),
                           "chosen_category": choice.get("category", suggestion["category"])})  # fmt: skip
        rows.append({"row": row, "data": data, "cells": cells, "events": events,
                     "at": _local(data.get("observed_at")),
                     "match_at": _local(row.match_details.get("observed_at")),
                     "category": row.choices.get("category", ""), "period": row.choices.get("period"),
                     "outcome": _outcome(batch, row)})  # fmt: skip
    order = ["new", "duplicate", "conflict", "error", "empty"]
    return {
        "columns": columns,
        "rows": rows,
        "counts": [(key, counts[key]) for key in order if counts.get(key)],
        "categories": ManualLogEntry.Category.choices,
        "category_labels": dict(ManualLogEntry.Category.choices),
        "to_import": sum(1 for r in rows if r["row"].decision == "import"),
        "mapping_rows": [{**m, "label": mapping.target_label(batch.test_run, m["target"]),
                          "column": next((c for c in batch.columns if c["letter"] == m["letter"]), {})}
                         for m in batch.mapping],  # fmt: skip
    }


def _local(iso):
    return timezone.localtime(datetime.fromisoformat(iso)) if iso else None


def _outcome(batch, row):
    entries = row.outcome.get("entries", [])
    if not entries:
        return "skipped" if batch.status in (Status.IMPORTED, Status.UNDONE) else ""
    text = "imported" + (f" + {len(entries) - 1} event{'s' if len(entries) > 2 else ''}" if len(entries) > 1 else "")
    return text + (", undone" if batch.status == Status.UNDONE else "")


class LogImportDetailView(View):
    """The import record: workbook, mapping copy, run check, rows and what became of them."""

    template_name = "execution/log_import_detail.html"

    def get(self, request, pk):
        batch = get_batch(pk)
        return render(request, self.template_name, {
            **batch_context(batch, "record"), **preview_context(batch),
            "can_undo": batch.status == Status.IMPORTED and can_undo_log_import(request.user)
            and not locks.result_refusal(batch.test_run),
        })  # fmt: skip


class LogImportUndoView(View):
    template_name = "execution/log_import_undo.html"

    def get(self, request, pk):
        return self.render(request, get_batch(pk))

    def post(self, request, pk):
        batch = get_batch(pk)
        try:
            removed = service.undo(batch, request.user, request.POST.get("reason", ""),
                                   include_edited=bool(request.POST.get("include_edited")))  # fmt: skip
        except service.LogImportError as error:
            return self.render(request, batch, error.problems, request.POST.get("reason", ""))
        messages.success(request, f"Import of {batch.original_name} undone: {removed} entr"
                                  f"{'y' if removed == 1 else 'ies'} removed. The import record is kept.")  # fmt: skip
        return redirect("execution:log_import_detail", batch.pk)

    def render(self, request, batch, problems=(), reason=""):
        return render(request, self.template_name, {
            **batch_context(batch, "record"), "problems": problems, "reason": reason,
            "edited": service.edited_after_import(batch) if batch.status == Status.IMPORTED else [],
            "entry_count": sum(len(r.outcome.get("entries", [])) for r in batch.rows.all()),
            "allowed": can_undo_log_import(request.user),
        })  # fmt: skip


class LogImportDiscardView(View):
    def post(self, request, pk):
        batch = get_batch(pk)
        run_pk, name = batch.test_run.pk, batch.original_name
        try:
            service.discard(batch)
        except service.LogImportError as error:
            for problem in error.problems:
                messages.error(request, problem)
            return redirect(step_url(batch), batch.pk)
        messages.success(request, f"Import of {name} discarded; nothing was imported.")
        return redirect("execution:log_import", run_pk)


class LogImportFileView(View):
    def get(self, request, pk):
        batch = get_batch(pk)
        return FileResponse(batch.raw_file.open("rb"), as_attachment=True, filename=batch.original_name)
