from django import forms

from testlab import locks

from .arrangements import SealRole
from .models import (
    REQUIREMENT_FACES,
    ProgramRequirement,
    Project,
    RequirementBasis,
    RequirementComparison,
    TestProgram,
    normalize_project_no,
)


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["project_no", "description", "customer"]
        widgets = {
            "description": forms.TextInput(attrs={"size": 60}),
            "customer": forms.TextInput(attrs={"size": 60}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if locks.project_no_lock(self.instance):
            # V2: shown read-only; a disabled field ignores posted values.
            field = self.fields["project_no"]
            field.disabled = True
            field.help_text = "Locked: a TCN of this project has been issued."

    def clean_project_no(self):
        # Normalise before the model's unique check so "2099rsd01tl" and
        # "2099RSD01TL" are the same project.
        return normalize_project_no(self.cleaned_data["project_no"])


class LimitField(forms.DecimalField):
    """A numeric limit: decimal point or comma, scientific notation allowed (5E-6)."""

    widget = forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off", "class": "reading"})

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return super().to_python(value)


AUTO_UNITS = {
    RequirementBasis.LEAKAGE_RATE: "ml/h",
    RequirementBasis.WEAR_RATE: "in/h",
    RequirementBasis.RUNNING_HOURS: "h",
    RequirementBasis.LOGGED_HOURS: "h",
}


class RequirementFieldsForm(forms.ModelForm):
    """What an acceptance requirement says (Test Program, TCN change). Operating set points are not
    requirements: only a limit or wording entered here is assessed."""

    limit = LimitField(label="Limit", required=False, max_digits=24, decimal_places=12)
    REQUIREMENT_FIELDS = ["criterion", "basis", "position", "face", "comparison", "limit", "unit", "requirement_text"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["comparison"].choices = [("", "— none —")] + RequirementComparison.choices
        self.fields["position"].choices = [("", "each seal under test")] + list(SealRole.choices)
        self.fields["face"].choices = [("", "both faces")] + REQUIREMENT_FACES
        self.fields["unit"].help_text = "e.g. ml/h, cc/h, in/h, mm/h, h. A mass rate (g/h) is assessed by the engineer."
        if self.instance.pk and self.instance.limit is not None:
            self.initial["limit"] = format(self.instance.limit.normalize(), "f")

    def clean(self):
        cleaned = super().clean()
        basis = cleaned.get("basis")
        if basis not in (RequirementBasis.LEAKAGE_RATE, RequirementBasis.WEAR_RATE):
            cleaned["position"] = ""
        if basis != RequirementBasis.WEAR_RATE:
            cleaned["face"] = ""
        if basis in AUTO_UNITS and cleaned.get("comparison") and not cleaned.get("unit"):
            cleaned["unit"] = AUTO_UNITS[basis]
        comparison, limit = cleaned.get("comparison"), cleaned.get("limit")
        if comparison and limit is None:
            self.add_error("limit", "Enter the limit.")
        if limit is not None and not comparison:
            self.add_error("comparison", "Choose ≤ or ≥ for the limit.")
        if not comparison and not cleaned.get("requirement_text"):
            self.add_error("requirement_text", "Enter the requirement: a limit or a wording.")
        if not cleaned.get("criterion"):
            self.add_error("criterion", "Name the criterion, e.g. Leakage, Face wear, Test duration.")
        return cleaned


class TestProgramForm(forms.ModelForm):
    class Meta:
        model = TestProgram
        fields = ["document_no", "revision", "title", "objective"]
        widgets = {"title": forms.TextInput(attrs={"size": 60}), "objective": forms.Textarea(attrs={"rows": 4})}


class ProgramRequirementForm(RequirementFieldsForm):
    class Meta:
        model = ProgramRequirement
        fields = RequirementFieldsForm.REQUIREMENT_FIELDS
        labels = {"comparison": "Limit"}


class DrawingUploadForm(forms.Form):
    """A tester drawing or system P&ID for the Project Defaults (drawings.py checks the file)."""

    drawing_no = forms.CharField(label="Drawing no.", max_length=60)
    revision = forms.CharField(label="Revision", max_length=20, required=False)
    file = forms.FileField(label="File")

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, prefix=kind, **kwargs)
        self.kind = kind
        self.fields["file"].widget.attrs["accept"] = (
            ".pdf,.tif,.tiff,.jpg,.jpeg,.png" if kind == "tester" else ".pdf"
        )
