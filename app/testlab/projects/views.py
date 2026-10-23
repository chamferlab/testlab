import mimetypes

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max, Prefetch, ProtectedError, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from testlab.tcn.models import TcnRevision

from .arrangements import seal_support_label
from . import drawings
from .forms import DrawingUploadForm, ProgramRequirementForm, ProjectForm, TestProgramForm
from .models import ProgramRequirement, Project, ProjectDrawing, TestProgram, project_no_format_warning
from .setup_forms import RunSetupEditor


class ProjectListView(ListView):
    """S2: engineering overview, read from each project's current defaults."""

    model = Project
    context_object_name = "projects"

    def get_queryset(self):
        # Explicit order: Meta.ordering is not applied to aggregated querysets.
        projects = (
            super()
            .get_queryset()
            .select_related("default_setup")
            .prefetch_related("default_setup__positions")
            .annotate(tcn_count=Count("test_runs"))
            .order_by("-project_no")
        )
        self.query = self.request.GET.get("q", "").strip()
        if self.query:
            projects = projects.filter(
                Q(project_no__icontains=self.query)
                | Q(description__icontains=self.query)
                | Q(customer__icontains=self.query)
            )
        return projects

    def get_context_data(self, **kwargs):
        context = super().get_context_data(query=self.query, **kwargs)
        context["rows"] = [
            {
                "project": project,
                "setup": project.default_setup,
                "seal_type": seal_type_summary(project.default_setup),
                "seal_support_label": seal_support_label(project.default_setup.arrangement),
            }
            for project in context["projects"]
        ]
        return context


def seal_type_summary(setup):
    """Seal type and size of the seals under test, e.g. "UHTW 4.750" or "UHTW 4.750 / DHTW 4.750".

    The Plug Seal is test equipment, not the seal under test, so it is left
    out. Equal values are shown once; empty positions are skipped.
    """
    values = []
    for position in setup.seal_under_test_positions():
        if position.seal_type_size and position.seal_type_size not in values:
            values.append(position.seal_type_size)
    return " / ".join(values)


class ProjectDetailView(DetailView):
    """S4: project identity, defaults summary and TCN register."""

    model = Project
    context_object_name = "project"

    def get_context_data(self, **kwargs):
        runs = self.object.test_runs.prefetch_related(
            Prefetch("revisions", queryset=TcnRevision.objects.select_related("setup"))
        )
        register = []
        for run in runs:
            current = run.current_revision()
            draft = next((r for r in run.revisions.all() if r.status == TcnRevision.Status.DRAFT), None)
            register.append(
                {
                    "tcn": current,
                    "setup": current.setup,
                    "seal_support_label": seal_support_label(current.setup.arrangement),
                    "pending_draft": draft if draft != current else None,
                }
            )
        return super().get_context_data(register=register, drawing_sets=drawings.drawing_sets(self.object), **kwargs)


class ProjectFormMixin:
    model = Project
    form_class = ProjectForm

    def get_success_url(self):
        return reverse("projects:detail", args=[self.object.pk])

    def form_valid(self, form):
        if form.instance.pk is None:
            form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Project {self.object.project_no} saved.")
        warning = project_no_format_warning(self.object.project_no)
        if warning:
            messages.warning(self.request, warning)
        return response


class ProjectCreateView(ProjectFormMixin, CreateView):
    def get_success_url(self):
        # S3 → S5: continue with the project defaults.
        return reverse("projects:defaults", args=[self.object.pk])


class ProjectUpdateView(ProjectFormMixin, UpdateView):
    pass


class ProjectDefaultsView(View):
    """S5: project defaults, edited with the shared run setup form."""

    template_name = "projects/project_defaults.html"

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        return self.render(request, project, RunSetupEditor(project.default_setup))

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        editor = RunSetupEditor(project.default_setup, request.POST)
        if request.POST.get("action") == "save":
            editor.show_errors = True
            if editor.is_valid():
                if editor.discards() and not request.POST.get("confirm_discard"):
                    editor.confirmation_required = True
                else:
                    with transaction.atomic():
                        editor.save()
                        project.updated_by = request.user
                        project.save(update_fields=["updated_by", "updated_at"])
                    for notice in editor.notices:
                        messages.info(request, notice)
                    messages.success(request, f"Project defaults of {project.project_no} saved.")
                    return redirect("projects:defaults", pk=project.pk)
        return self.render(request, project, editor)

    def render(self, request, project, editor, upload_forms=None):
        upload_forms = upload_forms or {}
        sets = drawings.drawing_sets(project)
        for entry in sets:
            current = entry["current"]
            initial = {"drawing_no": current.drawing_no} if current else None
            entry["form"] = upload_forms.get(entry["kind"]) or DrawingUploadForm(kind=entry["kind"], initial=initial)
        return render(request, self.template_name, {"project": project, "editor": editor, "drawing_sets": sets})


# ---- Project drawings: tester drawing and system P&ID (Project Defaults) ----------------------


def drawings_url(project):
    return reverse("projects:defaults", args=[project.pk]) + "#drawings"


class DrawingUploadView(View):
    def post(self, request, pk, kind):
        project = get_object_or_404(Project, pk=pk)
        if kind not in ProjectDrawing.Kind.values:
            return redirect(drawings_url(project))
        form = DrawingUploadForm(request.POST, request.FILES, kind=kind)
        if form.is_valid():
            try:
                drawing = drawings.add_drawing(
                    project, kind, form.cleaned_data["file"], request.user,
                    form.cleaned_data["drawing_no"], form.cleaned_data["revision"],
                )  # fmt: skip
            except drawings.DrawingUploadError as error:
                form.add_error("file", str(error))
            else:
                messages.success(request, f"{drawing.get_kind_display()} {drawing} added.")
                return redirect(drawings_url(project))
        view = ProjectDefaultsView()
        return view.render(request, project, RunSetupEditor(project.default_setup), upload_forms={kind: form})


def get_drawing(pk):
    return get_object_or_404(ProjectDrawing.objects.select_related("project"), pk=pk)


class DrawingFileView(View):
    """The drawing as uploaded, byte for byte (inline, or ?download=1), or the display copy of a picture."""

    preview = False

    def get(self, request, pk):
        drawing = get_drawing(pk)
        if self.preview and not drawing.is_pdf:
            name = drawings.preview_name(drawing)
            return FileResponse(drawing.file.storage.open(name, "rb"), content_type="image/jpeg")
        content_type = mimetypes.guess_type(drawing.original_name)[0] or "application/octet-stream"
        return FileResponse(
            drawing.file.open("rb"), filename=drawing.original_name, content_type=content_type,
            as_attachment=request.GET.get("download") == "1",
        )  # fmt: skip


class DrawingDeleteView(View):
    def post(self, request, pk):
        drawing = get_drawing(pk)
        project = drawing.project
        label = f"{drawing.get_kind_display()} {drawing}"
        try:
            drawings.delete_drawing(drawing)
        except drawings.DrawingUploadError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f"{label} deleted.")
        return redirect(drawings_url(project))


# ---- Test Program: title, objective, baseline acceptance requirements --------------------------


def program_url(project, anchor=""):
    return reverse("projects:program", args=[project.pk]) + (f"#{anchor}" if anchor else "")


class ProgramView(View):
    """The project's Test Program as TestLab uses it now: title, objective and acceptance requirements."""

    template_name = "projects/program.html"

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        program = TestProgram.objects.filter(project=project).first()
        return self.render(request, project, program, TestProgramForm(instance=program), ProgramRequirementForm())

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        program = TestProgram.objects.filter(project=project).first()
        form = TestProgramForm(request.POST, instance=program)
        if form.is_valid():
            program = form.save(commit=False)
            program.project = project
            program.updated_by = request.user
            program.save()
            messages.success(request, "Test Program saved.")
            return redirect(program_url(project))
        return self.render(request, project, program, form, ProgramRequirementForm())

    def render(self, request, project, program, form, requirement_form):
        requirements = list(program.requirements.all()) if program else []
        from testlab.tcn import requirements as tcn_requirements

        context = {"project": project, "program": program, "form": form, "requirements": requirements,
                   "requirement_form": requirement_form,
                   "outdated_tcns": tcn_requirements.outdated(project)}  # fmt: skip
        return render(request, self.template_name, context)


class ProgramRequirementAddView(View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        with transaction.atomic():
            program, _ = TestProgram.objects.get_or_create(project=project, defaults={"updated_by": request.user})
            requirement = ProgramRequirement(program=program, updated_by=request.user)
            form = ProgramRequirementForm(request.POST, instance=requirement)
            if form.is_valid():
                requirement = form.save(commit=False)
                requirement.number = (program.requirements.aggregate(m=Max("number"))["m"] or 0) + 1
                requirement.save()
                messages.success(request, f"Requirement {requirement.ref} added.")
                return redirect(program_url(project, "requirements"))
        return ProgramView().render(request, project, program, TestProgramForm(instance=program), form)


class ProgramRequirementEditView(View):
    template_name = "projects/program_requirement.html"

    def get(self, request, pk):
        requirement = get_object_or_404(ProgramRequirement.objects.select_related("program__project"), pk=pk)
        return self.render(request, requirement, ProgramRequirementForm(instance=requirement))

    def post(self, request, pk):
        requirement = get_object_or_404(ProgramRequirement.objects.select_related("program__project"), pk=pk)
        project = requirement.program.project
        if request.POST.get("delete"):
            try:
                requirement.delete()
            except ProtectedError:
                messages.error(request, f"{requirement.ref} is changed by a TCN and cannot be deleted.")
                return redirect("projects:program_requirement", requirement.pk)
            messages.success(request, f"Requirement {requirement.ref} deleted.")
            return redirect(program_url(project, "requirements"))
        form = ProgramRequirementForm(request.POST, instance=requirement)
        if form.is_valid():
            requirement = form.save(commit=False)
            requirement.updated_by = request.user
            requirement.save()
            messages.success(request, f"Requirement {requirement.ref} saved.")
            return redirect(program_url(project, "requirements"))
        return self.render(request, requirement, form)

    def render(self, request, requirement, form):
        context = {"project": requirement.program.project, "program": requirement.program,
                   "requirement": requirement, "form": form}  # fmt: skip
        return render(request, self.template_name, context)
