"""Drift guard: every metric the committed alert rules reference must exist.

The alert/SLO rules in observability/prometheus/alerts.yml are plain YAML that
nothing type-checks — a metric rename in jobs/metrics.py would silently break
every alert. This cross-checks the rule file's `foreman_*` references against
the live /metrics exposition (regex over raw text, so no YAML dependency).
Recording-rule names (`foreman:...`) are Prometheus-side and deliberately not
matched by the pattern (they contain colons, not underscores after `foreman`).
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db

RULES_FILE = Path(__file__).resolve().parents[2] / "observability" / "prometheus" / "alerts.yml"


def test_alert_rules_reference_only_exported_metrics(api_client):
    rules_text = RULES_FILE.read_text()
    referenced = set(re.findall(r"\bforeman_[a-z0-9_]+\b", rules_text))
    assert referenced, "no foreman_* metrics found in alerts.yml — pattern or file moved?"

    exposition = api_client.get("/metrics").content.decode()
    missing = {name for name in referenced if name not in exposition}
    assert not missing, f"alerts.yml references metrics absent from /metrics: {sorted(missing)}"
