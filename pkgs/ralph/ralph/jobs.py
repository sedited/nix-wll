"""Durable work queue for pull request reviews."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class JobStore:
    def __init__(self, path, *, max_attempts=3, retry_base_seconds=30,
                 retry_max_seconds=300):
        if max_attempts < 1 or retry_base_seconds < 0 or retry_max_seconds < 0:
            raise ValueError("retry limits must be non-negative")
        self.path = path
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        with self._connection(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    number INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    base_ref TEXT NOT NULL,
                    head TEXT NOT NULL,
                    action TEXT NOT NULL,
                    force INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    review_result TEXT,
                    token TEXT,
                    error TEXT,
                    UNIQUE(number, generation)
                )
            """)
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_pending_per_pr
                    ON jobs(number) WHERE status = 'pending'
            """)
            db.execute("DROP INDEX IF EXISTS jobs_one_running")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running_per_pr
                    ON jobs(number) WHERE status = 'running'
            """)

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        job["force"] = bool(job["force"])
        job["review_result"] = (json.loads(job["review_result"])
                                if job["review_result"] is not None else None)
        return job

    def enqueue(self, number, base_ref, head, action, force=False):
        """Store a webhook event before acknowledging it.

        A pending event for this PR is replaced by the latest event. An event
        with an already queued or finished head is ignored unless forced.
        """
        with self._connection(write=True) as db:
            pending = db.execute(
                "SELECT * FROM jobs WHERE number = ? AND status = 'pending'",
                (number,)).fetchone()
            latest = db.execute(
                "SELECT * FROM jobs WHERE number = ? ORDER BY generation DESC LIMIT 1",
                (number,)).fetchone()
            if pending:
                if pending["head"] == head and pending["base_ref"] == base_ref:
                    if force and not pending["force"]:
                        db.execute("UPDATE jobs SET force = 1 WHERE id = ?",
                                   (pending["id"],))
                        return True
                    return False
                generation = latest["generation"] + 1
                db.execute("""
                    UPDATE jobs SET generation = ?, base_ref = ?, head = ?,
                        action = ?, force = ?, attempts = 0, error = NULL
                    WHERE id = ?
                """, (generation, base_ref, head, action, int(force),
                      pending["id"]))
                return True

            if (latest and latest["head"] == head
                    and latest["base_ref"] == base_ref and not force):
                return False
            if latest and latest["status"] == "retry":
                db.execute("""
                    UPDATE jobs SET status = 'superseded', next_attempt = 0
                    WHERE id = ?
                """, (latest["id"],))
            generation = latest["generation"] + 1 if latest else 1
            db.execute("""
                INSERT INTO jobs (number, generation, base_ref, head, action,
                                  force, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """, (number, generation, base_ref, head, action, int(force)))
            return True

    def recover(self):
        """Release claims left by a previous process. Call once at startup."""
        with self._connection(write=True) as db:
            running = db.execute(
                "SELECT * FROM jobs WHERE status = 'running'").fetchall()
            for job in running:
                newer = db.execute("""
                    SELECT 1 FROM jobs WHERE number = ? AND generation > ?
                        AND status = 'pending'
                """, (job["number"], job["generation"])).fetchone()
                status = ("superseded" if newer else
                          "retry" if job["review_result"] is not None else
                          "pending")
                db.execute("""
                    UPDATE jobs SET status = ?, token = NULL, next_attempt = 0
                    WHERE id = ?
                """, (status, job["id"]))
            return len(running)

    def claim(self):
        """Claim the next due job whose PR has no running job."""
        with self._connection(write=True) as db:
            row = db.execute("""
                SELECT * FROM jobs AS candidate
                WHERE (candidate.status = 'pending'
                    OR (candidate.status = 'retry'
                        AND candidate.next_attempt <= ?))
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs AS running
                        WHERE running.number = candidate.number
                            AND running.status = 'running'
                    )
                ORDER BY candidate.id LIMIT 1
            """, (time.time(),)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("""
                UPDATE jobs SET status = 'running', token = ? WHERE id = ?
            """, (token, row["id"]))
            return self._job(db.execute("SELECT * FROM jobs WHERE id = ?",
                                        (row["id"],)).fetchone())

    def is_current(self, job):
        with self._connection() as db:
            return bool(db.execute("""
                SELECT 1 FROM jobs AS current
                WHERE current.id = ? AND current.token = ?
                    AND current.status = 'running'
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs AS newer
                        WHERE newer.number = current.number
                            AND newer.generation > current.generation
                            AND newer.status = 'pending'
                    )
            """, (job["id"], job["token"])).fetchone())

    def save_result(self, job, result):
        """Commit the review payload before attempting to publish it."""
        payload = json.dumps(result)
        with self._connection(write=True) as db:
            cursor = db.execute("""
                UPDATE jobs SET review_result = ?
                WHERE id = ? AND token = ? AND status = 'running'
            """, (payload, job["id"], job["token"]))
            return cursor.rowcount == 1

    def complete(self, job):
        return self._finish(job, "complete")

    def supersede(self, job):
        return self._finish(job, "superseded")

    def fail(self, job, error):
        return self._finish(job, "failed", error)

    def _finish(self, job, status, error=None):
        with self._connection(write=True) as db:
            cursor = db.execute("""
                UPDATE jobs SET status = ?, token = NULL, error = ?,
                    next_attempt = 0
                WHERE id = ? AND token = ? AND status = 'running'
            """, (status, str(error) if error is not None else None,
                  job["id"], job["token"]))
            return cursor.rowcount == 1

    def retry(self, job, error):
        """Schedule a retry, or record a terminal failure at the attempt cap."""
        with self._connection(write=True) as db:
            row = db.execute("""
                SELECT * FROM jobs WHERE id = ? AND token = ?
                    AND status = 'running'
            """, (job["id"], job["token"])).fetchone()
            if row is None:
                return None
            newer = db.execute("""
                SELECT 1 FROM jobs WHERE number = ? AND generation > ?
                    AND status = 'pending'
            """, (row["number"], row["generation"])).fetchone()
            attempts = row["attempts"] + 1
            if newer:
                status, next_attempt = "superseded", 0
            elif attempts >= self.max_attempts:
                status, next_attempt = "failed", 0
            else:
                status = "retry"
                delay = min(self.retry_base_seconds * 2 ** (attempts - 1),
                            self.retry_max_seconds)
                next_attempt = time.time() + delay
            db.execute("""
                UPDATE jobs SET status = ?, attempts = ?, next_attempt = ?,
                    error = ?, token = NULL WHERE id = ?
            """, (status, attempts, next_attempt, str(error), row["id"]))
            return status
