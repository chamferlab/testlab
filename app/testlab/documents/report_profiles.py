"""Report profiles and content selection of the Test Run Report (M2F-B).

A profile controls presentation, never calculation:

    record context (run_report_context, complete)  ──present(profile, selection)──►  rendered context

present() takes the record — the live one for a Draft, the frozen one of a
final revision — and only removes what the selection hides, adds the
mandatory qualifications and renumbers the sections. It calculates
nothing, so every profile shows the same numbers.

Selection, stored per Result and profile (results.ReportSelection), only the
engineer's overrides of the profile defaults:

    {"flags":    {key: bool},                                   explanatory notes and optional parts
     "slots":    {"<component id>:<kind>": {"summary": bool, "detail": bool, "figures": [code], "remark": bool}},
     "pictures": {"<picture id>": bool},
     "plots":    {"<plot id>": bool}}

Never hideable (LOCKED): identification and document identity, configuration
and description, the test conditions values, the leakage table with its
requirements and assessments, every acceptance row, the narrative, and the
mandatory qualifications — a limitation that changes how a result must be
read is stated whenever the detail that would show it is hidden.
"""

import copy
from dataclasses import dataclass

from django.conf import settings

CUSTOMER = "customer"
ENGINEERING = "engineering"
PROFILES = {CUSTOMER: "Customer", ENGINEERING: "Engineering"}
PROFILE_CODES = {CUSTOMER: "C", ENGINEERING: "E"}


@dataclass(frozen=True)
class Flag:
    key: str
    label: str
    customer: bool
    engineering: bool

    def default(self, profile):
        return self.customer if profile == CUSTOMER else self.engineering


FLAGS = [
    Flag("running_basis_note", "Shaft-running determination (how the running time was found)", False, True),
    Flag("recording_notes", "Recording and logged-period notes (file names, basis of t = 0)", False, True),
    Flag("objective_source", "Source of the objective", False, True),
    Flag("conditions_source", "Source column of the test conditions", False, True),
    Flag("conditions_note", "Full explanation of the actual ranges (a short note is always given)", False, True),
    Flag("leakage_details", "Leakage details per seal: uncovered running time, static and mixed-period "
         "collections, readings not in the average", False, True),
    Flag("leakage_method_note", "Leakage calculation method", False, True),
    Flag("leakage_chart", "Cumulative collected-volume chart", True, True),
    Flag("plot_provenance", "DCS plot sources (dataset, SHA-256, sampling)", False, True),
    Flag("events_table", "Events table (the observations are always shown)", False, True),
    Flag("measurement_notes", "Explanations under the measurement tables", False, True),
    Flag("measurement_sources", "Measurement files, checksums and calculation versions", False, True),
    Flag("wear_note", "Basis of the wear rate", True, True),
    Flag("acceptance_sources", "Acceptance: requirement sources and how each criterion was assessed", False, True),
    Flag("acceptance_note", "Acceptance method note", False, True),
    Flag("annex_manual_log", "Annex A: manually logged data", False, True),
]  # fmt: skip
FLAG_KEYS = {f.key for f in FLAGS}

LOCKED = [
    "Identification, report status and document identity",
    "Test description and seal configuration",
    "Test conditions: instructed and actual values",
    "Leakage summary: totals, average dynamic leakage, requirement and assessment",
    "Acceptance assessment: every criterion with requirement, actual, assessment and remark",
    "Observations, conclusion and recommendation",
    "Mandatory qualifications: limited leakage coverage, unknown shaft state, provisional data",
]

SLOT_PARTS = ("summary", "detail", "figures", "remark")


def figure_choices(kind):
    from testlab.results.seal_faces import REPORT_FIGURES

    return dict(REPORT_FIGURES.get(kind, {}))


def slot_default(profile, kind):
    """Defaults of a measurement slot (component × kind, Pre and Post together)."""
    from testlab.results.seal_faces import DEFAULT_FIGURES

    detailed = kind in ("mahr", "roundpak")
    if profile == CUSTOMER:
        return {"summary": True, "detail": False, "figures": [], "remark": False}
    return {"summary": True, "detail": detailed, "figures": list(DEFAULT_FIGURES.get(kind, [])) if detailed else [],
            "remark": True}  # fmt: skip


def resolve_slot(profile, kind, stored):
    found = slot_default(profile, kind)
    for part in SLOT_PARTS:
        if part in (stored or {}):
            found[part] = stored[part]
    allowed = figure_choices(kind)
    found["figures"] = [code for code in allowed if code in found["figures"]]
    if kind not in ("mahr", "roundpak"):
        found["detail"], found["figures"] = False, []
    return found


def resolve(profile, stored, slots=(), pictures=(), plots=()):
    """The complete selection: the stored overrides over the profile defaults.

    slots: (key, kind) pairs; pictures, plots: ids — the items there are to select.
    """
    stored = stored or {}
    flags = {f.key: f.default(profile) for f in FLAGS}
    flags.update({k: bool(v) for k, v in (stored.get("flags") or {}).items() if k in FLAG_KEYS})
    stored_slots = stored.get("slots") or {}
    return {
        "flags": flags,
        "slots": {key: resolve_slot(profile, kind, stored_slots.get(key)) for key, kind in slots},
        "pictures": {str(i): bool((stored.get("pictures") or {}).get(str(i), True)) for i in pictures},
        "plots": {str(i): bool((stored.get("plots") or {}).get(str(i), True)) for i in plots},
    }


def clean(stored):
    """A stored selection without unknown keys or values (what the database keeps)."""
    stored = stored or {}
    slots = {}
    for key, value in (stored.get("slots") or {}).items():
        value = {p: value[p] for p in SLOT_PARTS if p in (value or {})}
        if "figures" in value:
            value["figures"] = [str(c) for c in value["figures"]]
        slots[str(key)] = {p: (v if p == "figures" else bool(v)) for p, v in value.items()}
    found = {
        "flags": {k: bool(v) for k, v in (stored.get("flags") or {}).items() if k in FLAG_KEYS},
        "slots": slots,
        "pictures": {str(k): bool(v) for k, v in (stored.get("pictures") or {}).items()},
        "plots": {str(k): bool(v) for k, v in (stored.get("plots") or {}).items()},
    }
    return {part: value for part, value in found.items() if value}


# ---- what a record holds ----------------------------------------------------------------------


def record_items(record):
    """(slots, picture ids, plot ids) of a record; schema 1 (before M2F-B) has no ids: nothing to select."""
    slots = {}
    for group in record.get("measurements", []):
        for row in group.get("rows", []):
            if "component_id" in group and "kind" in row:
                slots.setdefault(f"{group['component_id']}:{row['kind']}", row["kind"])
    for block in record.get("measurement_details", []):
        if "slot" in block:
            slots.setdefault(block["slot"], block["kind"])
    pictures = [p["id"] for p in _all_pictures(record) if "id" in p]
    plots = [p["id"] for p in record.get("plots", []) if "id" in p]
    return list(slots.items()), pictures, plots


def _all_pictures(record):
    pictures = record.get("pictures") or {}
    found = list(pictures.get("cross_sections", [])) + list(pictures.get("other", []))
    for comparison in pictures.get("comparisons", []):
        for pair in comparison["pairs"]:
            found += [p for p in (pair["pre"], pair["post"]) if p]
    for group in pictures.get("single_stage", []):
        for row in group["rows"]:
            found += [item["picture"] for item in row]
    return found


def selection_for(record, profile, stored):
    slots, pictures, plots = record_items(record)
    return resolve(profile, stored, slots, pictures, plots)


# ---- mandatory qualifications -------------------------------------------------------------------


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def qualifications(record, flags, threshold=None):
    """Short statements a report keeps whatever its selection: {section: [text]}.

    Always: provisional data (t = 0 from the first reading, unconfirmed running intervals). When the
    detail that would show it is hidden: unknown shaft state, recording edges, and leakage collections
    covering less than the coverage threshold of the shaft-running time.
    """
    threshold = settings.TESTLAB_LEAKAGE_COVERAGE_WARNING_PCT if threshold is None else threshold
    found = {"identification": [], "leakage": []}
    ident = record.get("identification", {})
    if ident.get("provisional") and ident.get("start"):
        found["identification"].append("No recording start is known: t = 0 is the first manual log reading "
                                        "(provisional).")  # fmt: skip
    if ident.get("running_provisional"):
        found["identification"].append("Provisional: the running intervals are not confirmed yet.")
    if not flags.get("running_basis_note"):
        if _float(ident.get("unknown_hours")):  # "0.00": seconds only, nothing worth stating
            found["identification"].append(f"{ident['unknown_hours']} h of the logged period have no evidence of "
                                           "the shaft state and are not counted as running.")  # fmt: skip
        for note in ident.get("running_notes", []):
            found["identification"].append(f"{note[0].upper()}{note[1:]}.")
    if not flags.get("leakage_details"):
        for seal in record.get("leakage", []):
            coverage, running = seal.get("coverage_pct"), seal.get("running_hours")
            if coverage is None and seal.get("uncovered"):  # schema 1: from the formatted hours
                covered, uncovered = _float(seal.get("dynamic_hours")) or 0.0, _float(seal["uncovered"])
                running = covered + uncovered if uncovered is not None else None
                coverage = 100 * covered / running if running else None
            if coverage is not None and coverage < threshold:
                found["leakage"].append(
                    f"{seal['seal']}: the dynamic collections cover {seal['dynamic_hours']} h of {running:.2f} h shaft "
                    f"running ({coverage:.0f} %); the average dynamic leakage applies to the covered time only."
                )  # fmt: skip
    return found


# ---- presentation -------------------------------------------------------------------------------


def present(record, profile, stored=None, document=None):
    """The rendered context of a profile: the record with the selection applied (a new dict)."""
    from .run_report_context import _section_numbers

    if profile not in PROFILES:
        raise ValueError(f"Unknown report profile {profile!r}.")
    ctx = copy.deepcopy(record)
    selection = selection_for(record, profile, stored)
    flags = selection["flags"]
    ctx["profile"] = {"code": profile, "label": PROFILES[profile]}
    ctx["show"] = dict(flags)
    ctx["selection"] = selection
    ctx["document"] = document or {}
    ctx["is_preview"] = not ctx["document"].get("identity")
    if ctx["document"].get("issued_at"):
        ctx["generated"] = ctx["document"]["issued_at"]
    ctx["qualifications"] = qualifications(record, flags)
    ident = ctx.get("identification") or {}
    if ident.get("unknown_hours") and not _float(ident["unknown_hours"]):
        ident["unknown_hours"] = ""  # rounds to 0.00 h (records frozen before this was dropped at the source)
    slots = selection["slots"]

    def slot(component_id, kind):
        return slots.get(f"{component_id}:{kind}") or slot_default(profile, kind)

    # Pre/Post summary and face wear: per measurement slot.
    groups = []
    for group in ctx.get("measurements", []):
        if "component_id" in group:
            group["rows"] = [r for r in group["rows"] if slot(group["component_id"], r["kind"])["summary"]]
        if group["rows"]:
            groups.append(group)
    ctx["measurements"] = groups
    ctx["wear"] = [w for w in ctx.get("wear", []) if "component_id" not in w
                   or slot(w["component_id"], "dimensional")["summary"]]  # fmt: skip

    # Mahr / ROUNDPAK detail blocks, their figures and the measurement remarks.
    details, remarks = [], []
    for block in ctx.get("measurement_details", []):
        chosen = slots.get(block["slot"]) if "slot" in block else None
        if chosen is None:  # schema 1: as frozen
            chosen = {"detail": True, "figures": [f.get("code") for f in block["figures"]], "remark": True}
        shown_in_detail = chosen["detail"] and flags["measurement_sources"]
        for stage, source in block["sources"].items():
            if not source or not source.get("remark"):
                continue
            if not chosen["remark"]:
                source["remark"] = ""
            elif not shown_in_detail:
                remarks.append({"label": f"{block['title']} · {'Pre-test' if stage == 'pre' else 'Post-test'}",
                                "text": source["remark"]})  # fmt: skip
        if not chosen["detail"]:
            continue
        block["figures"] = [f for f in block["figures"] if f.get("code") in chosen["figures"] and f.get("grid")]
        if not flags["measurement_notes"]:
            block["notes"] = []
        details.append(block)
    ctx["measurement_details"] = details
    ctx["measurement_remarks"] = remarks

    # Pictures and plots: one by one.
    def keep(picture):
        return picture is not None and selection["pictures"].get(str(picture.get("id")), True)

    pictures = ctx.get("pictures") or {}
    pictures["cross_sections"] = [p for p in pictures.get("cross_sections", []) if keep(p)]
    pictures["other"] = [p for p in pictures.get("other", []) if keep(p)]
    comparisons = []
    for comparison in pictures.get("comparisons", []):
        pairs = [{"pre": pair["pre"] if keep(pair["pre"]) else None, "post": pair["post"] if keep(pair["post"])
                  else None} for pair in comparison["pairs"]]  # fmt: skip
        pairs = [pair for pair in pairs if pair["pre"] or pair["post"]]
        if pairs:
            comparisons.append({**comparison, "pairs": pairs})
    pictures["comparisons"] = comparisons
    single = []
    for group in pictures.get("single_stage", []):
        items = [item for row in group["rows"] for item in row if keep(item["picture"])]
        if items:
            single.append({**group, "rows": [items[i : i + 2] for i in range(0, len(items), 2)]})
    pictures["single_stage"] = single
    plots = [p for p in ctx.get("plots", []) if selection["plots"].get(str(p.get("id")), True)]
    for number, plot in enumerate(plots, start=1):
        plot["number"] = number
    ctx["plots"] = plots

    # Optional parts.
    if not flags["leakage_chart"]:
        ctx["leakage_chart"] = ""
    if not flags["events_table"]:
        ctx["events"] = []
        for table in ctx.get("log", {}).get("tables", []):
            for row in table["rows"]:
                row["event"] = ""
    if not flags["annex_manual_log"] and "log" in ctx:
        ctx["log"]["tables"] = []

    shown = {p["src"] for p in _all_pictures(ctx)} | {p["src"] for p in ctx["plots"]}
    ctx["assets"] = [a for a in ctx.get("assets", []) if a["path"] in shown]
    ctx["sections"] = _section_numbers(ctx)
    return ctx
