"""Seal-face measurements of the Result (M2D): add, review, report.

    + Add measurement (per component)   kind + stage, then the instrument file(s) or the face length
    measurement page                    numbers, figures and settings; recalculate (Draft); review
    reviewed                            figures kept, reported in the summary; reopen to correct

Every change goes through the same lifecycle/authorization check as the
rest of the Result (views.refused) and the measurement rules (seal_faces).
"""

import mimetypes

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from testlab import locks
from testlab.execution.views import run_context

from . import measurement_summary, seal_faces
from .inspection import MeasurementKind, Stage
from .measurement_forms import (
    AddMeasurementChoiceForm,
    DimensionalForm,
    MahrSettingsForm,
    MahrUploadForm,
    RemarkForm,
    RoundpakSettingsForm,
    RoundpakUploadForm,
)
from .models import FaceMeasurement, ResultComponent
from .views import refused, section_url

UPLOAD_FORMS = {
    MeasurementKind.MAHR: MahrUploadForm,
    MeasurementKind.ROUNDPAK: RoundpakUploadForm,
    MeasurementKind.DIMENSIONAL: DimensionalForm,
}


def get_component(pk):
    return get_object_or_404(ResultComponent.objects.select_related("result__test_run__project"), pk=pk)


def get_measurement(pk):
    return get_object_or_404(
        FaceMeasurement.objects.select_related(
            "component__result__test_run__project", "mahr", "roundpak", "dimensional", "created_by", "reviewed_by",
        ).prefetch_related("files", "figures"),
        pk=pk,
    )  # fmt: skip


def measurement_url(measurement):
    return reverse("results:measurement", args=[measurement.pk])


def cards_url(component):
    return section_url(component.result.test_run, f"component-{component.pk}")


class MeasurementAddView(View):
    """Step 1: kind and stage (GET); step 2: the files or the value (POST)."""

    template_name = "results/measurement_add.html"

    def choice(self, request, component):
        data = request.POST if request.method == "POST" else request.GET
        form = AddMeasurementChoiceForm(
            data if data.get("kind") else None, component=component, initial={"stage": Stage.PRE}
        )
        if form.is_bound and form.is_valid():
            return form, form.cleaned_data["kind"], form.cleaned_data["stage"]
        return form, None, None

    def upload_form(self, kind, component, data=None, files=None):
        form_class = UPLOAD_FORMS[MeasurementKind(kind)]
        if form_class is DimensionalForm:
            return DimensionalForm(data, files, component=component)
        form = form_class(data, files)
        if hasattr(form, "initial_from"):
            form.initial_from(component)
        return form

    def get(self, request, pk):
        component = get_component(pk)
        choice, kind, stage = self.choice(request, component)
        upload = self.upload_form(kind, component) if kind else None
        return self.render(request, component, choice, kind, stage, upload)

    def post(self, request, pk):
        component = get_component(pk)
        test_run = component.result.test_run
        if response := refused(request, test_run, "measurements"):
            return response
        choice, kind, stage = self.choice(request, component)
        if kind is None:
            return self.render(request, component, choice, None, None, None)
        form = self.upload_form(kind, component, request.POST, request.FILES)
        if not form.is_valid():
            return self.render(request, component, choice, kind, stage, form)
        c = form.cleaned_data
        try:
            if kind == MeasurementKind.DIMENSIONAL:
                measurement = seal_faces.create_dimensional(
                    component, stage, c["value"], request.user, instrument=c["instrument"], remark=c["remark"],
                    unit=form.cleaned_unit(), feature=c.get("feature", ""),
                )
                messages.success(request, f"{measurement} saved.")
                return redirect(cards_url(component))
            dimensions = {"od": c["od"], "id_": c["id"], "units": c["units"]}
            if kind == MeasurementKind.MAHR:
                uploads = [request.FILES["left_file"], request.FILES["right_file"]]
                measurement = seal_faces.create_mahr(component, stage, *uploads, request.user, **dimensions)
            else:
                uploads = [request.FILES["file"]]
                measurement = seal_faces.create_roundpak(component, stage, *uploads, request.user, **dimensions)
        except seal_faces.MeasurementError as error:
            form.add_error(None, str(error))
            return self.render(request, component, choice, kind, stage, form)
        remember_dimensions(component, c["od"], c["id"], c["units"])
        for warning in seal_faces.duplicate_warnings(measurement):
            messages.warning(request, warning)
        messages.success(request, f"{measurement} calculated as a Draft: check the result and review it.")
        return redirect(measurement_url(measurement))

    def render(self, request, component, choice, kind, stage, upload):
        context = {
            **run_context(component.result.test_run, "result"), "component": component, "choice": choice,
            "kind": kind, "kind_label": component.kind_label(kind) if kind else "",
            "stage": stage, "stage_label": Stage(stage).label if stage else "", "upload": upload,
        }  # fmt: skip
        return render(request, self.template_name, context)


def remember_dimensions(component, od, id_, units):
    """Enter once: face OD/ID typed for a measurement are kept on a component that has none yet."""
    if od is None or id_ is None or component.face_od is not None or component.face_id is not None:
        return
    component.face_od, component.face_id, component.diameter_unit = od, id_, units
    component.save(update_fields=["face_od", "face_id", "diameter_unit"])


class MeasurementView(View):
    """The measurement: source files, results, figures; settings while it is a Draft."""

    template_name = "results/measurement.html"

    def settings_form(self, measurement, data=None):
        if measurement.kind == MeasurementKind.MAHR:
            return MahrSettingsForm.for_analysis(measurement.mahr, data)
        if measurement.kind == MeasurementKind.ROUNDPAK:
            return RoundpakSettingsForm.for_analysis(measurement.roundpak, data)
        return None

    def get(self, request, pk):
        measurement = get_measurement(pk)
        return self.render(request, measurement, self.settings_form(measurement))

    def post(self, request, pk):
        measurement = get_measurement(pk)
        if response := refused(request, measurement.component.result.test_run, "measurements"):
            return response
        form = self.settings_form(measurement, request.POST)
        if form is None:
            raise Http404
        if form.is_valid():
            try:
                if measurement.kind == MeasurementKind.MAHR:
                    seal_faces.update_mahr(measurement, form.settings(), request.user)
                else:
                    seal_faces.update_roundpak(measurement, form.settings(), request.user)
            except seal_faces.MeasurementError as error:
                form.add_error(None, str(error))
            else:
                messages.success(request, "Recalculated with the new settings.")
                return redirect(measurement_url(measurement))
        return self.render(request, measurement, form)

    def render(self, request, measurement, form):
        component = measurement.component
        figures, error = {}, ""
        try:
            figures = seal_faces.stored_figures(measurement) if measurement.is_reviewed else seal_faces.figure_svgs(measurement)
        except seal_faces.MeasurementError as failure:
            error = str(failure)
        siblings = [m for m in component.face_measurements.select_related("mahr", "roundpak", "dimensional")
                    if m.stage == measurement.stage and m.kind == measurement.kind and m.pk != measurement.pk]  # fmt: skip
        context = {
            **run_context(component.result.test_run, "result"),
            "measurement": measurement, "component": component, "analysis": measurement.analysis,
            "settings_form": form, "remark_form": RemarkForm(measurement=measurement),
            "figures": figures, "figure_error": error, "files": list(measurement.files.all()),
            "siblings": siblings, "editable": not locks.result_refusal(component.result.test_run),
            "status": measurement_summary.slot_status(component, measurement.kind, measurement.stage),
        }  # fmt: skip
        return render(request, self.template_name, context)


class MeasurementActionView(View):
    """review · reopen · report (use this measurement for its slot)."""

    def post(self, request, pk, action):
        measurement = get_measurement(pk)
        if response := refused(request, measurement.component.result.test_run, "measurements"):
            return response
        try:
            if action == "review":
                seal_faces.review(measurement, request.user)
                messages.success(request, f"{measurement} reviewed: it is now reported in the summary.")
            elif action == "reopen":
                seal_faces.reopen(measurement, request.user)
                messages.success(request, f"{measurement} reopened as a Draft; it is not reported until reviewed.")
            elif action == "report":
                seal_faces.set_reported(measurement, request.user)
                messages.success(request, f"{measurement} is now the reported measurement.")
            else:
                raise Http404
        except seal_faces.MeasurementError as error:
            messages.error(request, str(error))
        return redirect(request.POST.get("next") or measurement_url(measurement))


class MeasurementOptionsView(View):
    def post(self, request, pk):
        measurement = get_measurement(pk)
        if response := refused(request, measurement.component.result.test_run, "measurements"):
            return response
        form = RemarkForm(request.POST, measurement=measurement)
        if form.is_valid():
            try:
                seal_faces.set_remark(measurement, form.cleaned_data["remark"], request.user)
                messages.success(request, "Remark saved.")
            except seal_faces.MeasurementError as error:
                messages.error(request, str(error))
        else:
            messages.error(request, "Remark not saved: " + " ".join(
                str(e) for errors in form.errors.values() for e in errors))
        return redirect(measurement_url(measurement))


class MeasurementDeleteView(View):
    template_name = "results/measurement_delete.html"

    def get(self, request, pk):
        measurement = get_measurement(pk)
        context = {**run_context(measurement.component.result.test_run, "result"), "measurement": measurement,
                   "files": list(measurement.files.all())}  # fmt: skip
        return render(request, self.template_name, context)

    def post(self, request, pk):
        measurement = get_measurement(pk)
        component = measurement.component
        if response := refused(request, component.result.test_run, "measurements"):
            return response
        label = str(measurement)
        try:
            seal_faces.delete_measurement(measurement, request.user)
        except seal_faces.MeasurementError as error:
            messages.error(request, str(error))
            return redirect(measurement_url(measurement))
        messages.success(request, f"{label} deleted.")
        return redirect(cards_url(component))


class MeasurementFileView(View):
    """An original instrument file, byte for byte."""

    def get(self, request, pk, role):
        measurement = get_measurement(pk)
        stored = next((f for f in measurement.files.all() if f.role == role), None)
        if stored is None:
            raise Http404
        content_type = mimetypes.guess_type(stored.original_name)[0] or "text/plain"
        return FileResponse(stored.file.open("rb"), filename=stored.original_name, content_type=content_type,
                            as_attachment=True)  # fmt: skip
