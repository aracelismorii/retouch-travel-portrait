#!/usr/bin/env python3
"""Maintain a non-destructive, ordered V6 portrait-retouch run state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

import check_geometry
import compile_pipeline as pipeline_compiler
import audit_result as audit_checker
import input_preflight


STAGE_ORDER = [
    "photo_correction",
    "light_balance",
    "color_balance",
    "skin_cleanup",
    "facial_features",
    "hair_clothing",
    "background",
    "final_review",
]
NON_FINAL_DECISIONS = {"accepted", "no_change", "rejected"}
MODEL_STATUSES = {"reserved", "awaiting_review", "failed", "rejected", "accepted"}
REQUIRED_AUDIT_CHECKS = [
    "skin_texture_natural",
    "face_neck_color_consistent",
    "sclera_teeth_not_blue",
    "lip_saturation_natural",
    "background_lines_straight",
    "hair_edges_clean",
    "subject_background_brightness_cohesive",
    "zoom_and_thumbnail_coherent",
    "change_not_excessive_vs_original",
    "phone_viewing_comfortable",
]


class PipelineStateError(ValueError):
    """Raised when a V6 run would violate ordering or provenance rules."""


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PipelineStateError(f"expected a JSON object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != 6:
        raise PipelineStateError("pipeline plan must use schema_version 6")
    stages = plan.get("stages")
    if not isinstance(stages, list):
        raise PipelineStateError("pipeline plan must contain a stages list")
    observed = [stage.get("id") for stage in stages if isinstance(stage, dict)]
    if observed != STAGE_ORDER:
        raise PipelineStateError(
            "pipeline stages must exactly match the required V6 order: "
            + ", ".join(STAGE_ORDER)
        )
    required_checks = plan.get("required_audit_checks")
    if required_checks != REQUIRED_AUDIT_CHECKS:
        raise PipelineStateError(
            "pipeline plan must require the fixed ordered ten audit checks"
        )

    # Do not trust a hand-authored plan merely because its stage ids look right.
    # Recompile the exact requested values against the installed V6 schemas and
    # require byte-for-byte-equivalent JSON structure. This checks parameter
    # ranges, actions, checkpoint contracts, mode/preset, and model policy in one
    # place without maintaining a second looser schema in the state machine.
    try:
        pipeline = pipeline_compiler.load_schema()
        parameters, gates = pipeline_compiler.load_linked_schemas(pipeline)
        overrides: dict[str, Any] = {}
        for stage in stages:
            if not isinstance(stage, dict):
                raise PipelineStateError("every compiled stage must be an object")
            stage_id = stage.get("id")
            stage_parameters = stage.get("parameters")
            if not isinstance(stage_parameters, dict):
                raise PipelineStateError(
                    f"compiled stage {stage_id!r} parameters must be an object"
                )
            for name, value in stage_parameters.items():
                overrides[f"{stage_id}.{name}"] = value
        expected = pipeline_compiler.compile_pipeline(
            pipeline,
            parameters,
            gates,
            mode=plan.get("mode"),
            overrides=overrides,
        )
    except (OSError, json.JSONDecodeError, pipeline_compiler.PipelineConfigError) as exc:
        raise PipelineStateError(f"pipeline plan failed schema validation: {exc}") from exc
    if plan != expected:
        raise PipelineStateError(
            "pipeline plan differs from the canonical V6 compiler output"
        )


@contextmanager
def _run_lock(run_dir: Path):
    """Serialize mutations for one run without leaving a stale crash lock."""

    run_dir = run_dir.resolve()
    lock_path = run_dir / ".pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineStateError(
                "another process is already modifying this run"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _locked_mutation(function):
    @wraps(function)
    def wrapper(run_dir: Path, *args, **kwargs):
        with _run_lock(Path(run_dir)):
            return function(Path(run_dir), *args, **kwargs)

    return wrapper


def _inside_run(run_dir: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise PipelineStateError(f"{label}.path must be non-empty text")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise PipelineStateError(f"{label}.path must be run-relative")
    resolved = (run_dir / relative).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exc:
        raise PipelineStateError(f"{label}.path escapes the run directory") from exc
    return resolved


def _run_bound_path(run_dir: Path, raw_path: Any, label: str) -> Path:
    """Resolve an absolute or run-relative path but never leave the run."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PipelineStateError(f"{label} must be a non-empty path")
    selected = Path(raw_path)
    resolved = selected.resolve() if selected.is_absolute() else (run_dir / selected).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exc:
        raise PipelineStateError(f"{label} must stay inside the run directory") from exc
    if not resolved.is_file():
        raise PipelineStateError(f"{label} file is missing")
    return resolved


def _validate_model_lifecycle(
    run_dir: Path,
    state: dict[str, Any],
    *,
    accepted_checkpoint: dict[str, Any] | None,
) -> None:
    model = state.get("model")
    if model is None:
        return
    if not isinstance(model, dict) or model.get("calls_recorded") != 1:
        raise PipelineStateError("model lifecycle must consume exactly one recorded call")
    status = model.get("status")
    if status not in MODEL_STATUSES:
        raise PipelineStateError("model lifecycle has an unsupported status")
    if model.get("input_sha256") != state.get("source_sha256"):
        raise PipelineStateError("model lifecycle input is not the immutable original")
    if model.get("chained") is not False or model.get("retried") is not False:
        raise PipelineStateError("model lifecycle violates the no-chain/no-retry policy")

    events = state.get("events", [])
    reserve_events = [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "model_attempt_reserved"
    ]
    if len(reserve_events) != 1:
        raise PipelineStateError("model lifecycle must have one reservation event")

    for key, label in (("request", "model request"), ("prompt", "model prompt")):
        record = model.get(key)
        if not isinstance(record, dict):
            raise PipelineStateError(f"{label} binding is missing")
        path = _inside_run(run_dir, record.get("path"), label)
        if not path.is_file() or record.get("sha256") != _sha256(path):
            raise PipelineStateError(f"{label} changed after the call was reserved")

    candidate_record = model.get("candidate")
    if status in {"awaiting_review", "rejected", "accepted"}:
        candidate = _validate_checkpoint_record(
            run_dir, candidate_record, "model candidate"
        )
        if model.get("candidate_sha256") != candidate["sha256"]:
            raise PipelineStateError("model candidate lifecycle hash is inconsistent")
        completion_events = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("type") == "model_attempt_completed"
            and event.get("candidate_sha256") == candidate["sha256"]
        ]
        if len(completion_events) != 1:
            raise PipelineStateError("model candidate has no unique completion event")
    elif candidate_record is not None or model.get("candidate_sha256") is not None:
        raise PipelineStateError("model lifecycle records a candidate before completion")

    if status == "failed":
        if not isinstance(model.get("failure_reason"), str) or not model["failure_reason"].strip():
            raise PipelineStateError("failed model attempt requires a reason")
        if len(
            [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "model_attempt_failed"
            ]
        ) != 1:
            raise PipelineStateError("failed model attempt has no failure event")
    if status == "rejected":
        if not isinstance(model.get("review_notes"), str) or not model["review_notes"].strip():
            raise PipelineStateError("rejected model attempt requires review notes")
        if len(
            [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "model_attempt_rejected"
            ]
        ) != 1:
            raise PipelineStateError("rejected model attempt has no rejection event")
    if status == "accepted":
        if accepted_checkpoint is None:
            raise PipelineStateError("accepted model lifecycle has no accepted checkpoint")
        if not _same_image_record(candidate_record, accepted_checkpoint):
            raise PipelineStateError("accepted model checkpoint differs from its candidate")
        if model.get("successful_candidates") != 1:
            raise PipelineStateError("accepted model lifecycle must record one candidate")
        audit_record = model.get("audit")
        if not isinstance(audit_record, dict):
            raise PipelineStateError("accepted model lifecycle has no audit binding")
        audit_path = _inside_run(run_dir, audit_record.get("path"), "model audit")
        if audit_record.get("sha256") != _sha256(audit_path):
            raise PipelineStateError("model acceptance audit changed")
        accepted_events = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("type") == "isolated_model_candidate_accepted"
            and event.get("candidate_sha256") == accepted_checkpoint["sha256"]
        ]
        if len(accepted_events) != 1:
            raise PipelineStateError("accepted model candidate has no acceptance event")
    elif model.get("successful_candidates", 0) != 0:
        raise PipelineStateError("unaccepted model lifecycle cannot record success")


def _validated_audit(
    run_dir: Path,
    state: dict[str, Any],
    plan: dict[str, Any],
    audit_path: Path,
    candidate: Path,
) -> dict[str, Any]:
    """Recompute an accepted audit and its proof binding from immutable files."""

    audit = _read_json(audit_path)
    if audit.get("schema_version") != 6:
        raise PipelineStateError("audit must use schema_version 6")
    if audit.get("checker") != "portrait-retouch-audit-v6":
        raise PipelineStateError("audit checker identity is invalid")
    if audit.get("mode") != state.get("mode"):
        raise PipelineStateError("audit mode does not match the run mode")
    proof = audit.get("proof")
    if not isinstance(proof, dict) or proof.get("verified") is not True:
        raise PipelineStateError("audit must contain verified visual proof")
    manifest_record = proof.get("manifest")
    if not isinstance(manifest_record, dict):
        raise PipelineStateError("audit proof manifest binding is missing")
    manifest_path = _run_bound_path(
        run_dir, manifest_record.get("path"), "audit proof manifest"
    )
    if manifest_record.get("sha256") != _sha256(manifest_path):
        raise PipelineStateError("audit proof manifest changed after review")

    checks = audit.get("checks")
    if not isinstance(checks, dict) or list(checks) != plan["required_audit_checks"]:
        raise PipelineStateError("audit checks do not match the required ordered list")
    manual: dict[str, dict[str, str]] = {}
    for check_id, item in checks.items():
        if not isinstance(item, dict):
            raise PipelineStateError(f"audit check {check_id} must be an object")
        status = item.get("manual_status")
        note = item.get("manual_note")
        if status not in {"pass", "not_applicable"}:
            raise PipelineStateError(f"audit check {check_id} is unresolved")
        if not isinstance(note, str) or not note.strip():
            raise PipelineStateError(f"audit check {check_id} has no review evidence")
        manual[check_id] = {"status": status, "note": note}

    original = _run_bound_path(
        run_dir, state["original"]["path"], "immutable original"
    )
    try:
        recomputed = audit_checker.audit_images(
            original,
            candidate,
            mode=state["mode"],
            manual_review={"checks": manual},
            proof_manifest=manifest_path,
        )
    except (OSError, ValueError, audit_checker.AuditConfigError) as exc:
        raise PipelineStateError(f"audit proof revalidation failed: {exc}") from exc
    def canonical_paths(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    str(Path(item).resolve())
                    if key == "path" and isinstance(item, str)
                    else canonical_paths(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [canonical_paths(item) for item in value]
        return value

    if canonical_paths(recomputed) != canonical_paths(audit):
        raise PipelineStateError(
            "audit differs from a fresh verification of the current image and proof"
        )
    if recomputed.get("decision") != "accept":
        raise PipelineStateError("audit final decision must be accept")
    summary = recomputed.get("summary", {})
    if (
        summary.get("decision") != "accept"
        or summary.get("manual_review_complete") is not True
        or summary.get("proof_verified") is not True
        or summary.get("may_auto_accept") is not False
    ):
        raise PipelineStateError("audit acceptance invariants are incomplete")
    return recomputed


def _validate_checkpoint_record(
    run_dir: Path, record: Any, label: str
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise PipelineStateError(f"{label} must be a checkpoint object")
    path = _inside_run(run_dir, record.get("path"), label)
    if not path.is_file():
        raise PipelineStateError(f"{label} file is missing")
    actual_hash = _sha256(path)
    if record.get("sha256") != actual_hash:
        raise PipelineStateError(f"{label} hash changed")
    width, height = check_geometry.read_dimensions(path)
    if record.get("width") != width or record.get("height") != height:
        raise PipelineStateError(f"{label} dimensions do not match its file")
    expected_ratio = round(width / height, 8)
    if record.get("aspect_ratio") != expected_ratio:
        raise PipelineStateError(f"{label} aspect ratio is inconsistent")
    return record


def _same_checkpoint(left: Any, right: Any) -> bool:
    keys = ("path", "sha256", "width", "height", "aspect_ratio")
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(key) == right.get(key) for key in keys
    )


def _same_image_record(left: Any, right: Any) -> bool:
    keys = ("sha256", "width", "height", "aspect_ratio")
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(key) == right.get(key) for key in keys
    )


def _validate_attempts(
    attempts: Any,
    *,
    stage_id: str,
    input_hash: str,
    completed_status: str | None,
) -> None:
    if not isinstance(attempts, list):
        raise PipelineStateError(f"stage {stage_id} attempts must be a list")
    allowed = NON_FINAL_DECISIONS | {"model_candidate_accepted"}
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("decision") not in allowed:
            raise PipelineStateError(f"stage {stage_id} contains an invalid attempt")
        if attempt.get("input_sha256") != input_hash:
            raise PipelineStateError(f"stage {stage_id} attempt breaks checkpoint provenance")
        notes = attempt.get("notes")
        if not isinstance(notes, str) or not notes.strip():
            raise PipelineStateError(f"stage {stage_id} attempt requires review notes")
    if completed_status is not None:
        if not attempts or attempts[-1].get("decision") != completed_status:
            raise PipelineStateError(
                f"stage {stage_id} status has no matching final attempt"
            )
    elif any(attempt.get("decision") != "rejected" for attempt in attempts):
        raise PipelineStateError(
            f"pending stage {stage_id} may contain only rejected attempts"
        )


def _validate_stage_event(
    events: list[Any], stage_id: str, decision: str, checkpoint_hash: str
) -> None:
    matches = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("type") == "stage_completed"
        and event.get("stage") == stage_id
        and event.get("decision") == decision
        and event.get("checkpoint_sha256") == checkpoint_hash
    ]
    if len(matches) != 1:
        raise PipelineStateError(
            f"stage {stage_id} status must have exactly one matching completion event"
        )


def _validate_state(run_dir: Path, state: dict[str, Any], plan: dict[str, Any]) -> None:
    """Reject incomplete or simply edited run state before any operation."""

    if state.get("schema_version") != 6:
        raise PipelineStateError("run state must use schema_version 6")
    if state.get("mode") != plan.get("mode") or state.get("preset") != plan.get("preset"):
        raise PipelineStateError("run mode or preset differs from the immutable plan")
    if state.get("status") not in {"in_progress", "complete"}:
        raise PipelineStateError("run state has an unsupported status")
    events = state.get("events")
    if not isinstance(events, list) or not events:
        raise PipelineStateError("run state must contain provenance events")
    if not isinstance(events[0], dict) or events[0].get("type") != "run_initialized":
        raise PipelineStateError("run state must begin with its initialization event")
    original = _validate_checkpoint_record(run_dir, state.get("original"), "original")
    if state.get("source_sha256") != original["sha256"]:
        raise PipelineStateError("run source hash differs from the immutable original")
    source_name = state.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
    ):
        raise PipelineStateError("run source name must not expose a local path")
    preflight = state.get("input_preflight")
    if (
        not isinstance(preflight, dict)
        or preflight.get("checker") != input_preflight.CHECKER_NAME
        or preflight.get("decision") != "accept"
        or preflight.get("source", {}).get("sha256") != original["sha256"]
        or preflight.get("source", {}).get("name") != source_name
        or preflight.get("source", {}).get("display_dimensions")
        != {"width": original["width"], "height": original["height"]}
    ):
        raise PipelineStateError("run input preflight binding is missing or inconsistent")
    stages = state.get("stages")
    if not isinstance(stages, dict) or list(stages) != STAGE_ORDER:
        raise PipelineStateError("run state must contain the exact ordered V6 stages")
    for position, stage_id in enumerate(STAGE_ORDER, start=1):
        stage = stages[stage_id]
        if not isinstance(stage, dict) or stage.get("position") != position:
            raise PipelineStateError(f"stage {stage_id} has an invalid position")

    model_statuses = [
        stages[stage_id].get("status") == "model_candidate_accepted"
        for stage_id in STAGE_ORDER[:-1]
    ]
    if any(model_statuses) and not all(model_statuses):
        raise PipelineStateError("model candidate status must cover all seven edit stages")

    checkpoint = original
    first_pending: str | None = None
    if all(model_statuses):
        model = state.get("model")
        model_checkpoint: dict[str, Any] | None = None
        for stage_id in STAGE_ORDER[:-1]:
            stage = stages[stage_id]
            attempts = stage.get("attempts")
            _validate_attempts(
                attempts,
                stage_id=stage_id,
                input_hash=original["sha256"],
                completed_status="model_candidate_accepted",
            )
            accepted = _validate_checkpoint_record(
                run_dir, stage.get("accepted_checkpoint"), f"stage {stage_id} checkpoint"
            )
            if model_checkpoint is None:
                model_checkpoint = accepted
            elif not _same_checkpoint(model_checkpoint, accepted):
                raise PipelineStateError("model stages do not share one isolated checkpoint")
        checkpoint = model_checkpoint or original
        _validate_model_lifecycle(
            run_dir, state, accepted_checkpoint=checkpoint
        )
    else:
        pending_seen = False
        for stage_id in STAGE_ORDER[:-1]:
            stage = stages[stage_id]
            status = stage.get("status")
            if status == "pending":
                if first_pending is None:
                    first_pending = stage_id
                elif stage.get("attempts"):
                    raise PipelineStateError(
                        f"future pending stage {stage_id} must not have attempts"
                    )
                pending_seen = True
                _validate_attempts(
                    stage.get("attempts"),
                    stage_id=stage_id,
                    input_hash=checkpoint["sha256"],
                    completed_status=None,
                )
                continue
            if pending_seen:
                raise PipelineStateError("completed stages must form one contiguous prefix")
            if status not in {"accepted", "no_change"}:
                raise PipelineStateError(f"stage {stage_id} has invalid status {status!r}")
            _validate_attempts(
                stage.get("attempts"),
                stage_id=stage_id,
                input_hash=checkpoint["sha256"],
                completed_status=status,
            )
            accepted = _validate_checkpoint_record(
                run_dir, stage.get("accepted_checkpoint"), f"stage {stage_id} checkpoint"
            )
            if status == "accepted":
                final_attempt = stage["attempts"][-1]
                candidate = final_attempt.get("candidate")
                if not isinstance(candidate, dict) or candidate.get("sha256") != accepted["sha256"]:
                    raise PipelineStateError(
                        f"stage {stage_id} accepted checkpoint does not match its candidate"
                    )
                if final_attempt.get("geometry_gate") != "pass":
                    raise PipelineStateError(f"stage {stage_id} accepted a failed geometry gate")
                checkpoint = accepted
            elif not _same_checkpoint(accepted, checkpoint):
                raise PipelineStateError(f"stage {stage_id} no-change altered the checkpoint")
            _validate_stage_event(events, stage_id, status, checkpoint["sha256"])
        _validate_model_lifecycle(run_dir, state, accepted_checkpoint=None)
        model = state.get("model")
        if isinstance(model, dict) and model.get("status") == "accepted":
            raise PipelineStateError("accepted model lifecycle is missing accepted stages")

    final_stage = stages["final_review"]
    if state["status"] == "in_progress":
        if final_stage.get("status") != "pending" or final_stage.get("attempts"):
            raise PipelineStateError("in-progress run must keep final_review pending")
        if first_pending is None:
            first_pending = "final_review"
        if state.get("current_stage") != first_pending:
            raise PipelineStateError("current_stage is not the first pending stage")
        current = _validate_checkpoint_record(
            run_dir, state.get("current_checkpoint"), "current checkpoint"
        )
        if not _same_checkpoint(current, checkpoint):
            raise PipelineStateError("current checkpoint breaks the accepted stage chain")
    else:
        if first_pending is not None or final_stage.get("status") != "accepted":
            raise PipelineStateError("complete run contains unresolved stages")
        if state.get("current_stage") is not None:
            raise PipelineStateError("complete run must not have a current stage")
        final = _validate_checkpoint_record(run_dir, state.get("final"), "final")
        current = _validate_checkpoint_record(
            run_dir, state.get("current_checkpoint"), "current checkpoint"
        )
        if not _same_checkpoint(final, current):
            raise PipelineStateError("complete run current checkpoint is not its final")
        if final["sha256"] != checkpoint["sha256"]:
            raise PipelineStateError("final pixels do not match the accepted checkpoint")
        _validate_attempts(
            final_stage.get("attempts"),
            stage_id="final_review",
            input_hash=checkpoint["sha256"],
            completed_status="accepted",
        )
        accepted_final = _validate_checkpoint_record(
            run_dir,
            final_stage.get("accepted_checkpoint"),
            "stage final_review checkpoint",
        )
        if not _same_checkpoint(accepted_final, final):
            raise PipelineStateError("final_review checkpoint does not match final output")
        _validate_stage_event(events, "final_review", "accepted", final["sha256"])
        completion_events = [
            event
            for event in events
            if isinstance(event, dict) and event.get("type") == "run_completed"
        ]
        if len(completion_events) != 1 or completion_events[0].get(
            "final_sha256"
        ) != final["sha256"]:
            raise PipelineStateError("complete run has no unique matching completion event")
        reviewed = _validate_checkpoint_record(
            run_dir,
            final_stage.get("reviewed_checkpoint"),
            "final_review reviewed checkpoint",
        )
        if not _same_image_record(reviewed, final):
            raise PipelineStateError("final output differs from the reviewed candidate pixels")
        audit_record = final_stage.get("audit")
        if not isinstance(audit_record, dict):
            raise PipelineStateError("final_review audit binding is missing")
        audit_path = _inside_run(
            run_dir, audit_record.get("path"), "final_review audit"
        )
        if audit_record.get("sha256") != _sha256(audit_path):
            raise PipelineStateError("final_review audit changed after completion")
        reviewed_path = _inside_run(
            run_dir, reviewed.get("path"), "final_review reviewed checkpoint"
        )
        verified_audit = _validated_audit(
            run_dir, state, plan, audit_path, reviewed_path
        )
        final_attempt = final_stage["attempts"][-1]
        if (
            final_attempt.get("audit_sha256") != audit_record["sha256"]
            or final_attempt.get("proof_manifest_sha256")
            != verified_audit["proof"]["manifest"]["sha256"]
        ):
            raise PipelineStateError("final_review attempt is not bound to its audit proof")


def _checkpoint_record(path: Path, run_dir: Path) -> dict[str, Any]:
    width, height = check_geometry.read_dimensions(path)
    return {
        "path": str(path.relative_to(run_dir)),
        "sha256": _sha256(path),
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 8),
    }


def _load_state(run_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.resolve()
    state_path = run_dir / "run.json"
    plan_path = run_dir / "plan.json"
    if not state_path.is_file() or not plan_path.is_file():
        raise PipelineStateError("run directory must contain run.json and plan.json")
    state = _read_json(state_path)
    plan = _read_json(plan_path)
    if state.get("plan_sha256") != _sha256(plan_path):
        raise PipelineStateError("run plan hash changed after initialization")
    _validate_plan(plan)
    _validate_state(run_dir, state, plan)
    return run_dir, state, plan


def validate_run(run_dir: Path) -> dict[str, Any]:
    """Public read-only validation entry point for batch/orchestrator callers."""

    resolved, state, _ = _load_state(run_dir)
    return {
        "run_dir": str(resolved),
        "status": state["status"],
        "current_stage": state.get("current_stage"),
        "source_sha256": state["source_sha256"],
        "mode": state["mode"],
        "model_calls": state.get("model", {}).get("calls_recorded", 0),
    }


def _current_checkpoint(run_dir: Path, state: dict[str, Any]) -> Path:
    checkpoint = run_dir / str(state["current_checkpoint"]["path"])
    if not checkpoint.is_file():
        raise PipelineStateError("current accepted checkpoint is missing")
    if _sha256(checkpoint) != state["current_checkpoint"]["sha256"]:
        raise PipelineStateError("current accepted checkpoint hash changed")
    return checkpoint


def _next_stage(state: dict[str, Any]) -> str | None:
    for stage_id in STAGE_ORDER:
        if state["stages"][stage_id]["status"] == "pending":
            return stage_id
    return None


def init_run(source: Path, plan_path: Path, run_dir: Path) -> dict[str, Any]:
    source = source.resolve()
    plan_path = plan_path.resolve()
    run_dir = run_dir.resolve()
    if not source.is_file():
        raise PipelineStateError(f"source image does not exist: {source}")
    try:
        preflight = input_preflight.preflight_image(source)
    except input_preflight.PreflightError as exc:
        raise PipelineStateError(
            f"input preflight rejected {source.name}: {exc.code}: {exc.message}"
        ) from exc
    plan = _read_json(plan_path)
    _validate_plan(plan)
    if run_dir.exists() and not run_dir.is_dir():
        raise PipelineStateError(f"run path is not a directory: {run_dir}")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise PipelineStateError("run directory must be new or empty")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    (run_dir / "checkpoints").mkdir(mode=0o700)

    suffix = source.suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    original = run_dir / f"original{suffix}"
    shutil.copy2(source, original)
    shutil.copy2(plan_path, run_dir / "plan.json")
    os.chmod(original, 0o600)
    os.chmod(run_dir / "plan.json", 0o600)
    original_record = _checkpoint_record(original, run_dir)
    state = {
        "schema_version": 6,
        "status": "in_progress",
        "plan_sha256": _sha256(run_dir / "plan.json"),
        "mode": plan.get("mode"),
        "preset": plan.get("preset"),
        "source_name": source.name,
        "source_sha256": original_record["sha256"],
        "input_preflight": preflight,
        "original": original_record,
        "current_checkpoint": original_record,
        "current_stage": STAGE_ORDER[0],
        "stages": {
            stage_id: {"position": index, "status": "pending", "attempts": []}
            for index, stage_id in enumerate(STAGE_ORDER, start=1)
        },
        "events": [
            {
                "type": "run_initialized",
                "at": _now(),
                "message": "Immutable original and ordered V6 plan preserved.",
            }
        ],
    }
    _write_json(run_dir / "run.json", state)
    return {
        "run_dir": str(run_dir),
        "state": str(run_dir / "run.json"),
        "current_stage": state["current_stage"],
        "original_sha256": state["source_sha256"],
    }


@_locked_mutation
def record_stage(
    run_dir: Path,
    stage_id: str,
    decision: str,
    input_path: Path | None = None,
    candidate_path: Path | None = None,
    notes: str = "",
) -> dict[str, Any]:
    run_dir, state, plan = _load_state(run_dir)
    if state.get("status") != "in_progress":
        raise PipelineStateError("run is not in progress")
    model = state.get("model")
    if isinstance(model, dict) and model.get("status") in {
        "reserved",
        "awaiting_review",
    }:
        raise PipelineStateError(
            "finish or reject the reserved model attempt before deterministic stages"
        )
    expected = _next_stage(state)
    if expected is None:
        raise PipelineStateError("all stages have already been recorded")
    if stage_id != expected:
        raise PipelineStateError(f"expected stage {expected}, got {stage_id}")
    if stage_id == "final_review":
        raise PipelineStateError("use finalize for the final_review stage")
    if decision not in NON_FINAL_DECISIONS:
        raise PipelineStateError("decision must be accepted, no_change, or rejected")
    if not isinstance(notes, str) or not notes.strip():
        raise PipelineStateError("every stage decision requires image-specific notes")

    current = _current_checkpoint(run_dir, state)
    resolved_input = (input_path or current).resolve()
    if not resolved_input.is_file() or _sha256(resolved_input) != _sha256(current):
        raise PipelineStateError(
            "stage input must be the current accepted checkpoint with matching hash"
        )

    stage_state = state["stages"][stage_id]
    attempt: dict[str, Any] = {
        "decision": decision,
        "at": _now(),
        "input_sha256": _sha256(current),
        "notes": notes,
    }
    if candidate_path is not None:
        candidate = candidate_path.resolve()
        if not candidate.is_file():
            raise PipelineStateError(f"candidate does not exist: {candidate}")
        try:
            candidate_relative: str | None = str(candidate.relative_to(run_dir))
        except ValueError:
            candidate_relative = None
        original_path = run_dir / str(state["original"]["path"])
        original_width, original_height = check_geometry.read_dimensions(original_path)
        width, height = check_geometry.read_dimensions(candidate)
        ratio_delta = abs((width / height) / (original_width / original_height) - 1.0)
        geometry = check_geometry.compare_geometry(original_path, candidate)
        attempt["candidate"] = {
            "sha256": _sha256(candidate),
            "width": width,
            "height": height,
            "relative_aspect_ratio_delta": round(ratio_delta, 8),
        }
        if candidate_relative is not None:
            attempt["candidate"]["path"] = candidate_relative
        else:
            attempt["candidate"]["source_name"] = candidate.name
        if geometry["decision"] != "accept" or min(width, height) < 768:
            if decision == "accepted":
                raise PipelineStateError(
                    "an accepted candidate must preserve the exact displayed canvas and resolution"
                )
            attempt["geometry_gate"] = "reject"
        else:
            attempt["geometry_gate"] = "pass"
    elif decision in {"accepted", "rejected"}:
        raise PipelineStateError(f"{decision} requires --candidate")

    stage_state["attempts"].append(attempt)
    if decision == "rejected":
        state["events"].append(
            {
                "type": "stage_candidate_rejected",
                "stage": stage_id,
                "at": _now(),
                "notes": notes,
            }
        )
        _write_json(run_dir / "run.json", state)
        return {
            "status": state["status"],
            "stage": stage_id,
            "decision": decision,
            "current_checkpoint": str(current),
        }

    if decision == "accepted":
        candidate = candidate_path.resolve()  # type: ignore[union-attr]
        suffix = candidate.suffix.lower() or ".png"
        destination = (
            run_dir
            / "checkpoints"
            / f"stage-{stage_state['position']:02d}-{stage_id}{suffix}"
        )
        if destination.exists():
            if _sha256(destination) != _sha256(candidate):
                raise PipelineStateError(f"existing checkpoint differs: {destination}")
        else:
            shutil.copy2(candidate, destination)
            os.chmod(destination, 0o600)
        checkpoint = _checkpoint_record(destination, run_dir)
    else:
        checkpoint = state["current_checkpoint"]

    stage_state["status"] = decision
    stage_state["accepted_checkpoint"] = checkpoint
    state["current_checkpoint"] = checkpoint
    state["current_stage"] = _next_stage(state)
    state["events"].append(
        {
            "type": "stage_completed",
            "stage": stage_id,
            "decision": decision,
            "at": _now(),
            "checkpoint_sha256": checkpoint["sha256"],
            "notes": notes,
        }
    )
    _write_json(run_dir / "run.json", state)
    return {
        "status": state["status"],
        "stage": stage_id,
        "decision": decision,
        "next_stage": state["current_stage"],
        "current_checkpoint": str(run_dir / checkpoint["path"]),
    }


def _copy_private(source: Path, destination: Path, label: str) -> None:
    source = source.resolve()
    if not source.is_file():
        raise PipelineStateError(f"{label} does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    if destination.exists():
        if _sha256(destination) != _sha256(source):
            raise PipelineStateError(f"existing {label} differs: {destination}")
    else:
        shutil.copy2(source, destination)
    os.chmod(destination, 0o600)


def _require_model_entry_point(
    state: dict[str, Any], plan: dict[str, Any]
) -> None:
    if state.get("status") != "in_progress":
        raise PipelineStateError("run is not in progress")
    if any(
        state["stages"][stage_id]["status"] != "pending"
        or state["stages"][stage_id]["attempts"]
        for stage_id in STAGE_ORDER
    ):
        raise PipelineStateError(
            "the isolated model path must be reserved before any stage attempt"
        )
    policy = plan.get("model_policy", {})
    if (
        policy.get("maximum_model_calls") != 1
        or policy.get("maximum_successful_candidates") != 1
        or policy.get("model_chaining_allowed") is not False
        or policy.get("model_retry_allowed") is not False
        or policy.get("model_candidate_source") != "immutable_original"
    ):
        raise PipelineStateError("plan does not enforce the V6 single-candidate model policy")


@_locked_mutation
def reserve_model_attempt(
    run_dir: Path,
    request_path: Path,
    prompt_path: Path,
    *,
    provider: str = "",
    model_name: str = "",
) -> dict[str, Any]:
    """Consume the run's one model-call allowance before any external call."""

    run_dir, state, plan = _load_state(run_dir)
    _require_model_entry_point(state, plan)
    if state.get("model") is not None:
        raise PipelineStateError("the single model-call allowance was already consumed")

    request_path = request_path.resolve()
    prompt_path = prompt_path.resolve()
    request = _read_json(request_path)
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise PipelineStateError(f"model prompt cannot be read: {exc}") from exc
    if (
        request.get("schema_version") != 6
        or request.get("model_enabled") is not True
        or request.get("planned_model_calls") != 1
        or request.get("maximum_total_model_calls") != 1
        or request.get("model_candidate_source") != "immutable_original"
        or request.get("model_retry_allowed") is not False
        or request.get("model_chaining_allowed") is not False
        or request.get("mode") != state["mode"]
        or request.get("stage_order") != STAGE_ORDER
        or request.get("required_audit_checks") != REQUIRED_AUDIT_CHECKS
    ):
        raise PipelineStateError("model request does not match the current V6 run policy")
    if not prompt_text or request.get("prompt", "").strip() != prompt_text:
        raise PipelineStateError("model prompt file does not match the compiled request")

    model_dir = run_dir / "model"
    request_copy = model_dir / "request.json"
    prompt_copy = model_dir / "prompt.txt"
    _copy_private(request_path, request_copy, "model request")
    _copy_private(prompt_path, prompt_copy, "model prompt")
    state["model"] = {
        "status": "reserved",
        "calls_recorded": 1,
        "successful_candidates": 0,
        "input_sha256": state["source_sha256"],
        "request": {
            "path": str(request_copy.relative_to(run_dir)),
            "sha256": _sha256(request_copy),
        },
        "prompt": {
            "path": str(prompt_copy.relative_to(run_dir)),
            "sha256": _sha256(prompt_copy),
        },
        "provider": provider.strip() if isinstance(provider, str) else "",
        "model_name": model_name.strip() if isinstance(model_name, str) else "",
        "chained": False,
        "retried": False,
        "reserved_at": _now(),
    }
    state["events"].append(
        {
            "type": "model_attempt_reserved",
            "at": _now(),
            "input_sha256": state["source_sha256"],
            "request_sha256": state["model"]["request"]["sha256"],
            "prompt_sha256": state["model"]["prompt"]["sha256"],
        }
    )
    _write_json(run_dir / "run.json", state)
    return {
        "status": "reserved",
        "model_calls": 1,
        "input": str(run_dir / state["original"]["path"]),
        "request": str(request_copy),
        "prompt": str(prompt_copy),
    }


@_locked_mutation
def complete_model_attempt(run_dir: Path, candidate_path: Path) -> dict[str, Any]:
    """Bind the one model response to the reserved call and await review."""

    run_dir, state, _ = _load_state(run_dir)
    model = state.get("model")
    if not isinstance(model, dict) or model.get("status") != "reserved":
        raise PipelineStateError("no reserved model attempt is awaiting a response")
    candidate_path = candidate_path.resolve()
    if not candidate_path.is_file():
        raise PipelineStateError(f"model candidate does not exist: {candidate_path}")
    original = run_dir / state["original"]["path"]
    geometry = check_geometry.compare_geometry(original, candidate_path)
    if geometry["decision"] != "accept":
        raise PipelineStateError(
            "model candidate must preserve the exact displayed canvas dimensions"
        )
    suffix = candidate_path.suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in {".jpg", ".png", ".webp"}:
        raise PipelineStateError("model candidate must be JPG, PNG, or WebP")
    destination = run_dir / "model" / f"candidate{suffix}"
    _copy_private(candidate_path, destination, "model candidate")
    record = _checkpoint_record(destination, run_dir)
    model["status"] = "awaiting_review"
    model["candidate"] = record
    model["candidate_sha256"] = record["sha256"]
    model["completed_at"] = _now()
    state["events"].append(
        {
            "type": "model_attempt_completed",
            "at": _now(),
            "candidate_sha256": record["sha256"],
        }
    )
    _write_json(run_dir / "run.json", state)
    return {
        "status": "awaiting_review",
        "candidate": str(destination),
        "candidate_sha256": record["sha256"],
        "model_calls": 1,
    }


@_locked_mutation
def fail_model_attempt(run_dir: Path, reason: str) -> dict[str, Any]:
    """Record an external service error; the consumed call can never be retried."""

    run_dir, state, _ = _load_state(run_dir)
    model = state.get("model")
    if not isinstance(model, dict) or model.get("status") != "reserved":
        raise PipelineStateError("no reserved model attempt can be marked failed")
    if not isinstance(reason, str) or not reason.strip():
        raise PipelineStateError("failed model attempt requires a reason")
    model["status"] = "failed"
    model["failure_reason"] = reason.strip()
    model["failed_at"] = _now()
    state["events"].append(
        {"type": "model_attempt_failed", "at": _now(), "reason": reason.strip()}
    )
    _write_json(run_dir / "run.json", state)
    return {"status": "failed", "model_calls": 1, "retry_allowed": False}


@_locked_mutation
def reject_model_attempt(run_dir: Path, notes: str) -> dict[str, Any]:
    """Reject the completed candidate while permanently consuming the call."""

    run_dir, state, _ = _load_state(run_dir)
    model = state.get("model")
    if not isinstance(model, dict) or model.get("status") != "awaiting_review":
        raise PipelineStateError("no completed model candidate is awaiting rejection")
    if not isinstance(notes, str) or not notes.strip():
        raise PipelineStateError("model rejection requires image-specific notes")
    model["status"] = "rejected"
    model["review_notes"] = notes.strip()
    model["reviewed_at"] = _now()
    state["events"].append(
        {"type": "model_attempt_rejected", "at": _now(), "notes": notes.strip()}
    )
    _write_json(run_dir / "run.json", state)
    return {"status": "rejected", "model_calls": 1, "retry_allowed": False}


@_locked_mutation
def record_model_candidate(
    run_dir: Path,
    audit_path: Path,
    notes: str = "",
) -> dict[str, Any]:
    """Accept the completed one-call model candidate after canonical proof review."""

    run_dir, state, plan = _load_state(run_dir)
    model = state.get("model")
    if not isinstance(model, dict) or model.get("status") != "awaiting_review":
        raise PipelineStateError("reserve and complete the model attempt before acceptance")
    if not isinstance(notes, str) or not notes.strip():
        raise PipelineStateError("model acceptance requires image-specific notes")
    candidate = _run_bound_path(
        run_dir, model["candidate"]["path"], "model candidate"
    )
    audit_path = audit_path.resolve()
    _validated_audit(run_dir, state, plan, audit_path, candidate)

    audit_copy = run_dir / "model" / "accepted-audit.json"
    _copy_private(audit_path, audit_copy, "model audit")
    checkpoint = model["candidate"]
    for stage_id in STAGE_ORDER[:-1]:
        stage_state = state["stages"][stage_id]
        stage_state["status"] = "model_candidate_accepted"
        stage_state["accepted_checkpoint"] = checkpoint
        stage_state["attempts"].append(
            {
                "decision": "model_candidate_accepted",
                "at": _now(),
                "input_sha256": state["source_sha256"],
                "candidate_sha256": checkpoint["sha256"],
                "logical_stage_order": STAGE_ORDER[:-1],
                "notes": notes.strip(),
            }
        )
    state["current_checkpoint"] = checkpoint
    state["current_stage"] = "final_review"
    model["status"] = "accepted"
    model["successful_candidates"] = 1
    model["review_notes"] = notes.strip()
    model["audit"] = {
        "path": str(audit_copy.relative_to(run_dir)),
        "sha256": _sha256(audit_copy),
    }
    model["reviewed_at"] = _now()
    state["events"].append(
        {
            "type": "isolated_model_candidate_accepted",
            "at": _now(),
            "input_sha256": state["source_sha256"],
            "candidate_sha256": checkpoint["sha256"],
            "notes": notes.strip(),
        }
    )
    _write_json(run_dir / "run.json", state)
    return {
        "status": state["status"],
        "decision": "model_candidate_accepted",
        "next_stage": "final_review",
        "current_checkpoint": str(candidate),
        "audit": str(audit_copy),
    }


@_locked_mutation
def finalize(run_dir: Path, audit_path: Path) -> dict[str, Any]:
    run_dir, state, plan = _load_state(run_dir)
    if state.get("status") != "in_progress":
        raise PipelineStateError("run is not in progress")
    expected = _next_stage(state)
    if expected != "final_review":
        raise PipelineStateError(
            f"final review is unavailable; next required stage is {expected}"
        )
    audit_path = audit_path.resolve()
    current = _current_checkpoint(run_dir, state)
    audit = _validated_audit(run_dir, state, plan, audit_path, current)
    reviewed_checkpoint = _checkpoint_record(current, run_dir)
    final_path = run_dir / f"final{current.suffix.lower()}"
    if final_path.exists():
        if _sha256(final_path) != _sha256(current):
            raise PipelineStateError(f"existing final output differs: {final_path}")
    else:
        shutil.copy2(current, final_path)
        os.chmod(final_path, 0o600)
    audit_copy = run_dir / "audit.json"
    # The documented workflow writes the audit directly into the run directory.
    # Avoid SameFileError while still preserving external audit files when used.
    if audit_path != audit_copy.resolve():
        _copy_private(audit_path, audit_copy, "final audit")
    else:
        os.chmod(audit_copy, 0o600)

    final_record = _checkpoint_record(final_path, run_dir)
    stage_state = state["stages"]["final_review"]
    stage_state["status"] = "accepted"
    stage_state["accepted_checkpoint"] = final_record
    stage_state["reviewed_checkpoint"] = reviewed_checkpoint
    stage_state["audit"] = {
        "path": str(audit_copy.relative_to(run_dir)),
        "sha256": _sha256(audit_copy),
    }
    stage_state["attempts"].append(
        {
            "decision": "accepted",
            "at": _now(),
            "input_sha256": reviewed_checkpoint["sha256"],
            "audit_sha256": _sha256(audit_copy),
            "proof_manifest_sha256": audit["proof"]["manifest"]["sha256"],
            "notes": "Accepted after current proof and all ten image-specific review checks.",
        }
    )
    state["status"] = "complete"
    state["current_stage"] = None
    state["current_checkpoint"] = final_record
    state["final"] = final_record
    state["events"].append(
        {
            "type": "stage_completed",
            "stage": "final_review",
            "decision": "accepted",
            "at": _now(),
            "checkpoint_sha256": final_record["sha256"],
            "notes": "Canonical ten-item audit and visual proof revalidated.",
        }
    )
    state["events"].append(
        {
            "type": "run_completed",
            "stage": "final_review",
            "at": _now(),
            "final_sha256": final_record["sha256"],
        }
    )
    _write_json(run_dir / "run.json", state)
    return {
        "status": "complete",
        "final": str(final_path),
        "audit": str(audit_copy),
        "final_sha256": final_record["sha256"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain ordered checkpoints for a V6 portrait-retouch run."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("source", type=Path)
    init_parser.add_argument("--plan", type=Path, required=True)
    init_parser.add_argument("--run-dir", type=Path, required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--run-dir", type=Path, required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--run-dir", type=Path, required=True)
    record_parser.add_argument("--stage", choices=STAGE_ORDER[:-1], required=True)
    record_parser.add_argument("--decision", choices=sorted(NON_FINAL_DECISIONS), required=True)
    record_parser.add_argument("--input", type=Path)
    record_parser.add_argument("--candidate", type=Path)
    record_parser.add_argument("--notes", default="")

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-dir", type=Path, required=True)
    finalize_parser.add_argument("--audit", type=Path, required=True)

    reserve_parser = subparsers.add_parser("reserve-model-attempt")
    reserve_parser.add_argument("--run-dir", type=Path, required=True)
    reserve_parser.add_argument("--request", type=Path, required=True)
    reserve_parser.add_argument("--prompt", type=Path, required=True)
    reserve_parser.add_argument("--provider", default="")
    reserve_parser.add_argument("--model-name", default="")

    complete_parser = subparsers.add_parser("complete-model-attempt")
    complete_parser.add_argument("--run-dir", type=Path, required=True)
    complete_parser.add_argument("--candidate", type=Path, required=True)

    fail_parser = subparsers.add_parser("fail-model-attempt")
    fail_parser.add_argument("--run-dir", type=Path, required=True)
    fail_parser.add_argument("--reason", required=True)

    reject_parser = subparsers.add_parser("reject-model-attempt")
    reject_parser.add_argument("--run-dir", type=Path, required=True)
    reject_parser.add_argument("--notes", required=True)

    model_parser = subparsers.add_parser("accept-model-candidate")
    model_parser.add_argument("--run-dir", type=Path, required=True)
    model_parser.add_argument("--audit", type=Path, required=True)
    model_parser.add_argument("--notes", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = init_run(args.source, args.plan, args.run_dir)
        elif args.command == "validate":
            result = validate_run(args.run_dir)
        elif args.command == "record":
            result = record_stage(
                args.run_dir,
                args.stage,
                args.decision,
                input_path=args.input,
                candidate_path=args.candidate,
                notes=args.notes,
            )
        elif args.command == "reserve-model-attempt":
            result = reserve_model_attempt(
                args.run_dir,
                args.request,
                args.prompt,
                provider=args.provider,
                model_name=args.model_name,
            )
        elif args.command == "complete-model-attempt":
            result = complete_model_attempt(args.run_dir, args.candidate)
        elif args.command == "fail-model-attempt":
            result = fail_model_attempt(args.run_dir, args.reason)
        elif args.command == "reject-model-attempt":
            result = reject_model_attempt(args.run_dir, args.notes)
        elif args.command == "accept-model-candidate":
            result = record_model_candidate(
                args.run_dir,
                args.audit,
                notes=args.notes,
            )
        else:
            result = finalize(args.run_dir, args.audit)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (
        OSError,
        json.JSONDecodeError,
        PipelineStateError,
        check_geometry.GeometryError,
        input_preflight.PreflightError,
    ) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
