from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from agentit.models import AssessmentReport, Severity


class AssessmentStore:
    def __init__(self, db_path: str | None = None) -> None:
        import os
        if db_path is None:
            db_path = os.environ.get("AGENTIT_DB_PATH", "agentit.db")
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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_app TEXT,
                severity TEXT DEFAULT 'info',
                summary TEXT NOT NULL,
                details_json TEXT DEFAULT '{}'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gates (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                gate_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                FOREIGN KEY (assessment_id) REFERENCES assessments(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediations (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (assessment_id) REFERENCES assessments(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_registry (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                capabilities TEXT DEFAULT '[]',
                last_heartbeat TEXT,
                registered_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slos (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                target_value REAL NOT NULL,
                current_value REAL,
                status TEXT DEFAULT 'unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT,
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
        self.log_event(
            "assessor",
            "assessment-complete",
            report.repo_name,
            "info",
            f"Assessment complete: {report.overall_score:.0f}/100",
        )
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
        # Resolve repo_name from the linked assessment for the event
        row = self._conn.execute(
            "SELECT repo_name FROM assessments WHERE id = ?",
            (assessment_id,),
        ).fetchone()
        target_app = row["repo_name"] if row else assessment_id
        self.log_event(
            "onboarding",
            "onboarding-complete",
            target_app,
            "info",
            f"Generated {len(files)} manifests",
        )
        return onboarding_id

    def get_onboarding(self, assessment_id: str) -> list[dict] | None:
        row = self._conn.execute(
            "SELECT files_json FROM onboarding_results WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["files_json"])

    # ── Events ──────────────────────────────────────────────────────────

    def log_event(
        self,
        agent_id: str,
        action: str,
        target_app: str | None,
        severity: str,
        summary: str,
        details: dict | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO events (id, timestamp, agent_id, action, target_app, severity, summary, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                action,
                target_app,
                severity,
                summary,
                json.dumps(details or {}),
            ),
        )
        self._conn.commit()
        return event_id

    def list_events(
        self, limit: int = 50, target_app: str | None = None
    ) -> list[dict]:
        if target_app is not None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE target_app = ? ORDER BY timestamp DESC LIMIT ?",
                (target_app, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Assessment history / trends ─────────────────────────────────────

    def list_history(self, repo_url: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, repo_name, repo_url, assessed_at, overall_score, criticality
            FROM assessments WHERE repo_url = ? ORDER BY assessed_at ASC
            """,
            (repo_url,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trend(self, repo_url: str) -> dict:
        history = self.list_history(repo_url)
        if not history:
            return {
                "current_score": None,
                "previous_score": None,
                "delta": None,
                "assessments_count": 0,
            }
        current = history[-1]["overall_score"]
        previous = history[-2]["overall_score"] if len(history) >= 2 else None
        delta = round(current - previous, 2) if previous is not None else None
        return {
            "current_score": current,
            "previous_score": previous,
            "delta": delta,
            "assessments_count": len(history),
        }

    # ── Fleet ──────────────────────────────────────────────────────────

    def get_fleet_data(self) -> list[dict]:
        """Return one row per unique repo_url with latest assessment + trend."""
        rows = self._conn.execute(
            """
            SELECT a.id, a.repo_url, a.repo_name, a.assessed_at,
                   a.overall_score, a.criticality, a.report_json
            FROM assessments a
            INNER JOIN (
                SELECT repo_url, MAX(assessed_at) AS max_at
                FROM assessments GROUP BY repo_url
            ) latest ON a.repo_url = latest.repo_url
                    AND a.assessed_at = latest.max_at
            ORDER BY a.overall_score ASC
            """
        ).fetchall()

        fleet: list[dict] = []
        for r in rows:
            report = AssessmentReport.model_validate_json(r["report_json"])
            critical_count = sum(
                1 for s in report.scores for f in s.findings
                if f.severity in (Severity.critical, Severity.high)
            )
            trend = self.get_trend(r["repo_url"])
            fleet.append({
                "id": r["id"],
                "repo_url": r["repo_url"],
                "repo_name": r["repo_name"],
                "latest_score": r["overall_score"],
                "previous_score": trend["previous_score"],
                "delta": trend["delta"],
                "criticality": r["criticality"],
                "last_assessed": r["assessed_at"],
                "assessment_count": trend["assessments_count"],
                "critical_count": critical_count,
            })
        return fleet

    # ── Gates ────────────────────────────────────────────────────────────

    def create_gate(self, assessment_id: str, gate_type: str, summary: str) -> str:
        gate_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO gates (id, assessment_id, gate_type, status, summary, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (
                gate_id,
                assessment_id,
                gate_type,
                summary,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return gate_id

    def list_gates(self, status: str = "pending") -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM gates WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_gate(self, gate_id: str, status: str, resolved_by: str) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE gates SET status = ?, resolved_at = ?, resolved_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                status,
                datetime.now(timezone.utc).isoformat(),
                resolved_by,
                gate_id,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ── Remediations ───────────────────────────────────────────────────

    def save_remediation(
        self, assessment_id: str, agent_name: str, description: str
    ) -> str:
        rem_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO remediations (id, assessment_id, agent_name, description, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                rem_id,
                assessment_id,
                agent_name,
                description,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return rem_id

    def list_remediations(self, assessment_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM remediations WHERE assessment_id = ? ORDER BY created_at DESC",
            (assessment_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def complete_remediation(self, remediation_id: str) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE remediations SET status = 'completed', completed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (datetime.now(timezone.utc).isoformat(), remediation_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ── Agent Registry ─────────────────────────────────────────────────

    def register_agent(
        self, agent_name: str, category: str, capabilities: str = "[]"
    ) -> str:
        agent_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO agent_registry
                (id, agent_name, category, status, capabilities, last_heartbeat, registered_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (agent_id, agent_name, category, capabilities, now, now),
        )
        self._conn.commit()
        return agent_id

    def list_agents(self, status: str = "active") -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_registry WHERE status = ? ORDER BY agent_name",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def agent_heartbeat(self, agent_name: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE agent_registry SET last_heartbeat = ? WHERE agent_name = ?",
            (datetime.now(timezone.utc).isoformat(), agent_name),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ── SLOs ───────────────────────────────────────────────────────────

    def save_slo(
        self, assessment_id: str, metric_name: str, target_value: float
    ) -> str:
        slo_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO slos (id, assessment_id, metric_name, target_value, status, created_at)
            VALUES (?, ?, ?, ?, 'unknown', ?)
            """,
            (
                slo_id,
                assessment_id,
                metric_name,
                target_value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return slo_id

    def list_slos(self, assessment_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM slos WHERE assessment_id = ? ORDER BY metric_name",
            (assessment_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_slo(
        self, slo_id: str, current_value: float, status: str
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE slos SET current_value = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (current_value, status, datetime.now(timezone.utc).isoformat(), slo_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0
