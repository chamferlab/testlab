"""Test Result of a Test Run (M2B): inspection evidence and engineering assessment.

    TCN                    INSTRUCTED
    Manual log + DCS       ACTUAL EXECUTION DATA (testlab.execution)
    Test Result            INSPECTION EVIDENCE + ENGINEERING ASSESSMENT (here)

    TestRun ── TestResult (one-to-one; never a TCN revision)
               ├── ResultComponent      a physical part inspected: position + component (inspection.py)
               │   └── FaceMeasurement  one Pre or Post measurement of one kind (M2D, seal_faces.py)
               │       ├── MeasurementFile     original instrument file(s), byte for byte (sha256)
               │       ├── MeasurementFigure   SVG figures generated from them at review
               │       └── DimensionalReading | MahrAnalysis | RoundpakAnalysis   settings + results
               ├── ResultImage          picture evidence, original file kept unchanged
               ├── ResultPlot           a DCS plot: its definition (reproducible) + the report image (plots.py)
               └── AcceptanceItem       criterion, requirement, actual, assessment, remark

Which measurement kinds are expected is set per Result for the seal faces
(required / not applicable) and can be overridden per component. The
Pre/Post summary, Δ and wear are derived from the reported measurements
(measurement_summary.py), never typed.

Nothing the manual log or the DCS data already holds is stored again:
leakage totals, average leakage rate, logged period and wear rate are
derived when shown (evidence.py, acceptance.py). Events are manual log
entries with a category (execution.ManualLogEntry).

The Result keeps its own copy of what it starts from (components, the
materials as instructed on the TCN at that moment), so later edits of the
project defaults or of the TCN never change it.

Lifecycle: Draft → Final (finalization.py). Final means complete and
controlled, not passed: the record (requirements with their sources,
evidence, assessments) is frozen as a ResultRevision, the Engineering report
is issued from it, and the Result, its children and the run's execution
evidence can no longer change (testlab/locks.py: result_lock for the pages
and the backstops).

Revision (M2F-B2, revisions.py): a final Result is corrected by opening the
next revision with a reason. The Result is a Draft again (revision N in
progress) until it is finalized as revision N; earlier revisions, their
frozen records and their documents never change. The run's execution
evidence can be corrected again while a revision is in progress (each
revision's frozen record keeps its own copy), and what was on
record in a final revision (pictures, plots, measurements with their
instrument files) is never deleted: it is superseded by a new measurement
or left out of the report.

A Result may exist while the run's TCN is a Draft — intentional
development-phase policy; production will refuse it through the same
lifecycle/authorization rule as the manual log
(locks.result_refusal, TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN),
not through these models.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from testlab import locks
from testlab.locks import Locked

from testlab.execution.models import DcsDataset
from testlab.projects.arrangements import SealRole
from testlab.projects.models import ProgramRequirement, RequirementBasis, RequirementComparison
from testlab.tcn.models import TcnRequirementChange, TcnRevision, TestRun

from . import inspection
from .inspection import Applicability, Component, MeasurementKind, Stage


class TestResult(models.Model):
    __test__ = False  # not a pytest test class

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        FINAL = "final", "Final"

    test_run = models.OneToOneField(TestRun, on_delete=models.PROTECT, related_name="result")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)

    # Report: prepared by the responsible engineer; title and objective come from the Test
    # Program / TCN, with an optional addition for this run.
    responsible_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="Responsible engineer", help_text="Printed as “Prepared by” on the report.",
    )
    objective_addition = models.TextField(
        "Run-specific objective", blank=True, help_text="Added to the objective of the Test Program for this run."
    )

    # Engineer-authored narrative (legacy report: commentary under the plots, Test Conclusion).
    observations = models.TextField(
        "Observations", blank=True,
        help_text="What was seen during the run and in the logged data.",
    )
    conclusion = models.TextField("Conclusion", blank=True)
    recommendation = models.TextField("Recommendation", blank=True)

    # Measurement plan of the seal faces (M2D); components may override (ResultComponent).
    dimensional_applicability = models.CharField(
        "Face length", max_length=16, choices=Applicability.choices, default=Applicability.REQUIRED
    )
    mahr_applicability = models.CharField(
        "Mahr profile / roughness", max_length=16, choices=Applicability.choices, default=Applicability.REQUIRED
    )
    roundpak_applicability = models.CharField(
        "ROUNDPAK flatness", max_length=16, choices=Applicability.choices, default=Applicability.REQUIRED
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    # Set at finalization (finalization.py) while the Result is final; the record and the documents of
    # each final revision are ResultRevision / ReportDocument.
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # The revision in progress (revisions.py): set when a final Result is opened for revision, moved to
    # its ResultRevision and cleared when that revision is finalized.
    revision_reason = models.TextField("Reason for the revision", blank=True)
    revision_opened_at = models.DateTimeField(null=True, blank=True)
    revision_opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["draft", "final"]), name="result_status_valid"),
            models.CheckConstraint(
                condition=Q(status="draft", finalized_at__isnull=True, finalized_by__isnull=True)
                | Q(status="final", finalized_at__isnull=False, finalized_by__isnull=False,
                    revision_opened_at__isnull=True),
                name="result_final_data_iff_final",
            ),
            models.CheckConstraint(
                condition=Q(revision_opened_at__isnull=True, revision_opened_by__isnull=True, revision_reason="")
                | (Q(revision_opened_at__isnull=False, revision_opened_by__isnull=False) & ~Q(revision_reason="")),
                name="result_revision_opened_with_reason",
            ),
        ]  # fmt: skip

    def __str__(self):
        return f"Result of {self.test_run}"

    @property
    def is_final(self):
        return self.status == self.Status.FINAL

    @property
    def is_revising(self):
        """A Draft that revises a final Result (revision 1 or later in progress)."""
        return self.status == self.Status.DRAFT and self.revision_opened_at is not None

    def save(self, *args, **kwargs):
        if self.pk is not None and (lock := locks.result_lock(self.test_run)):
            raise FinalResultError(lock)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if lock := locks.result_lock(self.test_run):
            raise FinalResultError(lock)
        return super().delete(*args, **kwargs)


class FinalResultError(Locked):
    """Attempt to change a final Result, its evidence or the run's execution evidence."""


class ResultComponent(models.Model):
    """A physical part inspected for the Result, e.g. IB Rotor, with its identity as fitted."""

    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name="components")
    position = models.CharField(max_length=10, choices=SealRole.choices, blank=True)  # "": whole cartridge
    component = models.CharField(max_length=20, choices=Component.choices)
    # As fitted for the run (legacy "Seal Configuration" table).
    part_no = models.CharField("Part drawing", max_length=60, blank=True)
    material = models.CharField("Material", max_length=100, blank=True)
    # Before the test; free text with suggestions. "New" is New = Y on the slides, any other text N.
    condition = models.CharField(
        "Face condition", max_length=60, blank=True, help_text="Before the test: New, Reused or Re-lapped"
    )
    observations = models.TextField(
        "Inspection observations", blank=True, help_text="What was seen on this part at the Pre/Post inspection."
    )
    # Face dimensions as fitted (M2D): entered once, copied into each Mahr/ROUNDPAK measurement as its input.
    face_od = models.DecimalField("Face OD", max_digits=10, decimal_places=4, null=True, blank=True)
    face_id = models.DecimalField("Face ID", max_digits=10, decimal_places=4, null=True, blank=True)
    diameter_unit = models.CharField("Unit", max_length=2, choices=[("in", "in"), ("mm", "mm")], default="in")
    # Measurement plan overrides; "" = the Result's default for a seal face; for an optional
    # component face length/dimensions are optional and Mahr/ROUNDPAK not applicable (inspection.py).
    dimensional_applicability = models.CharField(
        "Face length", max_length=16, choices=Applicability.choices, blank=True
    )
    mahr_applicability = models.CharField("Mahr", max_length=16, choices=Applicability.choices, blank=True)
    roundpak_applicability = models.CharField("ROUNDPAK", max_length=16, choices=Applicability.choices, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["result", "position", "component"], name="one_component_identity"),
            models.CheckConstraint(
                condition=Q(component="sleeve") | ~Q(position=""), name="only_sleeve_without_position"
            ),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return inspection.identity_label(self.position, self.component)

    @property
    def sort_key(self):
        return inspection.sort_key(self.position, self.component)

    @property
    def description(self):
        return inspection.COMPONENT_DESCRIPTIONS[Component(self.component)]

    @property
    def is_face(self):
        return self.component in inspection.FACES

    def applicability(self, kind):
        """Required, optional or not applicable for a measurement kind: own override, else the default."""
        return inspection.effective_applicability(
            self.component, getattr(self, f"{kind}_applicability"), getattr(self.result, f"{kind}_applicability"),
            kind,
        )

    def applicable_kinds(self):
        """Kinds that can be measured on the component: required or optional."""
        return [kind for kind in MeasurementKind.values if self.applicability(kind) != Applicability.NOT_APPLICABLE]

    def kind_label(self, kind):
        return inspection.kind_label(self.component, kind)

    def is_empty(self):
        return not (self.face_measurements.exists() or self.images.exists())


class ResultImage(models.Model):
    """A picture of the Result. The uploaded file is kept byte for byte (sha256);
    a display copy (preview) is derived from it and rebuilt when missing."""

    class Category(models.TextChoices):
        CROSS_SECTION = "cross_section", "Seal cross section"
        PRE_TEST = "pre_test", "Pre-test"
        POST_TEST = "post_test", "Post-test"
        OTHER = "other", "Other / supporting"

    result = models.ForeignKey(TestResult, on_delete=models.PROTECT, related_name="images")
    category = models.CharField(max_length=20, choices=Category.choices)
    # Optional: a cross section or an overall view belongs to no single component.
    component = models.ForeignKey(
        ResultComponent, on_delete=models.PROTECT, null=True, blank=True, related_name="images"
    )
    caption = models.CharField("Caption / remark", max_length=300, blank=True)
    order = models.PositiveIntegerField(default=0)

    file = models.FileField(max_length=255)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    image_format = models.CharField(max_length=10)  # as detected, e.g. JPEG, PNG
    preview_file = models.CharField(max_length=255, blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["result", "category", "order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(category__in=["cross_section", "pre_test", "post_test", "other"]),
                name="result_image_category_valid",
            ),
        ]

    def __str__(self):
        return self.original_name


class ResultPlot(models.Model):
    """A plot of a DCS dataset added to the Result.

    The definition is what reproduces the plot from the stored dataset:
    source dataset, signals with their axis, time window and axis ranges
    (null = automatic / whole recording). The image is the rendering of that
    definition for the report, made by the same plot code as the DCS viewer;
    it is never the source of truth and is replaced when the plot is
    updated from the viewer.
    """

    result = models.ForeignKey(TestResult, on_delete=models.PROTECT, related_name="plots")
    dataset = models.ForeignKey(DcsDataset, on_delete=models.PROTECT, related_name="result_plots")
    order = models.PositiveIntegerField(default=0)
    caption = models.CharField("Caption", max_length=300, blank=True)

    # Definition (plots.py validates it against the dataset)
    signals = models.JSONField()  # [{"column", "name", "unit", "axis": "y" | "y2"}], in legend order
    time_start_h = models.FloatField(null=True, blank=True)  # dataset time axis [h]; null: whole recording
    time_end_h = models.FloatField(null=True, blank=True)
    y_min = models.FloatField(null=True, blank=True)  # left axis; null: automatic
    y_max = models.FloatField(null=True, blank=True)
    y2_min = models.FloatField(null=True, blank=True)  # right axis; null: automatic
    y2_max = models.FloatField(null=True, blank=True)

    # Report image, rendered from the definition
    image = models.FileField(max_length=255)
    image_sha256 = models.CharField(max_length=64)
    image_width = models.PositiveIntegerField()
    image_height = models.PositiveIntegerField()
    # How the data was reduced for the image: {"samples_in_window", "points_per_signal", "decimated"}
    rendering = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["result", "order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(time_start_h__isnull=True, time_end_h__isnull=True)
                | Q(time_start_h__isnull=False, time_end_h__isnull=False, time_start_h__lt=models.F("time_end_h")),
                name="plot_time_window_valid",
            ),
            models.CheckConstraint(
                condition=Q(y_min__isnull=True, y_max__isnull=True)
                | Q(y_min__isnull=False, y_max__isnull=False, y_min__lt=models.F("y_max")),
                name="plot_y_range_valid",
            ),
            models.CheckConstraint(
                condition=Q(y2_min__isnull=True, y2_max__isnull=True)
                | Q(y2_min__isnull=False, y2_max__isnull=False, y2_min__lt=models.F("y2_max")),
                name="plot_y2_range_valid",
            ),
        ]

    def __str__(self):
        return self.caption or f"Plot of {self.dataset}"

    def definition(self):
        """The plot definition as the DCS viewer reads it back."""
        pair = lambda a, b: None if a is None else [a, b]  # noqa: E731
        return {
            "id": self.pk,
            "dataset": self.dataset_id,
            "signals": self.signals,
            "time_range": pair(self.time_start_h, self.time_end_h),
            "y_range": pair(self.y_min, self.y_max),
            "y2_range": pair(self.y2_min, self.y2_max),
            "caption": self.caption,
        }


class Assessment(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"
    NOT_EVALUATED = "not_evaluated", "Not evaluated"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


WITHDRAWN_PREFIX = "No longer in force — "


class AcceptanceItem(models.Model):
    """One acceptance criterion of the Result: requirement, actual, assessment, remark.

    The requirement comes from the requirements in force for the run, frozen
    with its issued TCN (program_requirement / tcn_change, taken at set
    moments by requirements.sync_requirements) or is entered on the Result. The
    actual is either typed by the engineer (basis manual) or derived from
    structured evidence (acceptance.py), never both. `assessment` is the
    engineer's decision; when it is empty, Pass/Fail is derived only where
    the comparison is unambiguous, otherwise the item is not assessed yet —
    which is not the same as an explicit Not evaluated.
    """

    Basis = RequirementBasis
    Comparison = RequirementComparison

    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name="acceptance_items")
    order = models.PositiveIntegerField(default=0)
    criterion = models.CharField("Criterion", max_length=120)
    basis = models.CharField("Actual from", max_length=20, choices=Basis.choices, default=Basis.MANUAL)
    # Where the requirement comes from (none: entered on the Result).
    program_requirement = models.ForeignKey(
        ProgramRequirement, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    tcn_change = models.ForeignKey(TcnRequirementChange, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")  # fmt: skip
    source = models.CharField("Requirement source", max_length=300, blank=True)
    position = models.CharField("Seal", max_length=10, choices=SealRole.choices, blank=True)  # leakage_rate
    component = models.ForeignKey(  # wear_rate
        ResultComponent, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    comparison = models.CharField(max_length=2, choices=Comparison.choices, blank=True)
    limit = models.DecimalField("Limit", max_digits=24, decimal_places=12, null=True, blank=True)
    requirement_text = models.CharField(
        "Requirement", max_length=200, blank=True, help_text="When the requirement is not a numeric limit."
    )
    unit = models.CharField(max_length=20, blank=True)
    actual_text = models.CharField("Actual", max_length=200, blank=True)  # basis manual only
    assessment = models.CharField(max_length=20, choices=Assessment.choices, blank=True)
    remark = models.TextField("Engineer remark", blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["result", "order", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(assessment__in=["", "pass", "fail", "not_evaluated", "not_applicable"]),
                name="acceptance_assessment_valid",
            ),
            models.CheckConstraint(
                condition=Q(basis__in=["manual", "leakage_rate", "wear_rate", "running_hours", "logged_hours"]),
                name="acceptance_basis_valid",
            ),
            models.CheckConstraint(
                condition=Q(comparison="", limit__isnull=True) | (~Q(comparison="") & Q(limit__isnull=False)),
                name="acceptance_comparison_with_limit",
            ),
        ]

    def __str__(self):
        return self.criterion

    @property
    def from_requirement(self):
        """True when the requirement comes from the requirements in force (Test Program or a TCN change)."""
        return bool(self.source) and not self.withdrawn

    @property
    def withdrawn(self):
        return self.source.startswith(WITHDRAWN_PREFIX)


# ---- Seal-face measurements (M2D) ----------------------------------------------------------


class FaceMeasurement(models.Model):
    """One Pre-test or Post-test measurement of one kind on one component.

    Chain of evidence: original instrument file(s) (MeasurementFile) →
    parsed and calculated with the stored settings by the engine named in
    `engine` (facemeasure/) → stored results (the analysis row) → figures
    (MeasurementFigure) → summary and report.

    Draft: uploaded, settings may change, results are recalculated on every
    change. Reviewed: the engineer accepted the result (reviewed_by/at);
    figures are generated and kept. Only a reviewed measurement can be the
    reported one of its slot (component, stage, kind); others stay on record.
    A stored result is never recalculated silently: a calculation change is
    a new engine version, applied only by a deliberate recalculation of a
    Draft measurement. When the Result is finalized (not yet built), its
    measurements will be frozen as they are.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEWED = "reviewed", "Reviewed"

    component = models.ForeignKey(ResultComponent, on_delete=models.PROTECT, related_name="face_measurements")
    stage = models.CharField(max_length=4, choices=Stage.choices)
    kind = models.CharField(max_length=12, choices=MeasurementKind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    reported = models.BooleanField(default=False, help_text="The measurement of its slot used in the summary.")
    # Whether a report shows it (summary, detail, figures, remark) is a report selection per profile
    # (ReportSelection, M2F-B), not a property of the measurement: reviewed ≠ selected for publication.
    engine = models.CharField(max_length=20, blank=True)  # calculation version of the stored results
    measured_at = models.DateTimeField(null=True, blank=True)  # from the instrument file, when it says
    measured_on_text = models.CharField(max_length=60, blank=True)  # instrument date/time as exported
    remark = models.CharField("Remark", max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["component", "kind", "stage", "created_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(stage__in=["pre", "post"]), name="face_measurement_stage_valid"),
            models.CheckConstraint(
                condition=Q(kind__in=["dimensional", "mahr", "roundpak"]), name="face_measurement_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(status="draft", reviewed_at__isnull=True)
                | Q(status="reviewed", reviewed_at__isnull=False, reviewed_by__isnull=False),
                name="face_measurement_review_stamped",
            ),
            models.CheckConstraint(
                condition=Q(reported=False) | Q(status="reviewed"), name="face_measurement_reported_reviewed"
            ),
            models.UniqueConstraint(
                fields=["component", "stage", "kind"], condition=Q(reported=True),
                name="one_reported_measurement_per_slot",
            ),
        ]  # fmt: skip

    def __str__(self):
        return f"{self.component} {self.get_kind_display()} {self.get_stage_display()}"

    @property
    def result(self):
        return self.component.result

    @property
    def is_reviewed(self):
        return self.status == self.Status.REVIEWED

    @property
    def source(self):
        return inspection.KIND_SOURCES[MeasurementKind(self.kind)]

    @property
    def analysis(self):
        """The kind's settings + results row (DimensionalReading, MahrAnalysis or RoundpakAnalysis), or None."""
        return getattr(self, self.kind, None)


class MeasurementFile(models.Model):
    """An original instrument file of a measurement, stored byte for byte; the source of truth."""

    class Role(models.TextChoices):
        MAHR_LEFT = "mahr_left", "Left-side scan (Mahr TXT)"
        MAHR_RIGHT = "mahr_right", "Right-side scan (Mahr TXT)"
        ROUNDPAK = "roundpak", "ROUNDPAK CSV"

    measurement = models.ForeignKey(FaceMeasurement, on_delete=models.PROTECT, related_name="files")
    role = models.CharField(max_length=12, choices=Role.choices)
    file = models.FileField(max_length=255)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)  # what the instrument wrote about itself (headers), as read
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["measurement", "role"]
        constraints = [models.UniqueConstraint(fields=["measurement", "role"], name="one_file_per_role")]

    def __str__(self):
        return self.original_name


class MeasurementFigure(models.Model):
    """An SVG figure generated from the measurement's files and settings when it was reviewed."""

    measurement = models.ForeignKey(FaceMeasurement, on_delete=models.CASCADE, related_name="figures")
    code = models.CharField(max_length=30)  # e.g. mahr_left, roundpak_circular (facemeasure.figures)
    file = models.FileField(max_length=255)
    sha256 = models.CharField(max_length=64)
    engine = models.CharField(max_length=20)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["measurement", "code"]
        constraints = [models.UniqueConstraint(fields=["measurement", "code"], name="one_figure_per_code")]

    def __str__(self):
        return f"{self.measurement} {self.code}"


class DimensionalReading(models.Model):
    """A dimensional inspection value, as measured: the face length of a seal face, or a named
    dimension of another part (sleeve, backing ring)."""

    measurement = models.OneToOneField(
        FaceMeasurement, on_delete=models.CASCADE, primary_key=True, related_name="dimensional"
    )
    quantity = models.CharField(max_length=30, default="face_length")
    feature = models.CharField("Measured feature", max_length=100, blank=True)  # e.g. "OD at seal seat"
    value = models.DecimalField(max_digits=16, decimal_places=6)
    unit = models.CharField(max_length=10)  # stored with the value, as entered
    instrument = models.CharField("Gauge / instrument", max_length=100, blank=True)

    def __str__(self):
        return f"{self.value} {self.unit}"


class MahrAnalysis(models.Model):
    """Settings and results of a Mahr measurement (facemeasure.mahr).

    Instrument values (Ra, Rz) are as exported for each scan; the taper is
    TestLab's opposing-scan calculation, classified Flat / Convex / Concave
    against the flat limit (mahr-2; mahr-1 results keep their stored class
    names). Profile P–V is a profile range, not flatness.
    """

    measurement = models.OneToOneField(FaceMeasurement, on_delete=models.CASCADE, primary_key=True, related_name="mahr")
    # Settings
    od = models.FloatField("Face OD")
    id = models.FloatField("Face ID")
    diameter_unit = models.CharField(max_length=2, default="in")
    light_band_um = models.FloatField("Light-band height (µm)")
    left_direction = models.CharField(max_length=3)
    right_direction = models.CharField(max_length=3)
    left_start_mm = models.FloatField()
    left_end_mm = models.FloatField()
    right_start_mm = models.FloatField()
    right_end_mm = models.FloatField()
    pv_view = models.CharField("Profile view", max_length=10, default="common")
    skip_points = models.PositiveSmallIntegerField("Skip first points", default=5)
    flat_limit_hlb = models.FloatField("Flat limit (± HLB)", default=0.25)
    # Results (calculated)
    face_width_mm = models.FloatField()
    left_slope = models.FloatField()  # µm/mm
    right_slope = models.FloatField()
    common_slope = models.FloatField()
    taper_um = models.FloatField()  # signed
    taper_hlb = models.FloatField()
    classification = models.CharField(max_length=30)
    left_pv_um = models.FloatField()  # in pv_view
    right_pv_um = models.FloatField()
    # Instrument values, as exported (None: not in the file)
    left_ra_um = models.FloatField(null=True, blank=True)
    left_rz_um = models.FloatField(null=True, blank=True)
    right_ra_um = models.FloatField(null=True, blank=True)
    right_rz_um = models.FloatField(null=True, blank=True)
    detail = models.JSONField(default=dict)  # P–V in every view, point counts, intervals used, notices

    def __str__(self):
        return f"Mahr analysis of {self.measurement}"

    @property
    def mean_ra_um(self):
        """Ra of the face: mean of the scans' exported Ra (one value when only one is exported)."""
        values = [v for v in (self.left_ra_um, self.right_ra_um) if v is not None]
        return sum(values) / len(values) if values else None


class RoundpakAnalysis(models.Model):
    """Settings and results of a ROUNDPAK measurement (facemeasure.roundpak).

    Instrument results (flatness FLTt, tolerances, check result) are stored
    exactly as exported and are never replaced by the values calculated
    from the traces (LFD P–V, radial slopes, taper, harmonics). The taper
    is classified from roundpak-2 on; a roundpak-1 result keeps its
    unclassified full-width rise (same formula and sign).
    """

    measurement = models.OneToOneField(
        FaceMeasurement, on_delete=models.CASCADE, primary_key=True, related_name="roundpak"
    )
    # Settings
    od = models.FloatField("Face OD", null=True, blank=True)  # optional for ROUNDPAK: both or neither
    id = models.FloatField("Face ID", null=True, blank=True)
    diameter_unit = models.CharField(max_length=2, default="in")
    light_band_um = models.FloatField("Light-band height (µm)")
    circle_scan = models.CharField(max_length=10, blank=True)
    circle_source = models.CharField(max_length=10, default="deviation")
    left_scan = models.CharField(max_length=10, blank=True)
    right_scan = models.CharField(max_length=10, blank=True)
    radial_source = models.CharField(max_length=10, default="deviation")
    left_start_mm = models.FloatField(null=True, blank=True)
    left_end_mm = models.FloatField(null=True, blank=True)
    right_start_mm = models.FloatField(null=True, blank=True)
    right_end_mm = models.FloatField(null=True, blank=True)
    flat_limit_hlb = models.FloatField("Flat limit (± HLB)", null=True, blank=True)  # None: roundpak-1
    # Instrument, as exported
    part_code = models.CharField(max_length=200, blank=True)  # Title
    description = models.CharField(max_length=300, blank=True)  # Sub Title
    operator = models.CharField(max_length=100, blank=True)  # Created By
    flatness_um = models.FloatField("Instrument flatness FLTt (µm)", null=True, blank=True)
    tolerance_upper = models.CharField(max_length=30, blank=True)
    tolerance_lower = models.CharField(max_length=30, blank=True)
    check_result = models.CharField(max_length=30, blank=True)
    instrument = models.JSONField(default=dict)  # every exported result parameter of the circular scan
    # Calculated from the traces
    lfd_pv_um = models.FloatField(null=True, blank=True)
    circular_radius = models.CharField(max_length=30, blank=True)  # X Position as exported
    full_revolution = models.BooleanField(default=False)
    left_slope = models.FloatField(null=True, blank=True)  # µm/mm
    right_slope = models.FloatField(null=True, blank=True)
    mean_slope = models.FloatField(null=True, blank=True)
    tilt_slope = models.FloatField(null=True, blank=True)  # setup tilt between the radial traces
    taper_um = models.FloatField(null=True, blank=True)  # radial taper, signed OD − ID high (positive: Concave)
    classification = models.CharField(max_length=30, blank=True)  # Flat / Convex / Concave; blank: none
    separation_deg = models.FloatField(null=True, blank=True)
    detail = models.JSONField(default=dict)  # scans, deleted points, harmonics, radial inclinations as exported

    def __str__(self):
        return f"ROUNDPAK analysis of {self.measurement}"

    @property
    def flatness_hlb(self):
        return None if self.flatness_um is None else self.flatness_um / self.light_band_um

    @property
    def lfd_pv_hlb(self):
        return None if self.lfd_pv_um is None else self.lfd_pv_um / self.light_band_um

    @property
    def taper_hlb(self):
        return None if self.taper_um is None else self.taper_um / self.light_band_um

    @property
    def strongest_harmonics(self):
        """The three largest waviness components (UPR) stored with the result; none for roundpak-1."""
        return sorted(self.detail.get("harmonics") or [], key=lambda h: -h["amplitude_um"])[:3]


# ---- Report profiles, frozen records and issued documents (M2F-B) --------------------------------


class ReportProfile(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    ENGINEERING = "engineering", "Engineering"


class ReportSelection(models.Model):
    """What a report profile shows of a Result: the engineer's overrides of the profile defaults
    (documents/report_profiles.py). Presentation only: it never changes evidence or a calculation,
    and it may change after the Result is final (a later document may present the record differently)."""

    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name="report_selections")
    profile = models.CharField(max_length=12, choices=ReportProfile.choices)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["result", "profile"], name="one_selection_per_profile")]

    def __str__(self):
        return f"{self.get_profile_display()} selection of {self.result}"


class ResultRevision(models.Model):
    """The frozen record of a final Result (M2F-B): the complete report context and the bytes of every
    picture and plot it shows, each with its SHA-256. Documents of any profile are rendered from it,
    never from live data. Written once at finalization (or migrated from M2E); never changed.

    Revision 0 is the first finalization. A later revision (M2F-B2) was opened on the final Result with
    a reason, by someone, at some time (reason, opened_by, opened_at) and has its own complete record.
    """

    result = models.ForeignKey(TestResult, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveSmallIntegerField()
    finalized_at = models.DateTimeField()
    finalized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    tcn_revision = models.ForeignKey(TcnRevision, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="result_revisions")  # fmt: skip
    record_file = models.FileField(max_length=255)  # gzip JSON of the record context
    record_sha256 = models.CharField(max_length=64)  # of the uncompressed canonical JSON
    record_schema = models.PositiveSmallIntegerField()
    # [{"path": asset path in the context, "kind", "id", "source_sha256", "file": frozen copy, "sha256": of it}]
    assets = models.JSONField(default=list)
    origin = models.CharField(max_length=12, default="finalized")  # finalized | migrated (M2E final)
    reason = models.TextField(blank=True)  # why the revision was opened (required from revision 1)
    opened_at = models.DateTimeField(null=True, blank=True)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="+")  # fmt: skip
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["result", "number"]
        constraints = [
            models.UniqueConstraint(fields=["result", "number"], name="one_revision_number"),
            models.CheckConstraint(
                condition=Q(number=0, reason="", opened_at__isnull=True, opened_by__isnull=True)
                | (Q(number__gt=0, opened_at__isnull=False, opened_by__isnull=False) & ~Q(reason="")),
                name="revision_opened_with_reason",
            ),
        ]

    def __str__(self):
        return f"{self.result} revision {self.number}"


class ReportDocument(models.Model):
    """An issued Test Run Report: one rendering of one final revision with one profile, stored once.

    Identity <project>-Run<nnn>-TRR-R<revision>-<C|E>[-<issue>]. Issued: the controlled publication.
    Superseded: replaced by a newer issue, or by a newer final revision of the Result. Withdrawn:
    withdrawn from use with a reason. The PDF and its SHA-256 never change; only the status moves on.
    """

    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        SUPERSEDED = "superseded", "Superseded"
        WITHDRAWN = "withdrawn", "Withdrawn"

    revision = models.ForeignKey(ResultRevision, on_delete=models.PROTECT, related_name="documents")
    profile = models.CharField(max_length=12, choices=ReportProfile.choices)
    issue = models.PositiveSmallIntegerField()
    identity = models.CharField(max_length=80, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ISSUED)
    reason = models.TextField(blank=True)  # why it was issued (again): required from issue 2
    selection = models.JSONField(default=dict)  # the resolved selection it was rendered with
    presentation_sha256 = models.CharField(max_length=64, blank=True)  # of the rendered context (canonical JSON)
    file = models.FileField(max_length=255)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    template_sha256 = models.CharField(max_length=64, blank=True)
    origin = models.CharField(max_length=12, default="issued")  # issued | finalized | migrated (M2E final)
    issued_at = models.DateTimeField()
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                          related_name="+")  # fmt: skip
    status_reason = models.TextField(blank=True)
    superseded_by = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["revision", "profile", "issue"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "profile", "issue"], name="one_document_issue"),
            models.UniqueConstraint(fields=["revision", "profile"], condition=Q(status="issued"),
                                    name="one_issued_document_per_profile"),
            models.CheckConstraint(condition=Q(status="issued") | Q(status_changed_at__isnull=False),
                                   name="document_status_change_stamped"),
            models.CheckConstraint(condition=Q(issue=1) | ~Q(reason=""), name="document_reissue_has_reason"),
        ]  # fmt: skip

    def __str__(self):
        return self.identity


class RecordIntegrityError(Exception):
    """A frozen record or an issued document does not match its SHA-256, or would be changed."""


# Immutable once written: a record never changes; a document only moves on in status.


@receiver(pre_save, sender=ResultRevision, dispatch_uid="frozen_result_revision")
def _guard_revision(sender, instance, **kwargs):
    if instance.pk is not None and ResultRevision.objects.filter(pk=instance.pk).exists():
        raise RecordIntegrityError(f"{instance} is a frozen record and cannot change.")


@receiver(pre_delete, sender=ResultRevision, dispatch_uid="frozen_result_revision_delete")
@receiver(pre_delete, sender=ReportDocument, dispatch_uid="issued_document_delete")
def _guard_record_delete(sender, instance, **kwargs):
    raise RecordIntegrityError(f"{instance} is a controlled record and cannot be deleted.")


@receiver(pre_save, sender=ReportDocument, dispatch_uid="issued_document")
def _guard_document(sender, instance, update_fields=None, **kwargs):
    if instance.pk is None:
        return
    stored = ReportDocument.objects.filter(pk=instance.pk).values().first()
    if stored is None:
        return
    changed = {f.attname for f in ReportDocument._meta.concrete_fields
               if stored[f.attname] != getattr(instance, f.attname)}  # fmt: skip
    allowed = {"status", "status_changed_at", "status_changed_by_id", "status_reason", "superseded_by_id"}
    linking = (changed == {"superseded_by_id"} and stored["status"] == ReportDocument.Status.SUPERSEDED
               and stored["superseded_by_id"] is None)  # a superseded issue gets its successor once  # fmt: skip
    if not linking and (changed - allowed or stored["status"] != ReportDocument.Status.ISSUED):
        raise RecordIntegrityError(f"{instance} is issued: only an issued document may become superseded or "
                                   "withdrawn, and nothing else of it changes.")  # fmt: skip


# The Result, its parts and the run's execution evidence are locked while the Result is Final, and what a
# final revision holds is never deleted: testlab/locks.py (backstops connected in ResultsConfig.ready).
