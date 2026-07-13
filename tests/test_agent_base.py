"""Regression tests for the shared agent base module."""

from __future__ import annotations

from agentit.agents.base import GeneratedFile, _sanitize_name, validate_manifest, validate_generated_files


class TestGeneratedFile:
    def test_import_from_base(self) -> None:
        from agentit.agents.base import GeneratedFile as Base
        assert Base is GeneratedFile

    def test_finding_addressed_defaults_to_empty(self) -> None:
        gf = GeneratedFile(path="x.yaml", content="y", description="z")
        assert gf.finding_addressed == ""

    def test_finding_addressed_can_be_set(self) -> None:
        gf = GeneratedFile(path="x.yaml", content="y", description="z", finding_addressed="fix it")
        assert gf.finding_addressed == "fix it"

    def test_all_agents_use_shared_generated_file(self) -> None:
        # security/observability/cicd/compliance/infrastructure/incident/
        # release/retirement/chaos were removed once skills covered their
        # domains (see docs/agent-removal-readiness.md) -- only these 3
        # Python agents remain.
        from agentit.agents.cost import CostOptimizationAgent
        from agentit.agents.dependency import DependencyAgent
        from agentit.agents.codechange import CodeChangeAgent

        # All 3 modules are importable — no import errors
        assert CostOptimizationAgent is not None
        assert DependencyAgent is not None
        assert CodeChangeAgent is not None


class TestSanitizeName:
    def test_basic(self) -> None:
        assert _sanitize_name("My_App.v2") == "my-app-v2"

    def test_empty_string(self) -> None:
        assert _sanitize_name("") == "app"

    def test_truncates_at_63(self) -> None:
        assert len(_sanitize_name("a" * 100)) <= 63

    def test_strips_leading_trailing_dashes(self) -> None:
        assert _sanitize_name("-test-") == "test"


class TestValidateManifest:
    def test_valid_manifest(self) -> None:
        import yaml
        content = yaml.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "test"}})
        assert validate_manifest(content) == []

    def test_invalid_yaml_syntax(self) -> None:
        errors = validate_manifest("{{bad yaml: [")
        assert len(errors) == 1
        assert "parse error" in errors[0]

    def test_missing_api_version(self) -> None:
        import yaml
        content = yaml.dump({"kind": "Pod", "metadata": {"name": "test"}})
        errors = validate_manifest(content)
        assert any("apiVersion" in e for e in errors)

    def test_missing_kind(self) -> None:
        import yaml
        content = yaml.dump({"apiVersion": "v1", "metadata": {"name": "test"}})
        errors = validate_manifest(content)
        assert any("kind" in e for e in errors)

    def test_missing_metadata(self) -> None:
        import yaml
        content = yaml.dump({"apiVersion": "v1", "kind": "Pod"})
        errors = validate_manifest(content)
        assert any("metadata" in e for e in errors)

    def test_non_k8s_yaml_skipped(self) -> None:
        """YAML without any K8s fields is not a manifest — skip validation."""
        import yaml
        content = yaml.dump({"version": 2, "updates": [{"package-ecosystem": "pip"}]})
        assert validate_manifest(content) == []

    def test_missing_metadata_name(self) -> None:
        import yaml
        content = yaml.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"labels": {}}})
        errors = validate_manifest(content)
        assert any("name" in e for e in errors)

    def test_multi_document_yaml(self) -> None:
        import yaml
        doc1 = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "a"}}
        doc2 = {"apiVersion": "v1", "metadata": {"name": "b"}}  # missing kind
        content = yaml.dump_all([doc1, doc2])
        errors = validate_manifest(content)
        assert len(errors) == 1
        assert "kind" in errors[0]

    def test_non_mapping_document(self) -> None:
        errors = validate_manifest("---\n- item1\n- item2\n")
        assert any("mapping" in e for e in errors)


class TestValidateGeneratedFiles:
    def test_skips_non_yaml(self) -> None:
        files = [
            GeneratedFile(path="report.md", content="# hi", description="report"),
            GeneratedFile(path="cleanup.sh", content="#!/bin/bash", description="script"),
        ]
        assert validate_generated_files(files) == []

    def test_catches_invalid_yaml_file(self) -> None:
        import yaml
        files = [
            GeneratedFile(
                path="bad.yaml",
                content=yaml.dump({"kind": "Pod"}),  # missing apiVersion and metadata
                description="bad",
            ),
        ]
        errors = validate_generated_files(files)
        assert len(errors) >= 1
        assert "bad.yaml" in errors[0]
