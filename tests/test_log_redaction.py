"""Unit tests for secret redaction in the sandbox log tail.

These exercise the pure helpers (``_redact`` / ``_log_tail``) directly so the
redaction contract is pinned independent of any adapter or subprocess.
"""
from plugin_runner.sandbox import (
    LOG_TAIL_MAX_BYTES,
    REDACTION_MARKER,
    _log_tail,
    _redact,
)

SECRET = "supersecretapikey-abc123XYZ"
TOKEN = "bearer-token-9f8e7d6c5b4a"


def test_redacts_secret_values():
    out = _redact(f"key is {SECRET} here", {"api_key": SECRET})
    assert SECRET not in out
    assert REDACTION_MARKER in out
    assert out == f"key is {REDACTION_MARKER} here"


def test_redacts_run_token():
    out = _redact(f"auth {TOKEN}", {}, run_token=TOKEN)
    assert TOKEN not in out
    assert REDACTION_MARKER in out


def test_short_and_empty_values_are_not_redacted():
    # "", "1", "0", "true" are all shorter than MIN_SECRET_LEN; redacting them
    # would corrupt ordinary log text. The log must survive untouched.
    text = "processed 1 item, ok=true, count=0, note="
    out = _redact(text, {"a": "", "b": "1", "c": "0", "d": "true"})
    assert out == text
    assert REDACTION_MARKER not in out


def test_non_string_secret_does_not_crash():
    # ints/bools are coerced to str() so the redactor never blows up. A numeric
    # secret long enough to matter is still masked; a short one is left alone.
    out = _redact("code 12345 flag True", {"num": 12345, "flag": True})
    assert "12345" not in out  # coerced + long enough -> redacted
    assert REDACTION_MARKER in out
    assert "True" in out  # str(True) is 4 chars -> below threshold, untouched


def test_longer_secret_masked_first():
    # A secret that contains a shorter secret as a substring is fully masked.
    inner = "abcde"
    outer = "abcdefghij"
    out = _redact(f"see {outer}", {"outer": outer, "inner": inner})
    assert outer not in out
    assert REDACTION_MARKER in out
    assert out.count(REDACTION_MARKER) == 1  # not double-masked


def test_secret_straddling_truncation_boundary_is_removed():
    # redact-then-tail: a secret sitting across the last-64KB cut must be gone,
    # not left as a leaking partial. Place SECRET so the cut falls mid-secret.
    filler_head = b"H" * (LOG_TAIL_MAX_BYTES - 5)
    filler_tail = b"T" * 100
    data = filler_head + SECRET.encode() + filler_tail
    out = _log_tail(data, {"api_key": SECRET})
    assert SECRET not in out
    assert "abc123XYZ" not in out  # no partial second half survives


def test_log_tail_respects_cap():
    data = b"a" * (200 * 1024)  # 200KB, no secrets
    out = _log_tail(data, {})
    assert len(out.encode("utf-8")) <= LOG_TAIL_MAX_BYTES


def test_log_tail_handles_no_secrets():
    assert _log_tail(b"plain output", None) == "plain output"
    assert _log_tail(b"plain output", {}) == "plain output"
