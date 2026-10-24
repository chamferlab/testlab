"""TestLab settings, driven by TESTLAB_* environment variables.

Development defaults: DEBUG on, SQLite file in <repo>/data/.
Production: set TESTLAB_DEBUG=0, TESTLAB_SECRET_KEY, TESTLAB_ALLOWED_HOSTS
and TESTLAB_DATABASE_URL (PostgreSQL). SQLite is for development/tests only.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


DEBUG = env_bool("TESTLAB_DEBUG", True)

SECRET_KEY = os.environ.get("TESTLAB_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("TESTLAB_SECRET_KEY must be set when TESTLAB_DEBUG is off.")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("TESTLAB_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "testlab",
    "testlab.accounts",
    "testlab.projects",
    "testlab.tcn",
    "testlab.execution",
    "testlab.results",
    "testlab.reports",
    "testlab.planning",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every page requires login unless the view is marked login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATA_DIR = Path(os.environ.get("TESTLAB_DATA_DIR", REPO_DIR / "data"))

DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("TESTLAB_DATABASE_URL", f"sqlite:///{DATA_DIR / 'testlab.sqlite3'}"),
        conn_max_age=60,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Normal users log in with their corporate email address; the username of
# a bootstrap account made with createsuperuser also works (accounts/backends.py).
AUTHENTICATION_BACKENDS = ["testlab.accounts.backends.EmailOrUsernameBackend"]

# Self-registration (stabilization decision S2): only these email domains may
# register, e.g. TESTLAB_ALLOWED_EMAIL_DOMAINS=example.com,example.org. Empty:
# registration is closed. New accounts are inactive until an administrator
# activates them in Django admin.
TESTLAB_ALLOWED_EMAIL_DOMAINS = [
    domain.lower().lstrip("@") for domain in env_list("TESTLAB_ALLOWED_EMAIL_DOMAINS", [])
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("TESTLAB_TIME_ZONE", "Europe/Amsterdam")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("TESTLAB_STATIC_ROOT", REPO_DIR / "staticfiles")

# Server-side file storage: issued documents under projects/<project_no>/…
# (M1 spec §9.4). Development default inside the git-ignored data directory.
TESTLAB_FILE_ROOT = Path(os.environ.get("TESTLAB_FILE_ROOT", DATA_DIR / "files"))
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": TESTLAB_FILE_ROOT},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Document templates of generated PDFs (documents/render.py): HTML template bundles.
TESTLAB_TEMPLATE_DIR = Path(os.environ.get("TESTLAB_TEMPLATE_DIR", APP_DIR / "doc_templates"))

# Seconds Chromium may take to print one PDF (documents/chromium.py).
TESTLAB_PDF_TIMEOUT = int(os.environ.get("TESTLAB_PDF_TIMEOUT", "120"))

# LabVIEW/DCS exports (execution/datasets.py): largest accepted upload. Files are
# read into memory once on import; the stored raw file is never changed.
TESTLAB_DCS_MAX_UPLOAD_MB = int(os.environ.get("TESTLAB_DCS_MAX_UPLOAD_MB", "200"))
# Largest Excel manual log accepted for import (M2F), in MB.
TESTLAB_LOG_IMPORT_MAX_MB = int(os.environ.get("TESTLAB_LOG_IMPORT_MAX_MB", "20"))

# Result pictures (results/images.py): largest accepted picture. The original is
# stored unchanged; a display copy is derived from it.
TESTLAB_RESULT_IMAGE_MAX_MB = int(os.environ.get("TESTLAB_RESULT_IMAGE_MAX_MB", "40"))

# Project drawings (projects/drawings.py): largest accepted tester drawing or P&ID. The file is
# stored unchanged; a picture also gets a display copy.
TESTLAB_DRAWING_MAX_MB = int(os.environ.get("TESTLAB_DRAWING_MAX_MB", "50"))

# PowerPoint slides of a project's tests (reports/slides.py): the company template, a .pptx kept
# out of git (it changes every year). By default data/slides-template.pptx when that file exists;
# without a template the slides are plain widescreen slides.
TESTLAB_SLIDES_TEMPLATE = os.environ.get("TESTLAB_SLIDES_TEMPLATE", "")
TESTLAB_SLIDES_TEMPLATE_DEFAULT = DATA_DIR / "slides-template.pptx"

# Shaft running (results/runtime.py): a speed at or above this value [rpm] counts as running.
# Manual log readings use it directly (the value used is in the final report snapshot). For a DCS
# recording it is only the default of a new running-interval determination
# (execution/running_intervals.py), which stores the values it used, together with the defaults of
# its dropout bridge and spike filter [s]. Changing these never changes a stored determination.
TESTLAB_RUNNING_SPEED_RPM = float(os.environ.get("TESTLAB_RUNNING_SPEED_RPM", "50"))
TESTLAB_RUNNING_MAX_DROPOUT_S = float(os.environ.get("TESTLAB_RUNNING_MAX_DROPOUT_S", "60"))
TESTLAB_RUNNING_MIN_RUN_S = float(os.environ.get("TESTLAB_RUNNING_MIN_RUN_S", "120"))
# A manual Stop event or speed reading this close [min] to a DCS start/stop is not reported as a
# discrepancy with the DCS running intervals (clocks and minute-resolution log times differ).
TESTLAB_RUNNING_DISCREPANCY_MIN = float(os.environ.get("TESTLAB_RUNNING_DISCREPANCY_MIN", "10"))

# Seal-face instrument files (results/seal_faces.py: Mahr TXT, ROUNDPAK CSV): largest accepted file.
# Stored unchanged as the evidence of the measurement.
TESTLAB_MEASUREMENT_MAX_MB = int(os.environ.get("TESTLAB_MEASUREMENT_MAX_MB", "20"))

# Report profiles (M2F-B): below this share [%] of the shaft-running time covered by dynamic leakage
# collections, a report that hides the leakage details states the coverage (a mandatory qualification,
# not an acceptance criterion).
TESTLAB_LEAKAGE_COVERAGE_WARNING_PCT = float(os.environ.get("TESTLAB_LEAKAGE_COVERAGE_WARNING_PCT", "95"))

# INTENTIONAL TEMPORARY POLICY (development and validation phase, decided
# 2026-10-03): manual log readings, DCS/LabVIEW imports and the Test Result
# (M2B) are allowed while the run's TCN is still a Draft. This is not missing validation.
# TODO(production): enforce "Issued TCN → execution permitted" by setting this
# to 1. The rule is a lifecycle/authorization check (execution/views.py,
# recording_refusal); ManualLogEntry, DcsDataset, TestResult and TestRun need no change.
TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN = env_bool("TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN", False)

# Usual project-number format. A mismatch only gives a warning (M1 spec V1).
TESTLAB_PROJECT_NO_PATTERN = os.environ.get("TESTLAB_PROJECT_NO_PATTERN", r"^\d{4}RSD\d{2}TL$")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
