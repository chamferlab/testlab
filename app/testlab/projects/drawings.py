"""Project drawings (Project Defaults): the tester drawing and the system P&ID.

Storage (server file store):

    projects/<project_no>/drawings/<upload time>-<file name>              the upload, byte for byte
    projects/<project_no>/drawings/<upload time>-<file name>.preview.jpg  display copy of a picture

The tester drawing is a PDF, TIFF, JPEG or PNG; the P&ID a PDF. The file is
never re-encoded (SHA-256 stored). A picture also gets a JPEG display copy,
rebuilt from the original when missing: browsers do not show TIFF. A PDF is
served as it is.

Uploading a drawing of a kind that already has one makes the new one the
current drawing; the earlier ones are kept as its history and are never
changed. Only the current drawing can be deleted (a wrong upload), which
makes the previous one current again, and only while no Final Report has
been issued with it (its appendices show the current drawings).
"""

import hashlib
import io
import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from testlab import locks

from .models import ProjectDrawing

Kind = ProjectDrawing.Kind
PREVIEW_MAX_PX = 2000
PICTURE_FORMATS = {"TIFF", "JPEG", "PNG"}
ACCEPTED = {Kind.TESTER: "PDF, TIFF, JPEG or PNG", Kind.PID: "PDF"}
NOUN = {Kind.TESTER: "tester drawing", Kind.PID: "system P&ID"}


class DrawingUploadError(Exception):
    """The upload is not a usable drawing; the message says why."""


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "drawing")[:120]


def drawing_dir(project):
    return f"projects/{project.project_no}/drawings"


def _storage():
    return ProjectDrawing._meta.get_field("file").storage


def _preview_bytes(content):
    with Image.open(io.BytesIO(content)) as image:  # the first page of a multi-page TIFF
        image = ImageOps.exif_transpose(image)
        image.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX))
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
    return out.getvalue()


def _file_format(content, name, kind):
    """PDF, TIFF, JPEG or PNG as read from the content (never from the file name), if accepted for the kind."""
    max_mb = settings.TESTLAB_DRAWING_MAX_MB
    if len(content) > max_mb * 1024 * 1024:
        raise DrawingUploadError(f"{name} is larger than {max_mb} MB.")
    refused = f"{name} is not a {ACCEPTED[kind]} file. A {NOUN[kind]} must be a {ACCEPTED[kind]}."
    if b"%PDF-" in content[:1024]:
        return "PDF"
    if kind != Kind.TESTER:
        raise DrawingUploadError(refused)
    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            image.verify()
    except Image.DecompressionBombError as error:
        raise DrawingUploadError(
            f"{name} has too many pixels to display; save it at a lower resolution or as a PDF."
        ) from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise DrawingUploadError(refused) from error
    if image_format not in PICTURE_FORMATS:
        raise DrawingUploadError(refused)
    return image_format


def current(project, kind):
    return project.drawings.filter(kind=kind).order_by("-uploaded_at", "-pk").first()


def add_drawing(project, kind, uploaded, user, drawing_no, revision=""):
    """Store one uploaded drawing unchanged (plus a display copy of a picture); it becomes the current one."""
    content = uploaded.read()
    original_name = uploaded.name
    file_format = _file_format(content, original_name, kind)
    preview = None if file_format == "PDF" else _preview_bytes(content)
    storage = _storage()
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    name = f"{drawing_dir(project)}/{stamp}-{_safe_name(original_name)}"
    written = []
    try:
        with transaction.atomic():
            file_name = storage.save(name, ContentFile(content))
            written.append(file_name)
            preview_name = ""
            if preview is not None:
                preview_name = storage.save(f"{file_name}.preview.jpg", ContentFile(preview))
                written.append(preview_name)
            return ProjectDrawing.objects.create(
                project=project,
                kind=kind,
                drawing_no=drawing_no.strip(),
                revision=revision.strip(),
                file=file_name,
                original_name=original_name,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                file_format=file_format,
                preview_file=preview_name,
                uploaded_by=user,
            )
    except Exception:
        for written_name in written:
            storage.delete(written_name)
        raise


def preview_name(drawing):
    """The display copy of a picture, rebuilt from the original when it is missing."""
    storage = _storage()
    if drawing.preview_file and storage.exists(drawing.preview_file):
        return drawing.preview_file
    with drawing.file.open("rb") as original:
        content = original.read()
    name = storage.save(f"{drawing.file.name}.preview.jpg", ContentFile(_preview_bytes(content)))
    ProjectDrawing.objects.filter(pk=drawing.pk).update(preview_file=name)
    drawing.preview_file = name
    return name


def pictures(drawing, max_px=2400, max_pages=None):
    """The drawing as pictures for documents: the display copy of a picture, or every page of a PDF as PNG
    (the first `max_pages`)."""
    if not drawing.is_pdf:
        with _storage().open(preview_name(drawing), "rb") as preview:
            return [preview.read()]
    import pypdfium2

    with drawing.file.open("rb") as stored:
        document = pypdfium2.PdfDocument(stored.read())
    found = []
    try:
        for index in range(min(len(document), max_pages or len(document))):
            page = document[index]
            width, height = page.get_size()
            image = page.render(scale=min(max_px / max(width, height), 300 / 72)).to_pil()
            out = io.BytesIO()
            image.convert("RGB").save(out, "PNG")
            found.append(out.getvalue())
    finally:
        document.close()
    return found


def delete_drawing(drawing):
    """Remove the current drawing (a wrong upload): the record, the file and its display copy."""
    if lock := locks.drawing_lock(drawing):
        raise DrawingUploadError(lock)
    storage = _storage()
    names = [drawing.file.name, drawing.preview_file]
    with transaction.atomic():
        drawing.delete()
        transaction.on_commit(lambda: [storage.delete(n) for n in names if n and storage.exists(n)])


def drawing_sets(project):
    """Per kind: the current drawing and the earlier ones, newest first."""
    drawings = list(project.drawings.select_related("uploaded_by").order_by("-uploaded_at", "-pk"))
    sets = []
    for kind in Kind:
        of_kind = [d for d in drawings if d.kind == kind]
        sets.append({"kind": kind, "label": kind.label, "accepted": ACCEPTED[kind],
                     "current": of_kind[0] if of_kind else None, "earlier": of_kind[1:]})  # fmt: skip
    return sets
