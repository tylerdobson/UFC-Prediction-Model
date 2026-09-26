"""Durable, credential-free status for local operator commands."""

from __future__ import annotations

import sqlite3

from .pipeline import utc_now, utc_string


TRACKED_COMMANDS = frozenset({
    "import-csv", "import-profile-observations", "import-fight-stat-observations",
    "import-ufcstats", "import-wikipedia-history", "import-wikipedia-years",
    "import-wikipedia-embedded", "import-odds",
    "import-historical-odds", "import-sportradar", "alert-event",
    "paper-trade", "settle-paper",
})


def start_job(connection: sqlite3.Connection, command: str, event_id: str | None = None) -> int:
    if command not in TRACKED_COMMANDS:
        raise ValueError(f"Command is not tracked: {command}")
    cursor = connection.execute(
        """INSERT INTO operator_job_runs(command, event_id, started_at_utc, status)
           VALUES (?, ?, ?, 'started')""",
        (command, event_id, utc_string(utc_now())),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_job(
    connection: sqlite3.Connection,
    job_run_id: int,
    *,
    error: BaseException | None = None,
) -> None:
    # Do not persist exception text: provider SDKs can include URLs or tokens.
    category = None if error is None else (
        "validation_error" if isinstance(error, ValueError)
        else "source_or_runtime_error" if isinstance(error, (RuntimeError, OSError))
        else "database_error" if isinstance(error, sqlite3.Error)
        else "unexpected_error"
    )
    status = "succeeded" if error is None else "failed"
    cursor = connection.execute(
        """UPDATE operator_job_runs
           SET status = ?, finished_at_utc = ?, error_category = ?
           WHERE job_run_id = ? AND status = 'started'""",
        (status, utc_string(utc_now()), category, job_run_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Job run is missing or already finished: {job_run_id}")
    connection.commit()
