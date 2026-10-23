"""Test execution pages of a Test Run (M2A): Manual log and DCS data."""

from datetime import datetime

from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from testlab import locks
from testlab.tcn.models import TestRun

from . import datasets, manual_log, running_intervals, signal_mapping
from .forms import DcsUploadForm, ManualLogEntryForm, plain_number
from .models import DcsDataset, ManualLogEntry


def get_run(pk):
    return get_object_or_404(TestRun.objects.select_related("project"), pk=pk)


def run_context(test_run, tab):
    """Context of a run page. The Result and the execution evidence are locked together (locks.result_lock):
    `recording_refused` (what the tab edits) and `result_refused` are the same refusal."""
    refused = locks.result_refusal(test_run)
    return {
        "run": test_run,
        "project": test_run.project,
        "tcn": test_run.current_revision(),
        "tab": tab,
        "recording_refused": refused,
        "result_refused": refused,
    }


class ManualLogView(View):
    """The log of a run: quick entry form on top, readings below (newest first)."""

    template_name = "execution/manual_log.html"

    def get(self, request, run_pk):
        test_run = get_run(run_pk)
        return self.render(request, test_run)

    def post(self, request, run_pk):
        test_run = get_run(run_pk)
        if locks.result_refusal(test_run):
            messages.error(request, locks.result_refusal(test_run))
            return redirect("execution:log", test_run.pk)
        entries = list(test_run.manual_log_entries.prefetch_related("measurements"))
        columns = manual_log.log_columns(test_run, entries)
        form = ManualLogEntryForm(request.POST, columns=columns)
        if form.is_valid():
            entry = manual_log.save_entry(test_run, form.cleaned_data, columns, request.user)
            messages.success(request, f"Reading of {entry.observed_at:%d-%b-%Y %H:%M} added.")
            return redirect(reverse("execution:log", args=[test_run.pk]) + "#add")
        return self.render(request, test_run, form)

    def render(self, request, test_run, form=None):
        entries = list(test_run.manual_log_entries.prefetch_related("measurements"))
        columns = manual_log.log_columns(test_run, entries)
        origin = manual_log.timeline_origin(test_run)
        rows = manual_log.log_rows(test_run, columns, origin)
        if form is None:
            form = ManualLogEntryForm(columns=columns, previous=rows[-1] if rows else None)
        return render(
            request,
            self.template_name,
            {
                **run_context(test_run, "log"),
                "form": form,
                "columns": columns,
                "groups": [
                    (name, [(column, form[column.code]) for column in group])
                    for name, group in manual_log.column_groups(columns)
                ],
                # The table shows only what has been logged; the form offers every measurement.
                "table_groups": [
                    (name, group, sum(3 if column.is_leakage else 1 for column in group))
                    for name, group in manual_log.column_groups(
                        [c for c in columns if any(c.code in row.values for row in rows)]
                    )
                ],
                "instructed": manual_log.instructed_codes(test_run),
                "rows": list(reversed(rows)),
                "has_leakage": any(c.is_leakage for c in columns),
                "origin": origin,
            },
        )


class EntryMixin:
    def get_entry(self, pk):
        return get_object_or_404(ManualLogEntry.objects.select_related("test_run__project"), pk=pk)


class EntryEditView(EntryMixin, View):
    template_name = "execution/entry_edit.html"

    def form(self, entry, columns, data=None):
        initial = {
            "observed_at": entry.observed_at,
            "note": entry.note,
            "category": entry.category,
            "leakage_hours": entry.leakage_hours,
            **{value.code: plain_number(value.value) for value in entry.measurements.all()},
        }
        return ManualLogEntryForm(data, columns=columns, initial=initial)

    def get(self, request, pk):
        entry = self.get_entry(pk)
        columns = manual_log.log_columns(entry.test_run, [entry])
        return self.render(request, entry, self.form(entry, columns))

    def post(self, request, pk):
        entry = self.get_entry(pk)
        if refusal := locks.result_refusal(entry.test_run):
            messages.error(request, refusal)
            return redirect("execution:log", entry.test_run.pk)
        columns = manual_log.log_columns(entry.test_run, [entry])
        form = self.form(entry, columns, request.POST)
        if form.is_valid():
            manual_log.save_entry(entry.test_run, form.cleaned_data, columns, request.user, entry=entry)
            messages.success(request, f"Reading of {entry.observed_at:%d-%b-%Y %H:%M} saved.")
            return redirect("execution:log", entry.test_run.pk)
        return self.render(request, entry, form)

    def render(self, request, entry, form):
        groups = [
            (name, [(column, form[column.code]) for column in group])
            for name, group in manual_log.column_groups(form.columns)
        ]
        return render(
            request,
            self.template_name,
            {**run_context(entry.test_run, "log"), "entry": entry, "form": form, "groups": groups,
             "instructed": manual_log.instructed_codes(entry.test_run)},
        )


class EntryDeleteView(EntryMixin, View):
    template_name = "execution/entry_delete.html"

    def get(self, request, pk):
        entry = self.get_entry(pk)
        return render(request, self.template_name, {**run_context(entry.test_run, "log"), "entry": entry})

    def post(self, request, pk):
        entry = self.get_entry(pk)
        run_pk = entry.test_run.pk
        if refusal := locks.result_refusal(entry.test_run):
            messages.error(request, refusal)
            return redirect("execution:log", run_pk)
        when = f"{entry.observed_at:%d-%b-%Y %H:%M}"
        entry.delete()
        messages.success(request, f"Reading of {when} deleted.")
        return redirect("execution:log", run_pk)


class DcsListView(View):
    """Datasets of a run, with the import form."""

    template_name = "execution/dcs_list.html"

    def get(self, request, run_pk):
        return self.render(request, get_run(run_pk), DcsUploadForm())

    def post(self, request, run_pk):
        test_run = get_run(run_pk)
        if locks.result_refusal(test_run):
            messages.error(request, locks.result_refusal(test_run))
            return redirect("execution:dcs", test_run.pk)
        form = DcsUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                dataset = datasets.import_dataset(test_run, form.cleaned_data["file"], request.user)
            except datasets.DatasetImportError as error:
                form.add_error("file", str(error))
            else:
                messages.success(request, f"{dataset.original_name} imported: {dataset.sample_count} samples.")
                return redirect("execution:dcs_detail", dataset.pk)
        return self.render(request, test_run, form)

    def render(self, request, test_run, form):
        return render(
            request,
            self.template_name,
            {**run_context(test_run, "dcs"), "form": form, "datasets": test_run.dcs_datasets.select_related("imported_by")},
        )


def get_dataset(pk):
    return get_object_or_404(DcsDataset.objects.select_related("test_run__project", "imported_by"), pk=pk)


class DcsDetailView(View):
    template_name = "execution/dcs_detail.html"

    def get(self, request, pk):
        return self.render(request, get_dataset(pk))

    def post(self, request, pk):
        """Review the signal mapping: mapping_<column> = parameter code, unit_<column> = confirmed."""
        dataset = get_dataset(pk)
        if refusal := locks.result_refusal(dataset.test_run):
            messages.error(request, refusal)
            return redirect("execution:dcs_detail", dataset.pk)
        chosen, confirmed = {}, set()
        for signal in dataset.value_signals():
            column = signal["column"]
            chosen[column] = request.POST.get(f"mapping_{column}", "")
            if request.POST.get(f"unit_{column}"):
                confirmed.add(column)
        try:
            signal_mapping.review_mapping(dataset, chosen, confirmed, request.user)
        except signal_mapping.MappingError as error:
            messages.error(request, str(error))
            return self.render(request, dataset, chosen=chosen, confirmed=confirmed)
        messages.success(request, "Signal mapping reviewed; it is reused for later imports of the same format.")
        if running_intervals.ensure_current(dataset, request.user) is not None:
            messages.info(request, "Running intervals of the speed signal: review them below.")
        return redirect(reverse("execution:dcs_detail", args=[dataset.pk]) + "#mapping")

    def render(self, request, dataset, chosen=None, confirmed=()):
        signals = [
            {**signal, "label": f"{signal['name']} [{signal['unit']}]" if signal["unit"] else signal["name"]}
            for signal in dataset.value_signals()
        ]
        offered = signal_mapping.parameters(dataset.test_run)
        suggested = chosen if chosen is not None else signal_mapping.suggestions(dataset)
        stored = dataset.mapping
        mapping_rows = []
        for signal in signals:
            column = signal["column"]
            code = suggested.get(column, "")
            entry = stored.get(str(column), {})
            parameter = offered.get(code)
            mapping_rows.append({
                "signal": signal, "code": code, "parameter": parameter,
                "unit_differs": bool(parameter) and not signal_mapping.units_match(signal["unit"], parameter.unit),
                "unit_confirmed": column in confirmed or entry.get("unit_confirmed", False),
            })  # fmt: skip
        return render(
            request,
            self.template_name,
            {
                **run_context(dataset.test_run, "dcs"),
                "dataset": dataset,
                "signals": signals,
                "time_signal": dataset.signals[dataset.time_column],
                "origin": manual_log.timeline_origin(dataset.test_run),
                "mapping_rows": mapping_rows,
                "parameters": list(offered.values()),
                "known_map": signal_mapping.known_map(dataset),
                **running_context(dataset),
            },
        )


# ---- running intervals -----------------------------------------------------------------------

DATETIME_INPUT = "%Y-%m-%dT%H:%M:%S"


def _when(dataset, hours):
    moment = running_intervals.moment(dataset, hours)
    return timezone.localtime(moment) if moment else None


def _interval_rows(dataset, intervals):
    return [
        {
            "start_h": i["start_h"], "end_h": i["end_h"], "hours": i["end_h"] - i["start_h"],
            "start": _when(dataset, i["start_h"]), "end": _when(dataset, i["end_h"]),
            "notes": running_intervals.edge_notes(i),
        }
        for i in intervals
    ]  # fmt: skip


def _input_value(dataset, hours):
    """What the edit form shows for a time: local date and time to the second, else elapsed hours."""
    moment = _when(dataset, hours)
    return moment.strftime(DATETIME_INPUT) if moment else f"{hours:.6f}"


def _spans(dataset, items):
    return [{"start": _when(dataset, a), "end": _when(dataset, b), "start_h": a, "end_h": b,
             "seconds": (b - a) * 3600} for a, b in items]  # fmt: skip


def running_context(dataset):
    found = running_intervals.state(dataset)
    d = found.determination
    context = {"running": found, "running_history": [h for h in running_intervals.history(dataset)
                                                      if not h.is_current]}  # fmt: skip
    if d is None:
        return context
    detected = d.detected.get("intervals", [])
    context.update({
        "running_intervals": _interval_rows(dataset, d.effective_intervals),
        "detected_intervals": _interval_rows(dataset, detected) if d.effective_intervals != detected else [],
        "running_dropouts": _spans(dataset, d.detected.get("dropouts", [])),
        "running_spikes": _spans(dataset, d.detected.get("spikes", [])),
        "running_gaps": _spans(dataset, d.detected.get("gaps", [])),
        "running_edit_rows": [
            {"start": _input_value(dataset, i["start_h"]), "end": _input_value(dataset, i["end_h"]),
             "start_h": i["start_h"], "end_h": i["end_h"]}
            for i in d.effective_intervals
        ] + [{"start": "", "end": "", "start_h": "", "end_h": ""}] * 2,
        "running_recording_end": _when(dataset, dataset.time_end_h),
        "running_recording_start": _when(dataset, dataset.time_start_h),
        "running_plot": {"intervals": [[a, b] for a, b in running_intervals.clipped(d.effective_intervals,
                                                                                   found.coverage)],
                         "provisional": found.provisional},
    })  # fmt: skip
    return context


def _parse_time(dataset, text, shown, shown_hours):
    """Hours on the recording's axis from the edit form: a local date/time, or elapsed hours."""
    text = (text or "").strip()
    if shown_hours not in (None, "") and text == shown:
        return float(shown_hours)  # unchanged: the stored value, not its display rounded to the second
    try:
        return float(text.replace(",", "."))
    except ValueError:
        pass
    if dataset.recording_started_at is None:
        raise running_intervals.DeterminationError(f"“{text}” is not a number of hours.")
    for pattern in (DATETIME_INPUT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            moment = timezone.make_aware(datetime.strptime(text, pattern))
        except ValueError:
            continue
        return (moment - dataset.recording_started_at).total_seconds() / 3600
    raise running_intervals.DeterminationError(f"“{text}” is not a date and time (or a number of hours).")


def _entered_intervals(dataset, post):
    found = []
    index = 0
    while f"start_{index}" in post:
        start, end = post.get(f"start_{index}", "").strip(), post.get(f"end_{index}", "").strip()
        if start or end:
            if not (start and end):
                raise running_intervals.DeterminationError("Every interval needs a start and an end; "
                                                           "clear both to remove one.")  # fmt: skip
            found.append((
                _parse_time(dataset, start, post.get(f"shown_start_{index}"), post.get(f"start_h_{index}")),
                _parse_time(dataset, end, post.get(f"shown_end_{index}"), post.get(f"end_h_{index}")),
            ))  # fmt: skip
        index += 1
    return found


def _setting(post, name):
    try:
        return float(post.get(name, "").replace(",", "."))
    except ValueError as error:
        raise running_intervals.DeterminationError("Enter the threshold, dropout and spike limits as numbers.") \
            from error


class DcsRunningView(View):
    """Confirm, edit (override) or redetect the running intervals of a dataset."""

    def post(self, request, pk):
        dataset = get_dataset(pk)
        target = reverse("execution:dcs_detail", args=[dataset.pk]) + "#running"
        if refusal := locks.result_refusal(dataset.test_run):
            messages.error(request, refusal)
            return redirect(target)
        action, shown = request.POST.get("action"), request.POST.get("determination", "")
        try:
            if action == "confirm":
                running_intervals.confirm(dataset, request.user, shown)
                messages.success(request, "Running intervals confirmed.")
            elif action == "override":
                running_intervals.override(dataset, _entered_intervals(dataset, request.POST),
                                           request.POST.get("reason", ""), request.user, shown)  # fmt: skip
                messages.success(request, "Running intervals changed; the detected ones are kept beside them.")
            elif action == "redetect":
                settings_ = running_intervals.Settings(
                    _setting(request.POST, "threshold_rpm"), _setting(request.POST, "max_dropout_s"),
                    _setting(request.POST, "min_run_s"),
                )  # fmt: skip
                running_intervals.redetect(dataset, settings_, request.user, request.POST.get("reason", ""), shown)
                messages.success(request, "Running intervals detected again: review and confirm them.")
            else:
                raise Http404("Unknown action")
        except running_intervals.DeterminationError as error:
            messages.error(request, str(error))
        return redirect(target)


def _float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as error:
        raise Http404("Invalid number") from error


class DcsDataView(View):
    """Plot data as JSON: ?columns=1,2&start=0.5&end=3.0&points=4000 (hours)."""

    def get(self, request, pk):
        dataset = get_dataset(pk)
        columns = request.GET.get("columns")
        try:
            wanted = None if columns in (None, "") else {int(c) for c in columns.split(",")}
            points = min(max(int(request.GET.get("points", datasets.DEFAULT_PLOT_POINTS)), 100), 20000)
        except ValueError as error:
            raise Http404("Invalid parameter") from error
        data = datasets.plot_data(
            dataset, wanted, _float(request.GET.get("start")), _float(request.GET.get("end")), points
        )
        return JsonResponse(data)


class DcsRawView(View):
    """The imported file, byte for byte, under its original name."""

    def get(self, request, pk):
        dataset = get_dataset(pk)
        return FileResponse(
            dataset.raw_file.open("rb"), as_attachment=True, filename=dataset.original_name,
            content_type="application/octet-stream",
        )
