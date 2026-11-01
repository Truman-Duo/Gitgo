"""Large release files must not make the existing email scan quadratic."""
import re
import time

from backend.core.authorship import _DEFAULT_PRIVACY_PATTERNS


def test_email_scan_keeps_existing_matches_at_punctuation_and_multiple_at_signs():
    previous = re.compile(r'[\w.\-+]+@[\w.\-]+\.\w+')
    content = 'name+tag@example.test; (中文.name@domain.test) a@bad@valid.test /x.y+z@host.invalid; no-at-here'
    assert _DEFAULT_PRIVACY_PATTERNS["email"].findall(content) == previous.findall(content)


def test_email_scan_has_bounded_cost_on_large_artifact_lines():
    started = time.monotonic()
    for content in ('x' * (3 * 1024 * 1024), 'a' * 300000 + '@' + 'b' * 300000):
        assert _DEFAULT_PRIVACY_PATTERNS["email"].findall(content) == []
    assert time.monotonic() - started < 5
