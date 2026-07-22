"""App audit logging: module+usage detection and FastAPI/Express wire-up."""
from __future__ import annotations

from pathlib import Path

from agentit.analyzers.compliance import ComplianceAnalyzer
from agentit.remediation.audit_wire import (
    discover_python_app_entry,
    enrich_audit_files_from_paths,
    enrich_audit_files_from_repo,
    has_audit_usage,
    repo_has_wired_audit,
    tree_has_wired_audit,
    wire_python_app,
)


class TestHasAuditUsage:
    def test_detects_call_and_import(self):
        assert has_audit_usage('audit_log("x", actor="a", resource="r")')
        assert has_audit_usage("from .audit import audit_log\n")
        assert has_audit_usage("import { auditLog } from './audit';")
        assert not has_audit_usage("print('hello')")


class TestComplianceRequiresUsage:
    def test_orphan_root_audit_py_does_not_clear(self, tmp_path: Path):
        (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (tmp_path / "sbom.json").write_text('{"bomFormat":"CycloneDX"}', encoding="utf-8")
        (tmp_path / "audit.py").write_text(
            "def audit_log(action, *, actor, resource, outcome='success'):\n    pass\n",
            encoding="utf-8",
        )
        score = ComplianceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "audit" in cats

    def test_yaml_audit_log_substring_does_not_clear(self, tmp_path: Path):
        (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (tmp_path / "sbom.json").write_text('{"bomFormat":"CycloneDX"}', encoding="utf-8")
        (tmp_path / "policy.yaml").write_text(
            "kind: ConfigMap\ndata:\n  note: audit log reference\n",
            encoding="utf-8",
        )
        score = ComplianceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "audit" in cats

    def test_module_plus_callsite_clears(self, tmp_path: Path):
        (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (tmp_path / "sbom.json").write_text('{"bomFormat":"CycloneDX"}', encoding="utf-8")
        pkg = tmp_path / "apps" / "api" / "src" / "demo"
        pkg.mkdir(parents=True)
        (pkg / "audit.py").write_text(
            "def audit_log(action, *, actor, resource, outcome='success'):\n    pass\n",
            encoding="utf-8",
        )
        (pkg / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "from .audit import audit_log\n"
            "app = FastAPI()\n"
            "audit_log('boot', actor='system', resource='app')\n",
            encoding="utf-8",
        )
        (tmp_path / "policy.yaml").write_text(
            "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata:\n  name: x\n",
            encoding="utf-8",
        )
        score = ComplianceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "audit" not in cats

    def test_root_module_with_callsite_still_does_not_clear(self, tmp_path: Path):
        """Packaged path required — root audit.py + import is still theater."""
        (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (tmp_path / "sbom.json").write_text('{"bomFormat":"CycloneDX"}', encoding="utf-8")
        (tmp_path / "audit.py").write_text(
            "def audit_log(action, *, actor, resource, outcome='success'):\n    pass\n",
            encoding="utf-8",
        )
        (tmp_path / "main.py").write_text(
            "from audit import audit_log\n"
            "audit_log('boot', actor='system', resource='app')\n",
            encoding="utf-8",
        )
        score = ComplianceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "audit" in cats

    def test_agentit_shaped_src_package_clears(self, tmp_path: Path):
        (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (tmp_path / "sbom.json").write_text('{"bomFormat":"CycloneDX"}', encoding="utf-8")
        pkg = tmp_path / "src" / "agentit"
        pkg.mkdir(parents=True)
        (pkg / "audit.py").write_text(
            "def audit_log(*, actor, action, resource, outcome='success'):\n    pass\n",
            encoding="utf-8",
        )
        routes = pkg / "portal" / "routes"
        routes.mkdir(parents=True)
        (routes / "settings.py").write_text(
            "from agentit.audit import audit_log\n"
            "audit_log(actor='u', action='purge', resource='store', outcome='ok')\n",
            encoding="utf-8",
        )
        (tmp_path / "policy.yaml").write_text(
            "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata:\n  name: x\n",
            encoding="utf-8",
        )
        score = ComplianceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "audit" not in cats


class TestRepoHasWiredAudit:
    def test_detects_packaged_module_plus_usage(self, tmp_path: Path):
        pkg = tmp_path / "src" / "demo"
        pkg.mkdir(parents=True)
        (pkg / "audit.py").write_text("def audit_log(*a, **k): pass\n", encoding="utf-8")
        (pkg / "app.py").write_text(
            "from .audit import audit_log\naudit_log('x', actor='a', resource='r')\n",
            encoding="utf-8",
        )
        assert repo_has_wired_audit(tmp_path)

    def test_root_only_is_not_wired(self, tmp_path: Path):
        (tmp_path / "audit.py").write_text("def audit_log(*a, **k): pass\n", encoding="utf-8")
        (tmp_path / "main.py").write_text(
            "from audit import audit_log\naudit_log('x', actor='a', resource='r')\n",
            encoding="utf-8",
        )
        assert not repo_has_wired_audit(tmp_path)

    def test_tree_variant(self):
        tree = ["src/demo/audit.py", "src/demo/app.py", "README.md"]
        blobs = {
            "src/demo/app.py": (
                "from .audit import audit_log\naudit_log('x', actor='a', resource='r')\n"
            ),
        }
        assert tree_has_wired_audit(tree, blobs.get)


class TestWirePythonApp:
    def test_injects_import_and_middleware(self):
        src = (
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI(title='demo')\n"
            "\n"
            "app.add_middleware(CORSMiddleware)\n"
            "\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {}\n"
        )
        out = wire_python_app(src)
        assert "from .audit import audit_log" in out
        assert "agentit_audit_middleware" in out
        assert "audit_log(" in out


class TestEnrichFromRepo:
    def test_relocates_and_wires_fastapi_monorepo(self, tmp_path: Path):
        pkg = tmp_path / "apps" / "api" / "src" / "pinky_api"
        pkg.mkdir(parents=True)
        (pkg / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "app.add_middleware(SecurityHeadersMiddleware)\n",
            encoding="utf-8",
        )
        files = [{
            "path": "patch-audit.py",
            "target_path": "audit.py",
            "content": "def audit_log(*a, **k): pass\n",
            "description": "audit module",
            "skill_name": "app-audit-logging",
        }]
        out = enrich_audit_files_from_repo(tmp_path, files)
        targets = {f["target_path"] for f in out}
        assert "apps/api/src/pinky_api/audit.py" in targets
        assert "apps/api/src/pinky_api/app.py" in targets
        wired = next(f for f in out if f["target_path"].endswith("app.py"))
        assert "from .audit import audit_log" in wired["content"]
        assert discover_python_app_entry(tmp_path) == (
            "apps/api/src/pinky_api",
            "apps/api/src/pinky_api/app.py",
        )


class TestEnrichFromPaths:
    def test_github_tree_variant(self):
        tree = [
            "apps/api/src/demo/app.py",
            "README.md",
        ]
        blobs = {
            "apps/api/src/demo/app.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n"
            ),
        }
        files = [{
            "target_path": "audit.py",
            "content": "def audit_log(*a, **k): pass\n",
            "skill_name": "app-audit-logging",
        }]
        out = enrich_audit_files_from_paths(
            files, tree_paths=tree, read_file=blobs.get,
        )
        assert any(f["target_path"] == "apps/api/src/demo/audit.py" for f in out)
        assert any(f["target_path"] == "apps/api/src/demo/app.py" for f in out)

    def test_drops_orphan_when_already_wired(self):
        tree = [
            "src/agentit/audit.py",
            "src/agentit/portal/routes/settings.py",
        ]
        blobs = {
            "src/agentit/portal/routes/settings.py": (
                "from agentit.audit import audit_log\n"
                "audit_log(actor='u', action='purge', resource='store')\n"
            ),
        }
        files = [{
            "target_path": "audit.py",
            "content": "def audit_log(*a, **k): pass\n",
            "skill_name": "app-audit-logging",
        }]
        out = enrich_audit_files_from_paths(
            files, tree_paths=tree, read_file=blobs.get,
        )
        assert out == []


class TestEnrichDropsWhenRepoWired:
    def test_already_wired_repo_drops_root_stub(self, tmp_path: Path):
        pkg = tmp_path / "src" / "agentit"
        pkg.mkdir(parents=True)
        (pkg / "audit.py").write_text("def audit_log(*a, **k): pass\n", encoding="utf-8")
        (pkg / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "from .audit import audit_log\n"
            "app = FastAPI()\n"
            "audit_log(action='boot', actor='system', resource='app')\n",
            encoding="utf-8",
        )
        files = [{
            "target_path": "audit.py",
            "content": "def audit_log(*a, **k): pass\n",
            "skill_name": "app-audit-logging",
        }]
        out = enrich_audit_files_from_repo(tmp_path, files)
        assert out == []
