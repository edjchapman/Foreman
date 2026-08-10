"""`manage.py check --deploy` is clean once the production env vars are set.

Django forces DEBUG=False in tests, so the hardening settings are env-opt-in (default off)
rather than `if not DEBUG` — this subprocess runs the deploy check in a fresh process with a
production environment and asserts zero warnings (`--fail-level WARNING`).
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROD_ENV = {
    "DJANGO_DEBUG": "false",
    "DJANGO_SECRET_KEY": "deploy-check-secret-key-with-enough-length-and-0123456789",
    "DJANGO_ALLOWED_HOSTS": "foreman.example.com",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://foreman.example.com",
    "DJANGO_SECURE_SSL_REDIRECT": "true",
    "DJANGO_SECURE_COOKIES": "true",
    "DJANGO_SECURE_HSTS_SECONDS": "31536000",
    "DATABASE_URL": "sqlite://:memory:",
}


def test_deploy_check_passes_with_production_env():
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        cwd=_REPO_ROOT,
        env={**os.environ, **_PROD_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
