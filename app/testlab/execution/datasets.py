"""Import and read LabVIEW/DCS datasets (M2A).

Storage (server file store, next to the issued TCN documents):

    projects/<project_no>/runs/<run_no>/dcs/<import time>-<file name>           the upload, byte for byte
    projects/<project_no>/runs/<run_no>/dcs/<import time>-<file name>.columns   derived: parsed numbers

The raw file is never changed and is the source of truth (SHA-256 stored).
The .columns file holds every sample at full resolution as float64 columns
(column-major, little-endian) so plots need not re-parse text; it is
rebuilt from the raw file whenever it is missing or does not fit.

Plots get at most `points` points per signal: when a time window holds
more samples, each bucket of samples is represented by its minimum and
maximum, so peaks and excursions stay visible. Nothing is discarded from
storage.
"""

import bisect
import hashlib
import math
import re
import sys
from array import array

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import labview, signal_mapping
from .models import DcsDataset

DEFAULT_PLOT_POINTS = 4000


class DatasetImportError(Exception):
    """The upload cannot be imported; the message says why."""


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "dataset")[:120]


def dataset_dir(test_run):
    return f"projects/{test_run.project.project_no}/runs/{test_run.display_no}/dcs"


def _columns_bytes(columns):
    data = bytearray()
    for column in columns:
        values = array("d", column)
        if sys.byteorder != "little":
            values.byteswap()
        data += values.tobytes()
    return bytes(data)


def import_dataset(test_run, uploaded, user):
    """Store an uploaded export unchanged, parse it and record what it contains."""
    max_bytes = settings.TESTLAB_DCS_MAX_UPLOAD_MB * 1024 * 1024
    if uploaded.size > max_bytes:
        raise DatasetImportError(f"The file is larger than {settings.TESTLAB_DCS_MAX_UPLOAD_MB} MB.")
    content = uploaded.read()
    original_name = uploaded.name
    try:
        parsed = labview.parse(content, original_name)
    except labview.LabviewFormatError as error:
        raise DatasetImportError(f"{original_name} could not be read: {error}") from error

    storage = DcsDataset._meta.get_field("raw_file").storage
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    name = f"{dataset_dir(test_run)}/{stamp}-{_safe_name(original_name)}"
    written = []
    try:
        with transaction.atomic():
            raw_name = storage.save(name, ContentFile(content))
            written.append(raw_name)
            with storage.open(raw_name, "rb") as stored:
                if stored.read() != content:
                    raise DatasetImportError(f"{original_name} was not stored correctly; nothing was imported.")
            columns_name = storage.save(raw_name + ".columns", ContentFile(_columns_bytes(parsed.columns)))
            written.append(columns_name)
            time = parsed.time_h
            started_at = parsed.recording_started_at
            if started_at is not None:
                started_at = timezone.make_aware(started_at)
            dataset = DcsDataset.objects.create(
                test_run=test_run,
                raw_file=raw_name,
                original_name=original_name[:255],
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                columns_file=columns_name,
                imported_by=user,
                encoding=parsed.encoding,
                delimiter=parsed.delimiter,
                header_lines=parsed.header_lines,
                signals=[
                    {"column": index, "name": name, "unit": unit}
                    for index, (name, unit) in enumerate(zip(parsed.names, parsed.units))
                ],
                time_column=parsed.time_column,
                time_unit=parsed.time_unit,
                sample_count=parsed.sample_count,
                time_start_h=min(time),
                time_end_h=max(time),
                recording_started_at=started_at,
                recording_start_note=parsed.recording_start_note,
                warnings=parsed.warnings,
            )
            if signal_mapping.apply_known_mapping(dataset):  # same format reviewed before: reused
                from . import running_intervals  # builds on this module

                running_intervals.ensure_current(dataset, user)  # detected: provisional until reviewed
            return dataset
    except BaseException:
        for stored_name in written:
            storage.delete(stored_name)
        raise


def deletion_refusal(dataset):
    """Why an imported dataset cannot be deleted, or "" (e.g. a recording imported mid-test, later replaced
    by the complete one). Refused while the Result is final, once a final revision holds it (it was evidence
    when that revision was finalized), and while Result plots are made from it (remove those first)."""
    from testlab import locks

    if refusal := locks.result_refusal(dataset.test_run):
        return refusal
    from testlab.results.models import ResultRevision

    finalized = ResultRevision.objects.filter(result__test_run=dataset.test_run, finalized_at__gte=dataset.imported_at)
    if revision := finalized.order_by("-number").first():
        return f"{dataset} is on record in final revision {revision.number} and cannot be deleted."
    if count := dataset.result_plots.count():
        plural = "s are" if count > 1 else " is"
        them = "them" if count > 1 else "it"
        return f"{count} Result plot{plural} made from {dataset}: remove {them} from the Result first."
    return ""


def delete_dataset(dataset):
    """Delete an imported dataset: its running determinations, the record and its stored files."""
    if refusal := deletion_refusal(dataset):
        raise DatasetImportError(refusal)
    storage = dataset.raw_file.storage
    names = [n for n in (dataset.raw_file.name, dataset.columns_file) if n]
    with transaction.atomic():
        dataset.running_determinations.all().delete()
        dataset.delete()
        transaction.on_commit(lambda: [storage.delete(n) for n in names if storage.exists(n)])


def load_columns(dataset):
    """All samples as one array("d") per column (time column in hours)."""
    storage = dataset.raw_file.storage
    width, count = len(dataset.signals), dataset.sample_count
    if dataset.columns_file and storage.exists(dataset.columns_file):
        with storage.open(dataset.columns_file, "rb") as stored:
            data = stored.read()
        if len(data) == width * count * 8:
            values = array("d")
            values.frombytes(data)
            if sys.byteorder != "little":
                values.byteswap()
            return [values[i * count : (i + 1) * count] for i in range(width)]
    return rebuild_columns(dataset)


def rebuild_columns(dataset):
    """Parse the raw file again and rewrite the derived column file."""
    storage = dataset.raw_file.storage
    with storage.open(dataset.raw_file.name, "rb") as stored:
        content = stored.read()
    if hashlib.sha256(content).hexdigest() != dataset.sha256:
        raise DatasetImportError(f"The stored file of {dataset} has changed since import (SHA-256 mismatch).")
    parsed = labview.parse(content, dataset.original_name)
    name = dataset.columns_file or dataset.raw_file.name + ".columns"
    if storage.exists(name):
        storage.delete(name)
    dataset.columns_file = storage.save(name, ContentFile(_columns_bytes(parsed.columns)))
    dataset.save(update_fields=["columns_file"])
    return parsed.columns


def _rounded(values, digits):
    return [None if math.isnan(v) else round(v, digits) for v in values]


def _min_max(time, values, first, last, buckets):
    """Indices of the minimum and maximum of each bucket of samples, in time order."""
    indices = []
    size = (last - first) / buckets
    for bucket in range(buckets):
        a = first + int(bucket * size)
        b = first + int((bucket + 1) * size) if bucket < buckets - 1 else last
        finite = [i for i in range(a, b) if values[i] == values[i]]
        if not finite:
            if a < b:
                indices.append(a)  # a gap: keeps the NaN so the line breaks
            continue
        low = min(finite, key=values.__getitem__)
        high = max(finite, key=values.__getitem__)
        indices.extend(sorted({low, high}))
    return indices


def plot_data(dataset, columns=None, start_h=None, end_h=None, points=DEFAULT_PLOT_POINTS):
    """Traces for a time window: full resolution when it fits, min/max buckets otherwise."""
    data = load_columns(dataset)
    time = data[dataset.time_column]
    wanted = [s for s in dataset.value_signals() if columns is None or s["column"] in columns]
    ordered = all(b >= a for a, b in zip(time, time[1:]))
    first, last = 0, len(time)
    if ordered:
        if start_h is not None:
            first = bisect.bisect_left(time, start_h)
        if end_h is not None:
            last = bisect.bisect_right(time, end_h)
    in_window = max(last - first, 0)
    decimated = ordered and in_window > points
    traces = []
    for signal in wanted:
        values = data[signal["column"]]
        if decimated:
            indices = _min_max(time, values, first, last, max(points // 2, 1))
            x = [time[i] for i in indices]
            y = [values[i] for i in indices]
        else:
            x, y = time[first:last], values[first:last]
        traces.append({"column": signal["column"], "x": _rounded(x, 6), "y": _rounded(y, 4)})
    return {
        "samples_in_window": in_window,
        "decimated": decimated,
        "points_per_signal": max((len(t["x"]) for t in traces), default=0),
        "traces": traces,
    }
