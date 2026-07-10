from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from agentit.models import AssessmentReport


class AssessmentStore:
    def __init__(self, db_path: str = "agentit.db") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                repo_url TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                assessed_at TEXT NOT NULL,
                criticality TEXT NOT NULL,
                overall_score REAL NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_results (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                files_json TEXT NOT NULL,
                FOREIGN KEY (assessment_id) REFERENCES assessments(id)
            )
            """
        )
        self._conn.commit()

    def save(self, report: AssessmentReport) -> str:
        assessment_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO assessments (id, repo_url, repo_name, assessed_at, criticality, overall_score, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment_id,
                report.repo_url,
                report.repo_name,
                report.assessed_at.isoformat(),
                report.criticality,
                report.overall_score,
                report.model_dump_json(),
            ),
        )
        self._conn.commit()
        return assessment_id

    def get(self, assessment_id: str) -> AssessmentReport | None:
        row = self._conn.execute(
            "SELECT report_json FROM assessments WHERE id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return AssessmentReport.model_validate_json(row["report_json"])

    def list_all(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, repo_name, repo_url, assessed_at, overall_score, criticality
            FROM assessments ORDER BY assessed_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, assessment_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM assessments WHERE id = ?",
            (assessment_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def save_onboarding(self, assessment_id: str, files: list[dict]) -> str:
        onboarding_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO onboarding_results (id, assessment_id, created_at, files_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                onboarding_id,
                assessment_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(files),
            ),
        )
        self._conn.commit()
        return onboarding_id

    def get_onboarding(self, assessment_id: str) -> list[dict] | None:
        row = self._conn.execute(
            "SELECT files_json FROM onboarding_results WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["files_json"])
