import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Q

from testlab import locks

from . import conditions
from .arrangements import (
    Arrangement,
    SealRole,
    applicable_monitored_parameters,
    applicable_roles,
    canonical_order,
    plug_seal_required,
    seal_under_test_roles,
)

project_no_characters = RegexValidator(
    r"^[A-Za-z0-9._-]+$",
    "Use only letters, digits, '-', '_' and '.' (the project number is used in file names).",
)


def normalize_project_no(value):
    return (value or "").strip().upper()


def project_no_format_warning(project_no):
    """Return a warning if project_no does not follow the lab convention (V1).

    The format is a convention only and never blocks saving.
    """
    pattern = settings.TESTLAB_PROJECT_NO_PATTERN
    if pattern and not re.fullmatch(pattern, project_no):
        return f"Project number {project_no} does not follow the usual format (e.g. 2026RSD03TL)."
    return None


class Project(models.Model):
    # Stored upper-case, so the unique constraint is case-insensitive (V1).
    project_no = models.CharField(
        "Project no.",
        max_length=20,
        unique=True,
        validators=[project_no_characters],
        error_messages={"unique": "A project with this number already exists."},
    )
    description = models.CharField(
        "Description",
        max_length=200,
        help_text="Printed on the TCN as “Project Description”, e.g. ADNOC BAB UHTW/UHTW 4.750/4.750.",
    )
    customer = models.CharField("Customer", max_length=200, blank=True)
    # Project defaults for new TCNs. Created with the project; TCNs take a
    # copy, so editing the defaults never changes an existing TCN.
    default_setup = models.OneToOneField(
        "RunSetup", on_delete=models.PROTECT, related_name="default_for_project", editable=False
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-project_no"]

    def __str__(self):
        return self.project_no

    def _project_no_changed(self):
        stored = Project.objects.filter(pk=self.pk).values_list("project_no", flat=True).first()
        return stored is not None and stored != normalize_project_no(self.project_no)

    def clean(self):
        super().clean()
        if self._project_no_changed() and (lock := locks.project_no_lock(self)):
            raise ValidationError({"project_no": lock})

    def save(self, *args, **kwargs):
        self.project_no = normalize_project_no(self.project_no)
        self.description = self.description.strip()
        self.customer = self.customer.strip()
        # V2 enforced here too, not only in the form: issued TCNs carry this number.
        if self.pk is not None and self._project_no_changed() and (lock := locks.project_no_lock(self)):
            raise ValidationError({"project_no": lock})
        with transaction.atomic():
            if self.default_setup_id is None:
                self.default_setup = RunSetup.create_with_positions()
            super().save(*args, **kwargs)


SINGLE_WITHOUT_PLUG_SEAL = "A single seal cannot be tested alone: the Plug Seal is required."
PROCESS_WITHOUT_PLUG_SEAL = (
    "Process conditions are filled in but no Plug Seal is installed "
    "(the process conditions are established with the Plug Seal)."
)


def _text(verbose_name, max_length):
    return models.CharField(verbose_name, max_length=max_length, blank=True)


class RunSetup(models.Model):
    """The copyable technical block of a Test Run (M1 spec §1.2).

    Used as a project's defaults (Project.default_setup) and as each TCN
    revision's own copy (TcnRevision.setup, made with clone()). Holds the
    seal arrangement under test, the Plug Seal (required for single), and
    the run conditions grouped by circuit (see conditions.py).
    """

    arrangement = models.CharField(
        "Seal arrangement", max_length=24, choices=Arrangement.choices, default=Arrangement.DUAL_PRESSURIZED
    )
    # Plug Seal: test equipment that establishes the process conditions, not
    # part of the arrangement. Required for single (V16), optional for dual.
    plug_seal_present = models.BooleanField("Plug Seal installed", default=False)
    reference_drawing = _text("Reference drawing", 200)
    run_description = models.TextField("Test Run description", blank=True)

    # Common run conditions
    test_unit = _text("Test unit", 20)
    duration = _text("Duration", 50)
    speed = _text("Speed", 50)

    # Process Conditions, established with the Plug Seal (only when plug_seal_present, V6)
    process_medium = _text("Process medium", 100)
    process_pressure = _text("Process pressure", 50)
    process_inlet_temperature = _text("Process inlet temperature", 50)
    process_flow = _text("Process flow", 50)

    # Mechanical-seal support circuit: barrier, buffer or flush (any arrangement)
    seal_support_medium = _text("Seal-support medium", 100)
    seal_support_pressure = _text("Seal-support pressure", 50)
    seal_support_inlet_temperature = _text("Seal-support inlet temperature", 50)
    seal_support_flow = _text("Seal-support flow", 50)

    monitored_parameters = models.JSONField("Parameters to log", default=list, blank=True)

    TEXT_FIELDS = (
        ["reference_drawing", "run_description"]
        + conditions.COMMON_FIELDS
        + conditions.PROCESS_FIELDS
        + conditions.SEAL_SUPPORT_FIELDS
    )

    class Meta:
        constraints = [
            # V16: single ⇒ Plug Seal.
            models.CheckConstraint(
                condition=~Q(arrangement="single") | Q(plug_seal_present=True),
                name="single_seal_requires_plug_seal",
                violation_error_message=SINGLE_WITHOUT_PLUG_SEAL,
            ),
            # V6: no process conditions without the Plug Seal.
            models.CheckConstraint(
                condition=Q(plug_seal_present=True)
                | Q(process_medium="", process_pressure="", process_inlet_temperature="", process_flow=""),
                name="process_conditions_require_plug_seal",
                violation_error_message=PROCESS_WITHOUT_PLUG_SEAL,
            ),
        ]

    def __str__(self):
        return f"RunSetup {self.pk} ({self.configuration_display})"

    @property
    def configuration_display(self):
        """e.g. "Single", "Dual Pressurized + Plug Seal".

        The Plug Seal is only named where it is optional: a single seal is
        always tested with it, and "Single + Plug" is not an arrangement.
        """
        optional_plug = self.plug_seal_present and not plug_seal_required(self.arrangement)
        return self.get_arrangement_display() + (" + Plug Seal" if optional_plug else "")

    @classmethod
    def create_with_positions(cls, **fields):
        """Create a setup with exactly the applicable (empty) seal positions."""
        with transaction.atomic():
            setup = cls.objects.create(**fields)
            for role in setup.applicable_roles():
                setup.positions.create(role=role)
        return setup

    def clone(self):
        """Deep copy of this block and its seal positions.

        The copy shares nothing with the original, so a TCN revision holding
        a clone of the project defaults (or of another TCN's setup) is not
        affected by later edits of its source. Copies the stored record, not
        unsaved (or stale) in-memory values.
        """
        with transaction.atomic():
            stored = RunSetup.objects.get(pk=self.pk)
            fields = {f.attname: getattr(stored, f.attname) for f in self._meta.concrete_fields if not f.primary_key}
            clone = RunSetup.objects.create(**fields)
            for position in stored.positions.all():
                position.pk = None
                position.setup = clone
                position.save()
        return clone

    def applicable_roles(self):
        return applicable_roles(self.arrangement, self.plug_seal_present)

    def seal_under_test_positions(self):
        roles = seal_under_test_roles(self.arrangement)
        positions = {p.role: p for p in self.positions.all()}
        return [positions[role] for role in roles if role in positions]

    def plug_seal_position(self):
        return self.positions.filter(role=SealRole.PLUG).first()

    def normalize(self):
        """Trim text and remove repeated units (V8). Returns user notices."""
        notices = []
        for field in self.TEXT_FIELDS:
            value = getattr(self, field) or ""
            # The run description keeps its indentation; only trailing space goes.
            value = value.rstrip() if field == "run_description" else value.strip()
            stripped = conditions.strip_repeated_unit(field, value)
            if stripped != value:
                notices.append(
                    f"{self._meta.get_field(field).verbose_name}: “{value}” saved as “{stripped}” "
                    f"(the unit is printed automatically)."
                )
            setattr(self, field, stripped)
        # Sort valid codes into canonical order (V7); invalid ones are reported by clean().
        if self.arrangement in Arrangement.values and isinstance(self.monitored_parameters, list):
            applicable = applicable_monitored_parameters(self.arrangement, self.plug_seal_present)
            if set(self.monitored_parameters) <= set(applicable):
                self.monitored_parameters = canonical_order(set(self.monitored_parameters))
        return notices

    def clean(self):
        self.normalization_notices = self.normalize()
        errors = {}
        # V16: a single seal is only tested with the Plug Seal.
        if plug_seal_required(self.arrangement) and not self.plug_seal_present:
            errors["plug_seal_present"] = SINGLE_WITHOUT_PLUG_SEAL
        # V6: process conditions only exist with the Plug Seal. Seal-support
        # values are valid for every arrangement, including single.
        elif not self.plug_seal_present and any(getattr(self, f) for f in conditions.PROCESS_FIELDS):
            errors["plug_seal_present"] = PROCESS_WITHOUT_PLUG_SEAL
        # V7
        if not isinstance(self.monitored_parameters, list):
            errors["monitored_parameters"] = "Must be a list of parameter codes."
        elif self.arrangement in Arrangement.values:
            applicable = applicable_monitored_parameters(self.arrangement, self.plug_seal_present)
            invalid = [c for c in self.monitored_parameters if c not in applicable]
            if invalid:
                errors["monitored_parameters"] = f"Not applicable to this configuration: {', '.join(invalid)}."
        if errors:
            raise ValidationError(errors)

    def validate_positions(self):
        """V5: exactly the positions required by arrangement and Plug Seal."""
        expected = self.applicable_roles()
        actual = sorted(self.positions.values_list("role", flat=True))
        if actual != sorted(expected):
            raise ValidationError(
                f"Seal positions {actual} do not match {self.configuration_display} (expected {sorted(expected)})."
            )

    def save(self, *args, **kwargs):
        self.normalize()
        super().save(*args, **kwargs)


class SealPosition(models.Model):
    """One seal of a RunSetup (M1 spec §1.3).

    Roles test / inboard / outboard belong to the mechanical seal arrangement
    under test. Role plug is the Plug Seal: the test equipment that
    establishes the process conditions, not a stage of a dual seal; it shares
    this table only because it is described by the same four fields.
    """

    setup = models.ForeignKey(RunSetup, on_delete=models.CASCADE, related_name="positions")
    role = models.CharField(max_length=10, choices=SealRole.choices)
    seal_type_size = _text("Seal type and size", 100)
    stationary_face = _text("Stationary seal face", 100)
    rotating_face = _text("Rotating seal face", 100)
    springs = _text("Spring(s)", 100)

    DESCRIPTIVE_FIELDS = ["seal_type_size", "stationary_face", "rotating_face", "springs"]

    class Meta:
        constraints = [models.UniqueConstraint(fields=["setup", "role"], name="unique_role_per_setup")]

    def __str__(self):
        return self.get_role_display()

    @property
    def is_plug_seal(self):
        return self.role == SealRole.PLUG

    def is_empty(self):
        return not any(getattr(self, f) for f in self.DESCRIPTIVE_FIELDS)

    def save(self, *args, **kwargs):
        for field in self.DESCRIPTIVE_FIELDS:
            setattr(self, field, (getattr(self, field) or "").strip())
        super().save(*args, **kwargs)


# ---- Test Program: baseline acceptance requirements (planned) --------------------------------


class RequirementBasis(models.TextChoices):
    """What an acceptance requirement is compared with.

    Calculated bases are compared automatically only when requirement, unit
    and evidence allow an unambiguous comparison (results/acceptance.py);
    "assessed by the engineer" never is.
    """

    MANUAL = "manual", "Assessed by the engineer"
    LEAKAGE_RATE = "leakage_rate", "Average dynamic leakage rate"
    WEAR_RATE = "wear_rate", "Face wear rate (face length)"
    RUNNING_HOURS = "running_hours", "Shaft-running hours"
    LOGGED_HOURS = "logged_hours", "Elapsed clock time"


class RequirementComparison(models.TextChoices):
    AT_MOST = "<=", "≤"
    AT_LEAST = ">=", "≥"


REQUIREMENT_FACES = [("stator", "Stator"), ("rotor", "Rotor")]


class AcceptanceRequirementFields(models.Model):
    """What an acceptance requirement says; shared by the Test Program and approved TCN changes.

    `position` and `face` narrow a leakage or wear requirement; blank means
    every seal of the arrangement under test (and both faces). An operating
    set point (speed, pressure, duration as instructed) is not a requirement:
    only what is entered here is assessed.
    """

    criterion = models.CharField("Criterion", max_length=120, blank=True)
    basis = models.CharField("Compared with", max_length=20, choices=RequirementBasis.choices,
                             default=RequirementBasis.MANUAL)  # fmt: skip
    position = models.CharField("Seal", max_length=10, choices=SealRole.choices, blank=True)
    face = models.CharField("Face", max_length=10, choices=REQUIREMENT_FACES, blank=True)
    comparison = models.CharField(max_length=2, choices=RequirementComparison.choices, blank=True)
    limit = models.DecimalField("Limit", max_digits=24, decimal_places=12, null=True, blank=True)
    unit = models.CharField("Unit", max_length=20, blank=True)
    requirement_text = models.CharField(
        "Requirement", max_length=200, blank=True, help_text="Wording of the requirement, or the whole of it."
    )

    REQUIREMENT_FIELDS = ["criterion", "basis", "position", "face", "comparison", "limit", "unit", "requirement_text"]

    class Meta:
        abstract = True

    def requirement_values(self):
        return {name: getattr(self, name) for name in self.REQUIREMENT_FIELDS}


class TestProgram(models.Model):
    """The planned Test Program of a project, as far as TestLab needs it now (PROGRAM = planned).

    Title and objective are quoted in the Test Run Report; the requirements
    are the baseline acceptance requirements of every run of the project. An
    approved (issued) TCN change can replace or waive one for its run
    (tcn.TcnRequirementChange). Program sections, planned tests and program
    revisions are milestone M3.
    """

    __test__ = False  # not a pytest test class

    project = models.OneToOneField(Project, on_delete=models.PROTECT, related_name="test_program")
    document_no = models.CharField("Document no.", max_length=60, blank=True)
    revision = models.CharField("Revision", max_length=10, blank=True)
    title = models.CharField("Title", max_length=200, blank=True)
    objective = models.TextField("Objective", blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"Test Program of {self.project}"

    @property
    def reference(self):
        """e.g. "Test Program TP-1234 rev B"."""
        parts = ["Test Program"]
        if self.document_no:
            parts.append(self.document_no)
        if self.revision:
            parts.append(f"rev {self.revision}")
        return " ".join(parts)


class ProgramRequirement(AcceptanceRequirementFields):
    """A baseline acceptance requirement of the Test Program; `number` never changes (traceability)."""

    program = models.ForeignKey(TestProgram, on_delete=models.CASCADE, related_name="requirements")
    number = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["program", "number"]
        constraints = [models.UniqueConstraint(fields=["program", "number"], name="one_requirement_number")]

    def __str__(self):
        return f"{self.ref} {self.criterion}"

    @property
    def ref(self):
        return f"P{self.number}"


class ProjectDrawing(models.Model):
    """A drawing of the project: the tester drawing or the system P&ID (Project Defaults).

    Project-wide, never copied into a TCN: the tester and the system do not
    change from one run to another. The uploaded file is kept byte for byte
    (sha256). A new upload of the same kind becomes the current drawing; the
    earlier ones stay as its history (drawings.py).
    """

    class Kind(models.TextChoices):
        TESTER = "tester", "Tester drawing"
        PID = "pid", "System P&ID"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="drawings")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    drawing_no = models.CharField("Drawing no.", max_length=60)
    revision = models.CharField("Revision", max_length=20, blank=True)

    file = models.FileField(max_length=255)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    file_format = models.CharField(max_length=10)  # as detected: PDF, TIFF, JPEG, PNG
    preview_file = models.CharField(max_length=255, blank=True)  # display copy of a picture; none for a PDF

    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["project", "kind", "-uploaded_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["tester", "pid"]), name="project_drawing_kind_valid"),
        ]

    def __str__(self):
        return f"{self.drawing_no}{f' rev {self.revision}' if self.revision else ''}"

    @property
    def is_pdf(self):
        return self.file_format == "PDF"
