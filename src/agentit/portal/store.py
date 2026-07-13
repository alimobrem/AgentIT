from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from agentit.models import AssessmentReport, Severity


class AssessmentStore:
    def __init__(self, db_path: str | None = None) -> None:
        import os
        if db_path is None:
            db_path = os.environ.get("AGENTIT_DB_PATH", "agentit.db")
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
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
                orchestration_json TEXT DEFAULT '{}',
                FOREIGN KEY (assessment_id) REFERENCES assessments(id)
            )
            """
        )
        # Migration: add orchestration_json to existing DBs
        try:
            self._conn.execute(
                "ALTER TABLE onboarding_results ADD COLUMN orchestration_json TEXT DEFAULT '{}'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE onboarding_results ADD COLUMN pr_url TEXT DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "ALTER TABLE apply_results ADD COLUMN repo_files_json TEXT DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass
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
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action ON events(action)")
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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apply_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                applied_json TEXT NOT NULL DEFAULT '[]',
                skipped_json TEXT NOT NULL DEFAULT '[]',
                errors_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (assessment_id) REFERENCES assessments(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_jobs (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                current_step TEXT DEFAULT '',
                steps_completed TEXT DEFAULT '[]',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_operations (
                id TEXT PRIMARY KEY,
                app_name TEXT NOT NULL,
                job_name TEXT NOT NULL,
                agent TEXT NOT NULL,
                schedule TEXT NOT NULL,
                command TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                delivery_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_feedback (
                id TEXT PRIMARY KEY,
                app_name TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                finding_category TEXT NOT NULL,
                action TEXT NOT NULL,
                human_reason TEXT,
                original_value TEXT,
                human_value TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_effectiveness (
                skill_name TEXT NOT NULL,
                app_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (skill_name, app_name, created_at)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suppressed_checks (
                id TEXT PRIMARY KEY,
                app_name TEXT NOT NULL,
                check_source TEXT NOT NULL,
                reason TEXT,
                suppressed_by TEXT DEFAULT 'user',
                created_at TEXT NOT NULL,
                UNIQUE(app_name, check_source)
            )
            """
        )
        self._conn.commit()

    # ── Settings ───────────────────────────────────────────────────────

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def list_settings(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM settings ORDER BY key",
        ).fetchall()
        return [dict(r) for r in rows]

    def save_apply_results(
        self, assessment_id: str, results: dict, namespace: str, dry_run: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO apply_results
               (assessment_id, namespace, dry_run, applied_json, skipped_json, errors_json, repo_files_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assessment_id, namespace, int(dry_run),
                json.dumps(results["applied"]),
                json.dumps(results["skipped"]),
                json.dumps(results["errors"]),
                json.dumps(results.get("repo_files", [])),
                now,
            ),
        )
        if results.get("missing_operators"):
            self._conn.execute(
                "DELETE FROM apply_results WHERE assessment_id = ? AND created_at < ?",
                (assessment_id, now),
            )
        self._conn.commit()

    def get_apply_results(self, assessment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM apply_results WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        repo_files_raw = row["repo_files_json"] if "repo_files_json" in row.keys() else "[]"
        return {
            "namespace": row["namespace"],
            "dry_run": bool(row["dry_run"]),
            "applied": json.loads(row["applied_json"]),
            "skipped": json.loads(row["skipped_json"]),
            "errors": json.loads(row["errors_json"]),
            "repo_files": json.loads(repo_files_raw),
            "created_at": row["created_at"],
        }

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
        self._conn.execute("DELETE FROM remediation_jobs WHERE assessment_id = ?", (assessment_id,))
        self._conn.execute("DELETE FROM onboarding_results WHERE assessment_id = ?", (assessment_id,))
        self._conn.execute("DELETE FROM remediations WHERE assessment_id = ?", (assessment_id,))
        self._conn.execute("DELETE FROM slos WHERE assessment_id = ?", (assessment_id,))
        self._conn.execute("DELETE FROM gates WHERE assessment_id = ?", (assessment_id,))
        self._conn.execute("DELETE FROM apply_results WHERE assessment_id = ?", (assessment_id,))
        cursor = self._conn.execute("DELETE FROM assessments WHERE id = ?", (assessment_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def save_onboarding(
        self, assessment_id: str, files: list[dict], orchestration: dict | None = None,
    ) -> str:
        onboarding_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO onboarding_results (id, assessment_id, created_at, files_json, orchestration_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                onboarding_id,
                assessment_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(files),
                json.dumps(orchestration or {}),
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

    def get_latest_onboarding(self, assessment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM onboarding_results WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_orchestration(self, assessment_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT orchestration_json FROM onboarding_results WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["orchestration_json"])

    def update_pr_url(self, assessment_id: str, pr_url: str) -> None:
        self._conn.execute(
            """
            UPDATE onboarding_results SET pr_url = ?
            WHERE id = (
                SELECT id FROM onboarding_results
                WHERE assessment_id = ? ORDER BY created_at DESC LIMIT 1
            )
            """,
            (pr_url, assessment_id),
        )
        self._conn.commit()

    def list_onboardings(self, assessment_id: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, assessment_id, created_at, files_json, orchestration_json, pr_url
            FROM onboarding_results WHERE assessment_id = ?
            ORDER BY created_at DESC
            """,
            (assessment_id,),
        ).fetchall()
        result = []
        for r in rows:
            files = json.loads(r["files_json"])
            orch = json.loads(r["orchestration_json"]) if r["orchestration_json"] else {}
            categories = list({f["category"] for f in files})
            result.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "file_count": len(files),
                "categories": categories,
                "recommendation": orch.get("recommendation", ""),
                "auto_approve": orch.get("auto_approve", False),
                "pr_url": r["pr_url"] or "",
            })
        return result

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

    def list_events_by_agent(self, agent_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_dlq_messages(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE action = 'dead-letter' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _update_dlq(self, event_id: str, new_action: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE events SET action = ? WHERE id = ? AND action = 'dead-letter'",
            (new_action, event_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def retry_dlq_message(self, event_id: str) -> bool:
        if not self._update_dlq(event_id, 'dlq-retry'):
            return False
        self.log_event(
            'portal', 'dlq-retry', None, 'info',
            f'Retried dead-letter event {event_id}',
        )
        return True

    def dismiss_dlq_message(self, event_id: str) -> bool:
        return self._update_dlq(event_id, 'dlq-dismissed')

    def dismiss_all_dlq(self) -> int:
        cursor = self._conn.execute(
            "UPDATE events SET action = 'dlq-dismissed' WHERE action = 'dead-letter'",
        )
        self._conn.commit()
        return cursor.rowcount

    def has_schedules_for_app(self, app_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM scheduled_operations WHERE app_name = ? LIMIT 1",
            (app_name,),
        ).fetchone()
        return row is not None

    def list_remediations_by_agent(self, agent_name: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM remediations WHERE agent_name = ? ORDER BY created_at DESC",
            (agent_name,),
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
        existing = self._conn.execute(
            "SELECT id FROM gates WHERE assessment_id = ? AND gate_type = ? AND status = 'pending'",
            (assessment_id, gate_type),
        ).fetchone()
        if existing:
            return existing["id"]

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

    def expire_stale_gates(self, hours: int = 24) -> int:
        """Auto-reject pending gates older than the given hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE gates SET status = 'expired', resolved_at = ?, resolved_by = 'auto-expire'
            WHERE status = 'pending' AND created_at < ?
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff),
        )
        self._conn.commit()
        return cursor.rowcount

    def list_gates(self, status: str = "pending") -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM gates WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_gates(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM gates ORDER BY created_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stale_gates(self, hours: int = 24) -> list[dict]:
        """Find pending gates older than the given hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM gates WHERE status = 'pending' AND created_at < ? ORDER BY created_at ASC",
            (cutoff,),
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
        self,
        assessment_id: str,
        agent_name: str,
        description: str,
        status: str = "generated",
        manifest_path: str | None = None,
    ) -> str:
        existing = self._conn.execute(
            """
            SELECT id, status FROM remediations
            WHERE assessment_id = ? AND agent_name = ? AND description = ?
              AND status NOT IN ('completed', 'applied')
            LIMIT 1
            """,
            (assessment_id, agent_name, description),
        ).fetchone()
        if existing:
            if status != "generated" and status != existing["status"]:
                self._conn.execute(
                    "UPDATE remediations SET status = ? WHERE id = ?",
                    (status, existing["id"]),
                )
                self._conn.commit()
            return existing["id"]
        rem_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO remediations (id, assessment_id, agent_name, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rem_id,
                assessment_id,
                agent_name,
                description,
                status,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return rem_id

    def update_remediation_status(self, remediation_id: str, status: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE remediations SET status = ? WHERE id = ? AND status != 'completed'",
            (status, remediation_id),
        )
        if status == "completed":
            self._conn.execute(
                "UPDATE remediations SET completed_at = ? WHERE id = ? AND status = 'completed'",
                (datetime.now(timezone.utc).isoformat(), remediation_id),
            )
        self._conn.commit()
        return cursor.rowcount > 0

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
            WHERE id = ? AND status != 'completed'
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

    # ── Assessment Jobs ──────────────────────────────────────────────────

    def create_assessment_job(self, repo_url: str) -> str:
        """Create a tracking job for an async assessment run."""
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO remediation_jobs
                (id, assessment_id, status, current_step, steps_completed, error, created_at, updated_at)
            VALUES (?, ?, 'assessing', ?, '[]', '', ?, ?)""",
            (job_id, "", repo_url[:200], now, now),
        )
        self._conn.commit()
        return job_id

    def update_assessment_job(
        self, job_id: str, status: str, step: str = "", assessment_id: str = "",
    ) -> None:
        """Update an assessment job's status and optionally link to the final assessment."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE remediation_jobs
            SET status = ?, current_step = ?, assessment_id = ?, updated_at = ?
            WHERE id = ?""",
            (status, step, assessment_id, now, job_id),
        )
        self._conn.commit()

    # ── Remediation Jobs ──────────────────────────────────────────────────

    def create_remediation_job(self, assessment_id: str) -> str:
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO remediation_jobs
                (id, assessment_id, status, current_step, steps_completed, error, created_at, updated_at)
            VALUES (?, ?, 'pending', '', '[]', '', ?, ?)
            """,
            (job_id, assessment_id, now, now),
        )
        self._conn.commit()
        return job_id

    def update_remediation_job(
        self, job_id: str, status: str, current_step: str = "", error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # Append current_step to steps_completed when transitioning
        if current_step:
            row = self._conn.execute(
                "SELECT steps_completed FROM remediation_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            steps = json.loads(row["steps_completed"]) if row else []
            if current_step not in steps:
                steps.append(current_step)
            self._conn.execute(
                """
                UPDATE remediation_jobs
                SET status = ?, current_step = ?, steps_completed = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, current_step, json.dumps(steps), error, now, job_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE remediation_jobs
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, job_id),
            )
        self._conn.commit()

    def get_remediation_job(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM remediation_jobs WHERE id = ?", (job_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["steps_completed"] = json.loads(d["steps_completed"])
        return d

    def list_remediation_jobs(self, assessment_id: str | None = None) -> list[dict]:
        if assessment_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM remediation_jobs WHERE assessment_id = ? ORDER BY created_at DESC",
                (assessment_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM remediation_jobs ORDER BY created_at DESC",
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["steps_completed"] = json.loads(d["steps_completed"])
            result.append(d)
        return result

    # ── Scheduled Operations ─────────────────────────────────────────

    def create_schedule(
        self,
        app_name: str,
        job_name: str,
        agent: str,
        schedule: str,
        command: str,
    ) -> str:
        schedule_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO scheduled_operations
                (id, app_name, job_name, agent, schedule, command, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (schedule_id, app_name, job_name, agent, schedule, command, now, now),
        )
        self._conn.commit()
        return schedule_id

    def list_schedules(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_operations ORDER BY created_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    def update_schedule_cron(self, schedule_id: str, schedule: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE scheduled_operations SET schedule = ?, updated_at = ? WHERE id = ?",
            (schedule, datetime.now(timezone.utc).isoformat(), schedule_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_schedule(self, schedule_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM scheduled_operations WHERE id = ?",
            (schedule_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def toggle_schedule(self, schedule_id: str, enabled: bool) -> bool:
        cursor = self._conn.execute(
            "UPDATE scheduled_operations SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), datetime.now(timezone.utc).isoformat(), schedule_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ── Webhook Deduplication ────────────────────────────────────────────

    def webhook_already_processed(self, delivery_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_webhooks WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return row is not None

    def mark_webhook_processed(self, delivery_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_webhooks (delivery_id, processed_at) VALUES (?, ?)",
            (delivery_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    # ── Agent Feedback ──────────────────────────────────────────────────

    def record_feedback(
        self,
        app_name: str,
        agent_name: str,
        finding_category: str,
        action: str,
        human_reason: str = "",
        original_value: str = "",
        human_value: str = "",
    ) -> str:
        """Record human feedback on an agent recommendation."""
        feedback_id = uuid.uuid4().hex
        self._conn.execute(
            """INSERT INTO agent_feedback (id, app_name, agent_name, finding_category,
               action, human_reason, original_value, human_value, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feedback_id, app_name, agent_name, finding_category, action,
                human_reason, original_value, human_value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return feedback_id

    def get_feedback_for_app(
        self,
        app_name: str,
        agent_name: str = "",
        finding_category: str = "",
    ) -> list[dict]:
        """Get feedback history for an app, optionally filtered by agent/category."""
        query = "SELECT * FROM agent_feedback WHERE app_name = ?"
        params: list[str] = [app_name]
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if finding_category:
            query += " AND finding_category = ?"
            params.append(finding_category)
        query += " ORDER BY created_at DESC"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def get_rejection_count(self, app_name: str, finding_category: str) -> int:
        """How many times has this category been rejected for this app?"""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_feedback WHERE app_name = ? AND finding_category = ? AND action = 'rejected'",
            (app_name, finding_category),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_human_override(self, app_name: str, finding_category: str) -> str | None:
        """Get the most recent human override value for this app/category."""
        row = self._conn.execute(
            """SELECT human_value FROM agent_feedback
               WHERE app_name = ? AND finding_category = ? AND action = 'modified' AND human_value != ''
               ORDER BY created_at DESC LIMIT 1""",
            (app_name, finding_category),
        ).fetchone()
        return row["human_value"] if row else None

    # ── Trust / Transparency ────────────────────────────────────────────

    def get_agent_stats(self, agent_name: str = "") -> list[dict]:
        """Get performance stats per agent: total runs, success rate, avg events."""
        query = """
            SELECT agent_id,
                   COUNT(*) as total_events,
                   SUM(CASE WHEN action LIKE '%complete%' THEN 1 ELSE 0 END) as successes,
                   SUM(CASE WHEN action LIKE '%failed%' OR action LIKE '%error%' THEN 1 ELSE 0 END) as failures,
                   MIN(timestamp) as first_seen,
                   MAX(timestamp) as last_seen
            FROM events
        """
        params: list[str] = []
        if agent_name:
            query += " WHERE agent_id = ?"
            params.append(agent_name)
        query += " GROUP BY agent_id ORDER BY total_events DESC"
        rows = self._conn.execute(query, params).fetchall()
        stats = []
        for r in rows:
            total = r["successes"] + r["failures"]
            success_rate = (r["successes"] / total * 100) if total > 0 else 0
            stats.append({
                "agent": r["agent_id"],
                "total_events": r["total_events"],
                "successes": r["successes"],
                "failures": r["failures"],
                "success_rate": round(success_rate, 1),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            })
        return stats

    def get_assessment_timeline(self, assessment_id: str) -> list[dict]:
        """Get chronological timeline of all events for an assessment."""
        events = self._conn.execute(
            """SELECT timestamp, agent_id, action, target_app, severity, summary
               FROM events
               WHERE details_json LIKE ? OR summary LIKE ?
               ORDER BY timestamp ASC""",
            (f'%{assessment_id}%', f'%{assessment_id[:12]}%'),
        ).fetchall()

        # Also get gates for this assessment
        gates = self._conn.execute(
            "SELECT created_at as timestamp, 'gate' as agent_id, gate_type as action, status as severity, summary FROM gates WHERE assessment_id = ? ORDER BY created_at ASC",
            (assessment_id,),
        ).fetchall()

        # Also get remediations
        remeds = self._conn.execute(
            "SELECT created_at as timestamp, agent_name as agent_id, 'remediation' as action, status as severity, description as summary FROM remediations WHERE assessment_id = ? ORDER BY created_at ASC",
            (assessment_id,),
        ).fetchall()

        # Merge and sort
        timeline = [dict(r) for r in events] + [dict(r) for r in gates] + [dict(r) for r in remeds]
        timeline.sort(key=lambda x: x.get("timestamp", ""))
        return timeline

    def get_fleet_insights(self) -> dict:
        """Get fleet-wide statistics for the insights dashboard."""
        row = self._conn.execute(
            "SELECT COUNT(*) as total_assessments FROM assessments"
        ).fetchone()
        total_assessments = row["total_assessments"] if row else 0

        row = self._conn.execute(
            "SELECT COUNT(DISTINCT repo_url) as unique_apps FROM assessments"
        ).fetchone()
        unique_apps = row["unique_apps"] if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) as total_onboardings FROM onboarding_results"
        ).fetchone()
        total_onboardings = row["total_onboardings"] if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) as total_remediations FROM remediations"
        ).fetchone()
        total_remediations = row["total_remediations"] if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) as pending FROM gates WHERE status = 'pending'"
        ).fetchone()
        pending_gates = row["pending"] if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) as total_events FROM events"
        ).fetchone()
        total_events = row["total_events"] if row else 0

        # Feedback stats
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) as rejections FROM agent_feedback"
        ).fetchone()
        total_feedback = row["total"] if row else 0
        total_rejections = row["rejections"] or 0 if row else 0

        return {
            "total_assessments": total_assessments,
            "unique_apps": unique_apps,
            "total_onboardings": total_onboardings,
            "total_remediations": total_remediations,
            "pending_gates": pending_gates,
            "total_events": total_events,
            "total_feedback": total_feedback,
            "total_rejections": total_rejections,
        }

    def get_score_history(self, repo_url: str, limit: int = 20) -> list[dict]:
        """Get score history for trend visualization."""
        rows = self._conn.execute(
            """SELECT id, assessed_at, overall_score, criticality
               FROM assessments WHERE repo_url = ?
               ORDER BY assessed_at DESC LIMIT ?""",
            (repo_url, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Skill Effectiveness ──────────────────────────────────────────

    def record_skill_outcome(self, skill_name: str, app_name: str, outcome: str, reason: str = '') -> None:
        self._conn.execute(
            'INSERT INTO skill_effectiveness (skill_name, app_name, outcome, reason, created_at) VALUES (?, ?, ?, ?, ?)',
            (skill_name, app_name, outcome, reason, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def get_skill_effectiveness(self, skill_name: str = '', min_count: int = 5) -> dict:
        if skill_name:
            rows = self._conn.execute(
                'SELECT skill_name, outcome, COUNT(*) as cnt FROM skill_effectiveness WHERE skill_name = ? GROUP BY skill_name, outcome',
                (skill_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                'SELECT skill_name, outcome, COUNT(*) as cnt FROM skill_effectiveness GROUP BY skill_name, outcome',
            ).fetchall()
        stats: dict[str, dict] = {}
        for r in rows:
            name = r['skill_name']
            if name not in stats:
                stats[name] = {'approved': 0, 'rejected': 0, 'total': 0}
            stats[name][r['outcome']] = r['cnt']
            stats[name]['total'] += r['cnt']
        return {k: v for k, v in stats.items() if v['total'] >= min_count}

    def get_low_effectiveness_skills(self, min_count: int = 5, max_rate: float = 0.3) -> list[dict]:
        stats = self.get_skill_effectiveness(min_count=min_count)
        low: list[dict] = []
        for name, s in stats.items():
            rate = s['approved'] / s['total'] if s['total'] > 0 else 0
            if rate < max_rate:
                low.append({'skill': name, 'approval_rate': round(rate, 2), 'total': s['total']})
        return low

    def get_recent_skill_activity(self, limit: int = 20) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT skill_name, app_name, outcome, reason, created_at "
            "FROM skill_effectiveness ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # ── Check Suppression ───────────────────────────────────────────

    def suppress_check(self, app_name: str, check_source: str, reason: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO suppressed_checks (id, app_name, check_source, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{app_name}:{check_source}", app_name, check_source, reason,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def unsuppress_check(self, app_name: str, check_source: str) -> None:
        self._conn.execute(
            "DELETE FROM suppressed_checks WHERE app_name = ? AND check_source = ?",
            (app_name, check_source),
        )
        self._conn.commit()

    def get_suppressions(self, app_name: str) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT check_source, reason, suppressed_by, created_at "
            "FROM suppressed_checks WHERE app_name = ? ORDER BY created_at DESC",
            (app_name,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_suppressed_sources(self, app_name: str) -> set[str]:
        cursor = self._conn.execute(
            "SELECT check_source FROM suppressed_checks WHERE app_name = ?",
            (app_name,),
        )
        return {row["check_source"] for row in cursor.fetchall()}

    def export_all(self) -> dict:
        """Export all tables as JSON for disaster recovery."""
        tables = ["assessments", "onboarding_results", "events", "gates",
                  "remediations", "agent_registry", "slos", "apply_results",
                  "settings", "remediation_jobs", "scheduled_operations",
                  "processed_webhooks", "agent_feedback", "skill_effectiveness",
                  "suppressed_checks"]
        result = {}
        for table in tables:
            try:
                cursor = self._conn.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in cursor.description]
                rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
                result[table] = rows
            except Exception:
                result[table] = []
        return result

    def purge_old_data(self, retention_days: int = 30) -> dict[str, int]:
        """Delete data older than retention_days. Returns count of deleted rows per table."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        counts: dict[str, int] = {}

        for table, col in [
            ("events", "timestamp"),
            ("remediation_jobs", "created_at"),
            ("apply_results", "created_at"),
        ]:
            cursor = self._conn.execute(
                f"DELETE FROM {table} WHERE {col} < ?", (cutoff,),
            )
            counts[table] = cursor.rowcount

        cursor = self._conn.execute(
            "DELETE FROM onboarding_results WHERE created_at < ? AND id NOT IN "
            "(SELECT id FROM onboarding_results GROUP BY assessment_id "
            "HAVING created_at = MAX(created_at))",
            (cutoff,),
        )
        counts["onboarding_results"] = cursor.rowcount

        cursor = self._conn.execute(
            "DELETE FROM remediations WHERE status = 'completed' AND completed_at < ?",
            (cutoff,),
        )
        counts["remediations"] = cursor.rowcount

        cursor = self._conn.execute(
            "DELETE FROM gates WHERE status IN ('approved', 'rejected', 'expired', 'cancelled') "
            "AND resolved_at < ?",
            (cutoff,),
        )
        counts["gates"] = cursor.rowcount

        webhook_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM processed_webhooks WHERE processed_at < ?",
            (webhook_cutoff,),
        )
        counts["processed_webhooks"] = cursor.rowcount

        self._conn.commit()
        total = sum(counts.values())
        if total > 0:
            self.log_event(
                "store", "data-purged", None, "info",
                f"Purged {total} stale rows (retention={retention_days}d): "
                + ", ".join(f"{t}={c}" for t, c in counts.items() if c > 0),
            )
        return counts
