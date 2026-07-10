"""On-disk runner credentials, persisted across restarts.

Enrollment tokens are one-time: the Catlico API consumes the token on the first
successful ``/register`` and hands back a long-lived machine credential plus a
per-runner push-signing secret. The runner must remember both across restarts,
or every process start (including a ``--reload`` restart) would try to enroll
again with an already-spent token and be rejected.

The state file holds bearer secrets, so it is created ``0600`` and written
atomically (temp file in the same directory, then ``os.replace``) — a crash
mid-write can never leave a half-written file that strands the next start.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerState:
    """Secrets captured at enrollment.

    ``push_signing_secret`` is only ever returned by ``/register`` and is used
    to verify inbound event pushes, so it must be stored alongside the
    credential — otherwise a restarted runner rejects every pushed event.
    """

    credential: str
    push_signing_secret: str = ""

    def is_usable(self) -> bool:
        return bool(self.credential)


def load_state(path: str | Path) -> RunnerState | None:
    """Return the stored credentials, or ``None`` if absent or unusable.

    A missing, corrupt, or partial file is treated as absent: re-enrolling with
    a fresh token is always recoverable, whereas refusing to boot on a bad file
    is not. Never logs the credential value.
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError (malformed/truncated/empty).
        logger.warning("ignoring unreadable runner state at %s; will re-enroll", p)
        return None

    if not isinstance(raw, dict) or not raw.get("credential"):
        logger.warning(
            "ignoring runner state at %s: missing 'credential'; will re-enroll", p
        )
        return None
    return RunnerState(
        credential=str(raw["credential"]),
        push_signing_secret=str(raw.get("push_signing_secret") or ""),
    )


def save_state(path: str | Path, state: RunnerState) -> None:
    """Persist credentials atomically with owner-only (``0600``) permissions.

    The payload is written to a temp file in the same directory, created with
    mode ``0600`` up front (so the secret is never briefly world-readable), then
    ``os.replace``-d over the destination. The destination is never touched
    until the temp file is complete, so a failed write leaves any pre-existing
    file intact and no stray temp file behind.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp")
    payload = {
        "credential": state.credential,
        "push_signing_secret": state.push_signing_secret,
    }
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, p)
