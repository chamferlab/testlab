"""Evidence recorded while a Test Run is executed (M2A): Actual, not Instructed.

Both sources belong to the TestRun, never to a TCN revision, and stay
separate; they meet on the run's timeline (absolute timestamps):

    TestRun
    ├── ManualLogEntry → ManualMeasurement   readings taken by a technician
    └── DcsDataset                           a LabVIEW/DCS export, kept unchanged

Deleting a run's evidence is never a side effect: the foreign keys to
TestRun protect it (a draft TCN whose run holds evidence cannot be deleted).

Whether evidence may be recorded for a run whose TCN is still a Draft is a
lifecycle/authorization rule, deliberately not a model constraint: it is
allowed during development and validation (intentional temporary policy) and
will be refused in production (locks.result_refusal,
TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN).
"""

from django.conf import settings
from django.db import models

from testlab.tcn.models import TestRun


class ManualLogEntry(models.Model):
    """One manual observation at a point in the run: readings, an event note, or both.

    Not an Excel row: date/time is one value, elapsed hours and leakage
    rates are calculated (manual_log.py), the person is the logged-in user.

    The log is also the run's event timeline (M2B): an entry with a category
    and/or a note is an observation or event (flow adjusted, test stopped,
    abnormal noise, …). Its absolute time places it on the DCS time axis, so
    events can later be drawn as plot markers without another model.
    """

    class Category(models.TextChoices):
        START = "start", "Start / restart"
        STOP = "stop", "Stop"
        ADJUSTMENT = "adjustment", "Adjustment"
        LEAKAGE_COLLECTION = "leakage_collection", "Leakage collection"
        ABNORMAL = "abnormal", "Abnormal observation"
        INSPECTION = "inspection", "Inspection"
        OTHER = "other", "Other"

    test_run = models.ForeignKey(TestRun, on_delete=models.PROTECT, related_name="manual_log_entries")
    observed_at = models.DateTimeField("Date / time")
    note = models.TextField("Note / event", blank=True)
    category = models.CharField("Event", max_length=20, choices=Category.choices, blank=True)
    # Leakage is collected since the previous reading (legacy practice). When the
    # collection did not start at the previous reading, the technician enters the
    # collection period; it then replaces the time since the previous reading.
    leakage_hours = models.DecimalField(
        "Leakage collected over", max_digits=8, decimal_places=3, null=True, blank=True,
        help_text="Hours. Leave empty: since the previous reading.",
    )
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    entered_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    # Imported from an Excel log (M2F): the workbook row it came from. entered_by is then the person who
    # imported it; who took the reading in the lab is the row's source_operator.
    source_row = models.ForeignKey(
        "ManualLogImportRow", on_delete=models.PROTECT, null=True, blank=True, related_name="entries"
    )

    class Meta:
        ordering = ["test_run", "observed_at", "pk"]
        indexes = [models.Index(fields=["test_run", "observed_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(leakage_hours__isnull=True) | models.Q(leakage_hours__gt=0),
                name="leakage_hours_positive",
            ),
        ]

    def __str__(self):
        return f"{self.test_run} reading {self.observed_at:%d-%b-%Y %H:%M}"


class ManualMeasurement(models.Model):
    """One entered value. `code` is from measurements.py; the unit is stored as entered."""

    entry = models.ForeignKey(ManualLogEntry, on_delete=models.CASCADE, related_name="measurements")
    code = models.CharField(max_length=40)
    value = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.CharField(max_length=10)

    class Meta:
        ordering = ["entry", "pk"]
        constraints = [models.UniqueConstraint(fields=["entry", "code"], name="one_value_per_code_per_entry")]

    def __str__(self):
        return f"{self.code} = {self.value} {self.unit}"


class DcsDataset(models.Model):
    """A LabVIEW/DCS export attached to a run.

    The uploaded file is stored unchanged (raw_file, sha256) and stays the
    source of truth. What the import learned from it is stored here; samples
    are not stored as rows. A derived column file (columns_file) holds the
    parsed numbers for fast plotting and is rebuilt from the raw file when
    missing.
    """

    test_run = models.ForeignKey(TestRun, on_delete=models.PROTECT, related_name="dcs_datasets")
    raw_file = models.FileField(max_length=255)
    original_name = models.CharField("Source file name", max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    columns_file = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    # Parsed structure (labview.py)
    encoding = models.CharField(max_length=20)
    delimiter = models.CharField(max_length=4)
    header_lines = models.JSONField(default=list)  # metadata lines before the signal names, as read
    signals = models.JSONField(default=list)  # [{"column", "name", "unit"}], every column in file order
    time_column = models.PositiveSmallIntegerField()
    time_unit = models.CharField(max_length=20, blank=True)  # unit as written in the file, e.g. "Hrs"
    sample_count = models.PositiveIntegerField()
    time_start_h = models.FloatField()
    time_end_h = models.FloatField()
    recording_started_at = models.DateTimeField(null=True, blank=True)
    recording_start_note = models.CharField(max_length=200, blank=True)  # how the start was determined, or why not
    warnings = models.JSONField(default=list)

    # Signal mapping (signal_mapping.py): {"<column>": {"code", "unit_confirmed"}} — which TestLab/TCN
    # parameter a signal is. Used only once reviewed; a mapping reused from a format reviewed earlier
    # counts as reviewed (mapping_from).
    mapping = models.JSONField(default=dict, blank=True)
    mapping_reviewed_at = models.DateTimeField(null=True, blank=True)
    mapping_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    mapping_from = models.ForeignKey(
        "DcsSignalMap", on_delete=models.PROTECT, null=True, blank=True, related_name="datasets"
    )

    class Meta:
        ordering = ["test_run", "imported_at", "pk"]

    def __str__(self):
        return self.original_name

    @property
    def duration_h(self):
        return self.time_end_h - self.time_start_h

    def value_signals(self):
        return [signal for signal in self.signals if signal["column"] != self.time_column]

    @property
    def mapping_reviewed(self):
        return self.mapping_reviewed_at is not None


class DcsSignalMap(models.Model):
    """A reviewed signal mapping of one export format, reused for later imports of the same format.

    The format is identified by its signals (names and units, in column
    order; `signature`). A dataset whose signals match exactly takes the
    mapping as reviewed; any difference in signals or units needs a review.
    """

    signature = models.CharField(max_length=64, unique=True)
    signals = models.JSONField()  # [{"name", "unit", "code", "unit_confirmed"}] of the value signals, in order
    reviewed_at = models.DateTimeField()
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    source_dataset = models.ForeignKey(
        DcsDataset, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return f"DCS signal mapping {self.signature[:12]}"


class DcsRunningDetermination(models.Model):
    """When the shaft was running during one DCS recording: one determination (running_intervals.py).

    The recording is never trimmed or changed. A determination states which
    stretches of the recording's elapsed-hours axis (t = 0 of the file
    unchanged) were shaft running, how that was found and who stands behind
    it:

    * detected — automatic, from the reviewed speed signal and the stored
      settings; used provisionally (a Draft may show it, marked provisional);
    * confirmed — an engineer accepted the detected intervals;
    * overridden — an engineer set the effective intervals, with a reason;
      the detected intervals are kept beside them.

    Records are never edited: a confirmation, an edit or a redetection adds a
    record and marks the previous one superseded, so the history stays
    complete. Exactly one record per dataset is current (superseded_at empty).
    """

    class Status(models.TextChoices):
        DETECTED = "detected", "Detected (not confirmed)"
        CONFIRMED = "confirmed", "Confirmed"
        OVERRIDDEN = "overridden", "Overridden"

    dataset = models.ForeignKey(DcsDataset, on_delete=models.PROTECT, related_name="running_determinations")
    dataset_sha256 = models.CharField(max_length=64)  # the recording the intervals were found in

    # The speed signal used: as in the file, and the mapping review it was taken from.
    speed_column = models.PositiveSmallIntegerField()
    speed_name = models.CharField(max_length=200)
    speed_unit = models.CharField(max_length=40, blank=True)
    mapping_reviewed_at = models.DateTimeField(null=True, blank=True)

    method = models.CharField(max_length=40)
    method_version = models.PositiveSmallIntegerField()
    threshold_rpm = models.FloatField()  # running: speed ≥ threshold
    max_dropout_s = models.FloatField()  # a shorter dip below the threshold between running samples is bridged
    min_run_s = models.FloatField()  # a shorter run above the threshold is ignored as a spike
    gap_limit_s = models.FloatField()  # a longer step between samples is missing data (unknown)

    # Hours on the recording's own axis. detected: {"intervals": [{"start_h", "end_h", "start_at", "end_at"}],
    # "coverage": [[start_h, end_h]], "gaps", "dropouts", "spikes": [[start_h, end_h]]}.
    detected = models.JSONField(default=dict)
    effective_intervals = models.JSONField(default=list)  # [{"start_h", "end_h", "start_at", "end_at"}]

    status = models.CharField(max_length=12, choices=Status.choices)
    reason = models.TextField(blank=True)  # why overridden (required) or redetected
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField()
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["dataset", "recorded_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset"], condition=models.Q(superseded_at__isnull=True),
                name="one_current_running_determination",
            ),
            models.CheckConstraint(condition=models.Q(threshold_rpm__gt=0), name="running_threshold_positive"),
        ]

    def __str__(self):
        return f"{self.dataset} running intervals ({self.status})"

    @property
    def is_current(self):
        return self.superseded_at is None


class ManualLogLayoutMap(models.Model):
    """A reviewed column mapping of an Excel log layout (M2F), reused for workbooks with the same layout.

    Identified by the layout fingerprint (the header cells in column order, log_import.mapping) and the
    run configuration it was reviewed for: the same column can be a different circuit in another
    arrangement. Applied as a starting point only; every import shows it for review.
    """

    fingerprint = models.CharField(max_length=64)
    arrangement = models.CharField(max_length=24)
    plug_seal_present = models.BooleanField()
    columns = models.JSONField()  # [{"letter", "header", "target"}]
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField()
    source_batch = models.ForeignKey(
        "ManualLogImport", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["fingerprint", "arrangement", "plug_seal_present"], name="one_log_layout_map_per_configuration"
            )
        ]

    def __str__(self):
        return f"Log layout {self.fingerprint[:12]} ({self.arrangement})"


class ManualLogImport(models.Model):
    """One Excel manual log imported into a run (M2F): the import batch and its audit record.

    The workbook is kept byte for byte. The batch holds what TestLab read (header, columns, rows with
    their cells and formulas) and decided (a copy of the confirmed mapping, the run check, warnings);
    every entry it creates points to its source row. Mapping → Preview → Imported; an imported batch
    can be undone as a whole (Undone: its entries are removed, the record stays).
    """

    class Status(models.TextChoices):
        MAPPING = "mapping", "Mapping"
        PREVIEW = "preview", "Preview"
        IMPORTED = "imported", "Imported"
        UNDONE = "undone", "Undone"

    class SourceKind(models.TextChoices):
        # A TestLab-generated log template (field ids instead of header mapping) is a later source kind
        # feeding the same service.
        WORKBOOK = "workbook", "Excel workbook (header mapping)"

    test_run = models.ForeignKey(TestRun, on_delete=models.PROTECT, related_name="log_imports")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.MAPPING)
    source_kind = models.CharField(max_length=10, choices=SourceKind.choices, default=SourceKind.WORKBOOK)

    raw_file = models.FileField(max_length=255)
    original_name = models.CharField("Source file name", max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)

    sheet_name = models.CharField(max_length=100)
    sheet_names = models.JSONField(default=list)
    header_row = models.PositiveIntegerField()
    group_row = models.PositiveIntegerField(null=True, blank=True)
    columns = models.JSONField(default=list)  # [{"letter", "group", "label", "unit", "header"}] as read
    ignored_rows = models.JSONField(default=list)  # sheet rows without typed values (empty, formulas only)
    warnings = models.JSONField(default=list)

    layout_fingerprint = models.CharField(max_length=64)
    layout_map = models.ForeignKey(
        ManualLogLayoutMap, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # The mapping as confirmed for this batch: a copy, the reusable layout map may change later.
    mapping = models.JSONField(default=list)  # [{"letter", "target", "status", "reason", "reviewed"}]
    mapping_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    mapping_confirmed_at = models.DateTimeField(null=True, blank=True)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    imported_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    undone_at = models.DateTimeField(null=True, blank=True)
    undo_reason = models.TextField(blank=True)
    undo_details = models.JSONField(default=dict, blank=True)  # entries removed, those edited after import

    class Meta:
        ordering = ["test_run", "-uploaded_at", "-pk"]

    def __str__(self):
        return f"{self.test_run} log import {self.original_name}"

    @property
    def is_open(self):
        """Not imported yet: the mapping or preview can still change, or the draft can be discarded."""
        return self.status in (self.Status.MAPPING, self.Status.PREVIEW)


class ManualLogImportRow(models.Model):
    """One data row of an imported workbook: its cells as read, how TestLab interpreted it, what was decided.

    cells: {"<column letter>": {"k": kind, "v": value as text, "raw": number as stored in the file,
    "f": formula}}. interpreted: observed_at, values by code (decimal text), note, leakage period,
    errors and flags. Running hours and leakage rates in the workbook are kept in the cells only;
    TestLab calculates its own.
    """

    class Classification(models.TextChoices):
        NEW = "new", "New"
        DUPLICATE = "duplicate", "Possible duplicate"
        CONFLICT = "conflict", "Conflict"
        ERROR = "error", "Error"
        EMPTY = "empty", "Nothing to import"

    class Decision(models.TextChoices):
        IMPORT = "import", "Import"
        SKIP = "skip", "Skip"

    batch = models.ForeignKey(ManualLogImport, on_delete=models.CASCADE, related_name="rows")
    sheet_row = models.PositiveIntegerField()
    cells = models.JSONField(default=dict)
    interpreted = models.JSONField(default=dict)
    source_operator = models.CharField("Operator (source)", max_length=200, blank=True)
    classification = models.CharField(max_length=10, choices=Classification.choices, blank=True)
    matched_entry = models.ForeignKey(
        ManualLogEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    match_details = models.JSONField(default=dict, blank=True)
    decision = models.CharField(max_length=6, choices=Decision.choices, blank=True)  # "": not decided yet
    suggestions = models.JSONField(default=dict, blank=True)
    choices = models.JSONField(default=dict, blank=True)  # the engineer's choices: category, events, period
    outcome = models.JSONField(default=dict, blank=True)  # entries created: {"entries": [ids]}

    class Meta:
        ordering = ["batch", "sheet_row"]
        constraints = [models.UniqueConstraint(fields=["batch", "sheet_row"], name="one_import_row_per_sheet_row")]

    def __str__(self):
        return f"{self.batch.original_name} row {self.sheet_row}"
