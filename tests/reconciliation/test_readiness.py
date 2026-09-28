"""Tests for the offline reconciliation deployment-readiness checker.

Every test runs offline. The checks under test must report a truthful status
for a healthy tree, for a damaged tree and for a host that lacks a tool, and
must never emit a credential value or claim a cloud result.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from scripts import check_reconciliation_readiness as readiness

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checker() -> readiness.ReadinessChecker:
    return readiness.ReadinessChecker(ROOT, environ={})


# --------------------------------------------------------------------------- #
# aggregation contract
# --------------------------------------------------------------------------- #


def test_a_blocked_finding_blocks_only_its_own_target() -> None:
    findings = [
        readiness.Finding("a", "toolchain", readiness.LOCAL, readiness.PASSED, "ok"),
        readiness.Finding(
            "b",
            "cloud",
            readiness.CLOUD,
            readiness.BLOCKED,
            "no account",
            external_input="An account.",
        ),
        readiness.Finding("c", "verification", readiness.VERIFICATION, readiness.PASSED, "ok"),
    ]

    report = readiness.build_report(findings, ROOT)

    assert report["gates"][readiness.LOCAL]["ready"] is True
    assert report["gates"][readiness.VERIFICATION]["ready"] is True
    assert report["gates"][readiness.CLOUD]["ready"] is False
    assert report["gates"][readiness.CLOUD]["blocking_checks"] == ["b"]
    assert report["external_inputs_required"] == ["An account."]
    assert readiness.exit_code(report) == 0


def test_a_failed_local_check_fails_the_run_even_when_cloud_is_only_not_run() -> None:
    findings = [
        readiness.Finding("a", "database", readiness.LOCAL, readiness.FAILED, "defect"),
        readiness.Finding("b", "verification", readiness.VERIFICATION, readiness.PASSED, "ok"),
        readiness.Finding("c", "cloud", readiness.CLOUD, readiness.NOT_RUN, "not attempted"),
    ]

    report = readiness.build_report(findings, ROOT)

    assert readiness.exit_code(report) == 1
    assert report["gates"][readiness.LOCAL]["ready"] is False
    assert report["gates"][readiness.CLOUD]["ready"] is False  # not_run is not evidence
    assert report["summary"]["failed"] == 1


def test_a_not_run_cloud_target_never_reports_a_cloud_deployment_success() -> None:
    report = readiness.build_report(
        [
            readiness.Finding("local", "database", readiness.LOCAL, readiness.PASSED, "ok"),
            readiness.Finding(
                "gate", "verification", readiness.VERIFICATION, readiness.PASSED, "ok"
            ),
        ],
        ROOT,
    )

    assert report["product_scope"].startswith("local API")
    assert report["tool_authority"] == {
        "deploys": False,
        "calls_aws": False,
        "calls_models": False,
        "network_egress": False,
        "installs_dependencies": False,
        "reads_real_env_files": False,
        "prints_credentials": False,
    }
    # An absent cloud finding must not be rendered as a passed cloud gate.
    assert report["gates"][readiness.CLOUD]["checks"] == 0
    assert report["gates"][readiness.CLOUD]["ready"] is False


def test_a_target_whose_checks_only_declined_to_run_is_never_ready() -> None:
    report = readiness.build_report(
        [
            readiness.Finding("a", "cloud", readiness.CLOUD, readiness.NOT_RUN, "not attempted"),
            readiness.Finding("b", "cloud", readiness.CLOUD, readiness.NOT_RUN, "not attempted"),
        ],
        ROOT,
    )

    gate = report["gates"][readiness.CLOUD]
    assert gate["ready"] is False
    assert gate["blocking_checks"] == []
    assert gate["unattempted_checks"] == ["a", "b"]


def test_a_mixed_target_is_not_ready_while_any_check_declined_to_run() -> None:
    report = readiness.build_report(
        [
            readiness.Finding("a", "database", readiness.LOCAL, readiness.PASSED, "ok"),
            readiness.Finding("b", "database", readiness.LOCAL, readiness.NOT_RUN, "skipped"),
        ],
        ROOT,
    )

    assert report["gates"][readiness.LOCAL]["ready"] is False
    assert readiness.exit_code(report) == 1


def test_a_checker_defect_becomes_a_failed_finding_instead_of_a_crash(
    checker: readiness.ReadinessChecker,
) -> None:
    def broken() -> readiness.Finding:
        raise RuntimeError("synthetic defect")

    checker.guarded("broken.check", "database", readiness.LOCAL, broken)

    assert len(checker.findings) == 1
    finding = checker.findings[0].to_dict()
    assert finding["status"] == readiness.FAILED
    assert finding["blocking"] is True
    assert finding["detail"] == {"error": "RuntimeError"}
    assert "synthetic defect" not in json.dumps(finding, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# credential handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["DART_API_KEY", "SESSION_SECRET_ARN", "KMS_KEY_ARN", "AWS_SECRET_ACCESS_KEY", "app_password"],
)
def test_credential_named_values_are_never_reproduced(name: str) -> None:
    assert readiness.redact(name, "s3cr3t-value") == "set"
    assert readiness.redact(name, "") == "empty"


def test_plain_configuration_values_survive_redaction() -> None:
    assert readiness.redact("APP_ORIGIN", "http://localhost:5173") == "http://localhost:5173"


def test_a_populated_credential_in_the_example_file_is_reported_without_its_value(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.example").write_text(
        "APP_ENV=local\nDART_API_KEY=leaked-real-key\nKMS_KEY_ARN=\n", encoding="utf-8"
    )

    finding = readiness.ReadinessChecker(root, environ={}).check_example_secrets().to_dict()

    assert finding["status"] == readiness.FAILED
    assert finding["detail"]["populated"] == ["DART_API_KEY"]
    assert "leaked-real-key" not in json.dumps(finding, ensure_ascii=False)


def test_no_credential_named_process_value_reaches_the_report(tmp_path: Path) -> None:
    """The host environment may hold real credentials; none may reach the report."""
    output = tmp_path / "run"
    readiness.main(["--output", str(output), "--root", str(ROOT)])
    report = (output / "readiness.json").read_text(encoding="utf-8")

    leaked = [
        name
        for name, value in os.environ.items()
        if readiness.SECRET_NAME.search(name)
        and len(value.strip()) >= 8
        and value.strip() in report
    ]

    assert leaked == [], f"credential-named values reached the report: {leaked}"


def test_real_environment_files_are_detected_but_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("DART_API_KEY=must-not-be-read\n", encoding="utf-8")
    opened: list[str] = []
    original = Path.read_text

    def tracking_read_text(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    finding = readiness.ReadinessChecker(root, environ={}).check_unread_env_files().to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["files"][".env"] == {"present": True, "read_by_this_tool": False}
    assert ".env" not in opened


# --------------------------------------------------------------------------- #
# configuration parsing
# --------------------------------------------------------------------------- #


def test_the_environment_specification_and_example_are_parsed_from_the_real_files() -> None:
    documented = readiness.parse_env_doc(ROOT / readiness.ENV_DOC)
    example = readiness.parse_env_file(ROOT / readiness.ENV_EXAMPLE)

    assert documented["APP_ENV"].default == "local"
    assert documented["APP_ENV"].requirement == "yes"
    assert documented["S3_ARTIFACT_BUCKET"].default == ""  # the doc's "빈 값" means empty
    assert documented["S3_ARTIFACT_BUCKET"].requirement == "cloud"
    assert example["APP_ENV"] == "local"
    assert example["MODEL_ADAPTER"] == "synthetic"
    assert example["S3_ARTIFACT_BUCKET"] == ""


def test_a_remote_origin_in_the_example_is_reported_as_not_the_local_profile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.example").write_text(
        "APP_ENV=local\nMODEL_ADAPTER=synthetic\nAPP_ORIGIN=https://app.example.com\n",
        encoding="utf-8",
    )

    finding = readiness.ReadinessChecker(root, environ={}).check_local_profile().to_dict()

    assert finding["status"] == readiness.FAILED
    assert finding["detail"]["remote"] == ["APP_ORIGIN"]


def test_a_production_example_profile_is_reported_as_wrong(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.example").write_text(
        "APP_ENV=production\nMODEL_ADAPTER=bedrock\nAPP_ORIGIN=http://localhost:5173\n",
        encoding="utf-8",
    )

    finding = readiness.ReadinessChecker(root, environ={}).check_local_profile().to_dict()

    assert finding["status"] == readiness.FAILED
    assert finding["detail"]["unexpected"] == {"APP_ENV": "production", "MODEL_ADAPTER": "bedrock"}


def test_partially_supplied_cloud_variables_still_block_the_cloud_target(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.example").write_text("S3_ARTIFACT_BUCKET=\nDDB_CORE_TABLE=\n", encoding="utf-8")
    documented = {
        "S3_ARTIFACT_BUCKET": readiness.DocumentedVariable("S3_ARTIFACT_BUCKET", "", "cloud", "x"),
        "DDB_CORE_TABLE": readiness.DocumentedVariable("DDB_CORE_TABLE", "", "cloud", "x"),
    }

    checker = readiness.ReadinessChecker(root, environ={"S3_ARTIFACT_BUCKET": "a-real-bucket"})
    finding = checker.check_cloud_variables(documented).to_dict()

    assert finding["status"] == readiness.BLOCKED
    assert finding["detail"]["supplied"] == ["S3_ARTIFACT_BUCKET"]
    assert finding["detail"]["absent"] == ["DDB_CORE_TABLE"]
    assert "DDB_CORE_TABLE" in (finding["external_input"] or "")


def test_the_repository_environment_contract_holds(checker: readiness.ReadinessChecker) -> None:
    documented = readiness.parse_env_doc(ROOT / readiness.ENV_DOC)

    finding = checker.check_env_contract(documented).to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["missing_from_example"] == []
    assert finding["detail"]["default_mismatch"] == []


# --------------------------------------------------------------------------- #
# toolchain probes
# --------------------------------------------------------------------------- #


def test_a_missing_tool_is_blocked_and_names_the_external_input(
    checker: readiness.ReadinessChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness.shutil, "which", lambda name: None)

    finding = checker.check_pinned_tool(
        "pnpm", ["pnpm", "--version"], {"pnpm": "10.0.0"}, readiness.LOCAL
    ).to_dict()

    assert finding["status"] == readiness.BLOCKED
    assert finding["detail"]["error"] == "not_on_path"
    assert "10.0.0" in (finding["external_input"] or "")


def test_a_major_version_mismatch_is_blocked_rather_than_assumed_compatible(
    checker: readiness.ReadinessChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        readiness,
        "probe_version",
        lambda command, timeout=20.0: {"resolved": True, "error": None, "output": "v25.4.0"},
    )

    finding = checker.check_pinned_tool(
        "node", ["node", "--version"], {"node": "22"}, readiness.LOCAL
    ).to_dict()

    assert finding["status"] == readiness.BLOCKED
    assert finding["detail"]["detected_version"] == "25.4.0"
    assert finding["detail"]["ci_pin"] == "22"


def test_a_matching_major_version_passes(
    checker: readiness.ReadinessChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        readiness,
        "probe_version",
        lambda command, timeout=20.0: {"resolved": True, "error": None, "output": "v22.14.0"},
    )

    finding = checker.check_pinned_tool(
        "node", ["node", "--version"], {"node": "22"}, readiness.LOCAL
    ).to_dict()

    assert finding["status"] == readiness.PASSED


def test_toolchain_pins_come_from_the_committed_workflow_not_from_constants() -> None:
    pins = readiness.parse_ci_pins(ROOT / readiness.CI_WORKFLOW)

    assert pins["python"] == "3.12"
    assert pins["java"] == "21"
    assert pins["node"] == "22"
    assert pins["pnpm"] == "10.0.0"


def test_java_is_resolved_from_the_variable_the_parser_tests_actually_use(tmp_path: Path) -> None:
    fake = tmp_path / "java"
    fake.write_text("", encoding="utf-8")

    assert readiness.resolve_java({"PROOFOPS_TEST_JAVA": str(fake)}) == ("PROOFOPS_TEST_JAVA", fake)
    assert readiness.resolve_java({"PROOFOPS_TEST_JAVA": str(tmp_path / "absent")}) == (
        "PROOFOPS_TEST_JAVA",
        None,
    )


def test_an_unsupported_platform_is_blocked_with_the_scope_reason(
    checker: readiness.ReadinessChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness.sys, "platform", "darwin")
    monkeypatch.setattr(readiness.platform, "machine", lambda: "x86_64")

    finding = checker.check_platform().to_dict()

    assert finding["status"] == readiness.BLOCKED
    assert "Intel" in finding["summary"]


def test_apple_silicon_is_inside_the_supported_set(
    checker: readiness.ReadinessChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness.sys, "platform", "darwin")
    monkeypatch.setattr(readiness.platform, "machine", lambda: "arm64")

    assert checker.check_platform().status == readiness.PASSED


# --------------------------------------------------------------------------- #
# repository and contract checks against the real tree
# --------------------------------------------------------------------------- #


def test_a_missing_product_file_is_reported_as_a_defect(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    finding = readiness.ReadinessChecker(root, environ={}).check_required_paths().to_dict()

    assert finding["status"] == readiness.FAILED
    assert len(finding["detail"]["missing"]) == len(readiness.REQUIRED_PATHS)


def test_the_served_routes_match_the_published_contract_fragment(
    checker: readiness.ReadinessChecker,
) -> None:
    finding = checker.check_route_parity().to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["undocumented"] == []
    assert finding["detail"]["unimplemented"] == []
    assert len(finding["detail"]["served"]) == 7


def test_every_published_example_validates_against_the_strict_output_schema(
    checker: readiness.ReadinessChecker,
) -> None:
    finding = checker.check_contract_examples().to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["examples"] == 8
    assert finding["detail"]["invalid"] == []


def test_the_rollback_surface_is_one_router_mount(checker: readiness.ReadinessChecker) -> None:
    finding = checker.check_rollback_surface().to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["api_router_mounts"] == 1
    assert finding["detail"]["composition_store_references"] >= 1


def test_the_product_refuses_staging_and_production_so_no_cloud_claim_is_possible(
    checker: readiness.ReadinessChecker,
) -> None:
    finding = checker.check_non_local_refused().to_dict()

    assert finding["status"] == readiness.BLOCKED
    assert finding["target"] == readiness.CLOUD
    assert finding["detail"]["composition"] == {
        "local": "accepted",
        "staging": "refused",
        "production": "refused",
    }


def test_the_release_verifier_gate_inputs_all_exist(checker: readiness.ReadinessChecker) -> None:
    finding = checker.check_release_verifier_inputs().to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["missing_paths"] == []
    assert finding["detail"]["synthetic_cases"] == 8
    assert finding["detail"]["verifier_executed_here"] is False


# --------------------------------------------------------------------------- #
# database, backup and restore rehearsal
# --------------------------------------------------------------------------- #


def test_the_rehearsal_creates_backs_up_and_restores_a_real_database(tmp_path: Path) -> None:
    checker = readiness.ReadinessChecker(ROOT, environ={})

    checker.rehearse_database(tmp_path)

    by_check = {finding.check: finding for finding in checker.findings}
    assert by_check["database.schema_initializes"].status == readiness.PASSED
    assert by_check["database.additive_only"].status == readiness.PASSED
    assert by_check["database.immutability"].status == readiness.PASSED
    assert by_check["database.unknown_version_rejected"].status == readiness.PASSED
    assert by_check["backup.online_copy"].status == readiness.PASSED
    assert by_check["restore.rehearsal"].status == readiness.PASSED

    populated = by_check["backup.online_copy"].detail["row_counts"]
    assert populated["reconciliation_case"]["source"] >= 1
    assert populated["reconciliation_source"]["source"] >= 2  # the fixture imports two sources
    assert populated["reconciliation_revision"]["source"] >= 1

    additive = by_check["database.additive_only"].detail
    assert additive["removed"] == [] and additive["changed_existing"] == []
    assert all(name.startswith("reconciliation_") for name in additive["added"])

    restored = by_check["restore.rehearsal"].detail
    assert restored["read_through"] == ["get_case", "revision", "source_content"]
    assert restored["restored_revision"] == 1
    assert len(restored["restored_revision_sha256"]) == 64
    assert restored["restored_sources"] == ["fs-scope", "sr-scope"]
    assert restored["product_reads_differing_from_live"] == []
    assert restored["tables_differing_from_live"] == []
    assert restored["artifact_paths_differing_from_live"] == []
    assert restored["source_bytes_match"] is True


def test_a_tampered_backup_is_reported_instead_of_being_called_a_restore(tmp_path: Path) -> None:
    checker = readiness.ReadinessChecker(ROOT, environ={})
    case_id = checker.rehearse_database(tmp_path)
    stored = sorted((tmp_path / "backup" / "artifacts").rglob("*.txt"))
    assert stored, "the fixture import must have written managed source copies"

    corrupted = tmp_path / "corrupted"
    shutil.copytree(tmp_path / "live", corrupted / "live")
    shutil.copytree(tmp_path / "backup", corrupted / "backup")
    target = (
        corrupted
        / "backup"
        / "artifacts"
        / stored[0].relative_to(tmp_path / "backup" / "artifacts")
    )
    target.write_bytes(b"tampered")

    module, run_store_class = tuple(readiness.reconciliation_imports(ROOT))
    finding = checker._restore_finding(corrupted, module, run_store_class, case_id).to_dict()

    assert finding["status"] == readiness.FAILED
    assert finding["detail"]["source_bytes_match"] is False
    assert finding["detail"]["compared_against"] == "live"
    # The product's own hash re-check is what catches the tampering.
    assert finding["detail"]["restored_read_rejection"] == "SOURCE_UNVERIFIED"
    assert finding["detail"]["http_status"] == 409


def test_the_rehearsal_leaves_nothing_behind_in_the_repository(tmp_path: Path) -> None:
    before = {path.name for path in ROOT.iterdir()}

    readiness.ReadinessChecker(ROOT, environ={}).rehearse_database(tmp_path)

    assert {path.name for path in ROOT.iterdir()} == before
    assert (tmp_path / "live" / "state.sqlite3").is_file()


def test_an_immutable_table_that_accepts_a_delete_is_reported(tmp_path: Path) -> None:
    checker = readiness.ReadinessChecker(ROOT, environ={})
    checker.rehearse_database(tmp_path)
    database = tmp_path / "live" / "state.sqlite3"
    module = next(readiness.reconciliation_imports(ROOT))
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER reconciliation_case_no_delete")
        db.commit()

    finding = checker._immutability_finding(database, module).to_dict()

    assert finding["status"] == readiness.FAILED
    assert finding["detail"]["unprotected"] == ["reconciliation_case.DELETE"]


# --------------------------------------------------------------------------- #
# temporary workspace ownership
# --------------------------------------------------------------------------- #


def test_the_workspace_is_created_under_temp_and_removed_after_use() -> None:
    with readiness.owned_workspace() as workspace:
        created = workspace
        assert workspace.is_dir()
        assert workspace.parent == Path(tempfile.gettempdir()).resolve()
        assert workspace.name.startswith("reconciliation-readiness-")
        (workspace / "scratch.txt").write_text("x", encoding="utf-8")

    assert not created.exists()


def test_a_workspace_outside_the_temp_root_is_refused_rather_than_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keep = tmp_path / "reconciliation-readiness-lookalike"
    keep.mkdir()
    (keep / "precious.txt").write_text("do not delete", encoding="utf-8")
    monkeypatch.setattr(readiness.tempfile, "mkdtemp", lambda prefix: str(keep))

    with readiness.owned_workspace() as workspace:
        assert workspace == keep.resolve()

    assert keep.is_dir()
    assert (keep / "precious.txt").read_text(encoding="utf-8") == "do not delete"
    assert "refusing to remove unexpected workspace" in capsys.readouterr().err


def test_a_workspace_without_the_expected_prefix_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = Path(tempfile.mkdtemp(prefix="unrelated-directory-"))
    (foreign / "precious.txt").write_text("do not delete", encoding="utf-8")
    try:
        monkeypatch.setattr(readiness.tempfile, "mkdtemp", lambda prefix: str(foreign))
        with readiness.owned_workspace():
            pass

        assert foreign.is_dir()
        assert (foreign / "precious.txt").is_file()
        assert "refusing to remove unexpected workspace" in capsys.readouterr().err
    finally:
        (foreign / "precious.txt").unlink()
        foreign.rmdir()


def test_workspace_resolution_change_refuses_recursive_cleanup(tmp_path, monkeypatch):
    keep = tmp_path / "reconciliation-readiness-owned"
    keep.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    original_resolve = Path.resolve
    removals = []
    monkeypatch.setattr(readiness.tempfile, "mkdtemp", lambda prefix: str(keep))
    monkeypatch.setattr(readiness.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(readiness.shutil, "rmtree", lambda *args, **kwargs: removals.append(args))
    with readiness.owned_workspace():

        def redirected(path, **kwargs):
            return foreign if path == keep else original_resolve(path, **kwargs)

        monkeypatch.setattr(Path, "resolve", redirected)
    assert removals == []
    assert keep.is_dir() and foreign.is_dir()


# --------------------------------------------------------------------------- #
# application wiring
# --------------------------------------------------------------------------- #


def test_the_real_application_assembles_against_a_throwaway_database(tmp_path: Path) -> None:
    checker = readiness.ReadinessChecker(ROOT, environ={})

    finding = checker.check_application_wiring(tmp_path).to_dict()

    assert finding["status"] == readiness.PASSED
    assert finding["detail"]["required_routes_missing"] == []
    assert finding["detail"]["total_routes"] > 40
    assert sorted(finding["detail"]["sandbox_files_created"]) == [
        "reconciliation-artifacts",
        "state.sqlite3",
    ]


def test_the_application_check_leaves_the_repository_database_untouched(tmp_path: Path) -> None:
    repository_database = ROOT / ".local" / "state.sqlite3"
    before = repository_database.stat().st_mtime_ns if repository_database.exists() else None
    existed = repository_database.exists()

    readiness.ReadinessChecker(ROOT, environ={}).check_application_wiring(tmp_path)

    assert repository_database.exists() is existed
    if before is not None:
        assert repository_database.stat().st_mtime_ns == before


def test_the_application_check_restores_the_process_environment(tmp_path: Path) -> None:
    sentinel = "readiness-sentinel-value"
    previous = os.environ.get("LOCAL_DATABASE_PATH")
    os.environ["LOCAL_DATABASE_PATH"] = sentinel
    try:
        readiness.ReadinessChecker(ROOT, environ={}).check_application_wiring(tmp_path)
        assert os.environ["LOCAL_DATABASE_PATH"] == sentinel
    finally:
        if previous is None:
            os.environ.pop("LOCAL_DATABASE_PATH", None)
        else:
            os.environ["LOCAL_DATABASE_PATH"] = previous


# --------------------------------------------------------------------------- #
# command-line behaviour
# --------------------------------------------------------------------------- #


def test_an_existing_output_directory_is_rejected_without_overwriting_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    keep = output / "keep.txt"
    keep.write_text("original", encoding="utf-8")

    assert readiness.main(["--output", str(output)]) == 2
    assert keep.read_text(encoding="utf-8") == "original"
    assert list(output.iterdir()) == [keep]
    assert "already exists" in capsys.readouterr().err


def test_a_full_run_writes_both_report_files_and_reports_the_local_only_scope(
    tmp_path: Path,
) -> None:
    output = tmp_path / "run"

    code = readiness.main(["--output", str(output), "--root", str(ROOT)])

    report = json.loads((output / "readiness.json").read_text(encoding="utf-8"))
    markdown = (output / "readiness.md").read_text(encoding="utf-8")
    assert code in (0, 1)  # a host missing pnpm or Node 22 legitimately exits 1
    assert report["schema_version"] == readiness.SCHEMA_VERSION
    assert report["summary"]["failed"] == 0
    assert report["gates"][readiness.CLOUD]["ready"] is False
    assert report["tool_authority"]["deploys"] is False
    assert {finding["status"] for finding in report["findings"]} <= {
        readiness.PASSED,
        readiness.BLOCKED,
        readiness.NOT_RUN,
    }
    assert "| Target | Ready |" in markdown
    assert "cloud_deployment" in markdown
    if code == 1:
        assert report["gates"][readiness.LOCAL]["blocking_checks"]


def test_the_markdown_render_lists_every_finding_and_external_input() -> None:
    report = readiness.build_report(
        [
            readiness.Finding(
                "cloud.account",
                "cloud",
                readiness.CLOUD,
                readiness.BLOCKED,
                "no account exists",
                external_input="A real AWS account.",
            )
        ],
        ROOT,
    )

    markdown = readiness.render_markdown(report)

    assert "`cloud.account`" in markdown
    assert "**blocked**" in markdown
    assert "- A real AWS account." in markdown


def test_the_checker_module_uses_no_cloud_or_http_client() -> None:
    source = (ROOT / "scripts/check_reconciliation_readiness.py").read_text(encoding="utf-8")

    for forbidden in ("boto3", "botocore", "httpx", "urllib.request", "aiohttp", "urlopen"):
        assert forbidden not in source, f"{forbidden} must not appear in an offline checker"
