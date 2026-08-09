#!/usr/bin/env python3
"""Prepare and resume independent, review-gated V6 folder runs.

The queue may generate at most one deterministic pending candidate per image and
may render final proof views. It never accepts a candidate, supplies a visual
decision, or calls an image model, so one portrait's judgment cannot be copied
to another portrait.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import compile_pipeline
import execute_pipeline
import pipeline_state
import render_proof


SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
CLASSIFICATIONS = (
    "complete",
    "safe_but_subtle",
    "partial_v3_fallback",
    "failed",
    "awaiting_review",
    "prepared",
)
MANIFEST_NAME = "queue.json"
PLAN_NAME = "queue-plan.json"
QUEUE_SCHEMA_VERSION = 1


class BatchPipelineError(ValueError):
    """Raised when a V6 queue cannot be prepared or refreshed safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(data: Any) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Durably replace a JSON file without exposing a partial document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(data)
    if path.is_file() and path.read_bytes() == payload:
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path, label: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BatchPipelineError(f"{label} must contain a JSON object: {path}")
    return data


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_images(input_dir: Path, exclude: Iterable[Path] = ()) -> list[Path]:
    """Recursively discover supported images in stable relative-path order."""

    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise BatchPipelineError(f"input folder does not exist: {input_dir}")
    excluded = tuple(path.resolve() for path in exclude)
    discovered: list[Path] = []
    for path in input_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        resolved = path.resolve()
        if any(_is_within(resolved, blocked) for blocked in excluded):
            continue
        discovered.append(path)
    return sorted(
        discovered,
        key=lambda path: (
            path.relative_to(input_dir).as_posix().casefold(),
            path.relative_to(input_dir).as_posix(),
        ),
    )


def stable_run_id(relative_path: Path | str) -> str:
    """Return a readable path slug plus a 128-bit path-derived collision guard."""

    relative = Path(relative_path).as_posix()
    if relative in {"", "."} or relative.startswith("/"):
        raise BatchPipelineError("run ids require a non-empty relative source path")
    without_suffix = str(Path(relative).with_suffix(""))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", without_suffix).strip("-._")
    slug = slug[:72].rstrip("-._") or "portrait"
    path_digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]
    return f"{slug}--{path_digest}"


def _canonical_overrides(overrides: dict[str, float] | None) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in sorted((overrides or {}).items(), key=lambda item: item[0])
    }


def _compile_plan(mode: str, overrides: dict[str, float]) -> dict[str, Any]:
    pipeline = compile_pipeline.load_schema()
    parameters, audit = compile_pipeline.load_linked_schemas(pipeline)
    return compile_pipeline.compile_pipeline(
        pipeline,
        parameters,
        audit,
        mode=mode,
        overrides=overrides,
    )


def _plan_sha256(plan: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(plan)).hexdigest()


def _validate_queue_manifest(manifest: dict[str, Any], path: Path) -> None:
    if manifest.get("queue_schema_version") != QUEUE_SCHEMA_VERSION:
        raise BatchPipelineError(f"unsupported queue manifest schema: {path}")
    if manifest.get("pipeline_schema_version") != 6:
        raise BatchPipelineError(f"queue is not a V6 pipeline: {path}")
    if not isinstance(manifest.get("items"), list):
        raise BatchPipelineError(f"queue manifest items must be a list: {path}")
    observed_ids: set[str] = set()
    observed_paths: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise BatchPipelineError(f"queue manifest contains a non-object item: {path}")
        item_id = item.get("id")
        relative_path = item.get("relative_path")
        if not isinstance(item_id, str) or not isinstance(relative_path, str):
            raise BatchPipelineError(f"queue item is missing id or relative_path: {path}")
        if item_id in observed_ids or relative_path in observed_paths:
            raise BatchPipelineError(f"queue manifest contains duplicate items: {path}")
        observed_ids.add(item_id)
        observed_paths.add(relative_path)


def _load_manifest(output_dir: Path) -> dict[str, Any] | None:
    manifest_path = output_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    manifest = _read_json(manifest_path, "queue manifest")
    _validate_queue_manifest(manifest, manifest_path)
    return manifest


def _configuration_from_existing(
    manifest: dict[str, Any],
    requested_mode: str | None,
    requested_overrides: dict[str, float] | None,
) -> tuple[str, dict[str, float]]:
    existing_mode = manifest.get("mode")
    existing_overrides = manifest.get("overrides")
    if not isinstance(existing_mode, str) or not isinstance(existing_overrides, dict):
        raise BatchPipelineError("queue manifest is missing its immutable configuration")
    mode = requested_mode or existing_mode
    overrides = (
        _canonical_overrides(requested_overrides)
        if requested_overrides is not None
        else _canonical_overrides(existing_overrides)
    )
    if mode != existing_mode or overrides != _canonical_overrides(existing_overrides):
        raise BatchPipelineError(
            "mode or --set overrides differ from the existing queue; use a new "
            "output folder so reviewed decisions cannot be reused under a new plan"
        )
    return mode, overrides


def _validate_queue_location(
    input_dir: Path, output_dir: Path, manifest: dict[str, Any] | None
) -> None:
    if input_dir == output_dir:
        raise BatchPipelineError("input and output folders must be different")
    if output_dir.exists() and not output_dir.is_dir():
        raise BatchPipelineError(f"output path is not a folder: {output_dir}")
    if manifest is not None:
        recorded_input = manifest.get("input_dir")
        recorded_output = manifest.get("output_dir")
        if recorded_input != str(input_dir) or recorded_output != str(output_dir):
            raise BatchPipelineError(
                "queue paths differ from the paths recorded at preparation time"
            )


def _initialize_run_atomically(source: Path, plan_path: Path, run_dir: Path) -> None:
    if run_dir.exists():
        raise BatchPipelineError(f"refusing to replace existing run directory: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir.parent, 0o700)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{run_dir.name}.prepare-", dir=run_dir.parent)
    )
    try:
        pipeline_state.init_run(source, plan_path, temporary)
        os.replace(temporary, run_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _confined_file(run_dir: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise BatchPipelineError(f"run state is missing {label}.path")
    candidate = (run_dir / raw_path).resolve()
    if not _is_within(candidate, run_dir):
        raise BatchPipelineError(f"run state {label} escapes its run directory")
    if not candidate.is_file():
        raise BatchPipelineError(f"run state {label} file is missing")
    return candidate


def _pending_visual_candidate(run_dir: Path, current_stage: Any) -> bool:
    if not isinstance(current_stage, str):
        return False
    candidates = run_dir / "candidates"
    if not candidates.is_dir():
        return False
    for review_path in candidates.glob("*.review.json"):
        try:
            review = _read_json(review_path, "candidate review")
        except (OSError, json.JSONDecodeError, BatchPipelineError):
            continue
        if (
            review.get("stage") == current_stage
            and review.get("status") == "awaiting_visual_review"
        ):
            return True
    return False


def _proof_manifest_is_current(
    proof_dir: Path, original_hash: str, candidate_hash: str
) -> bool:
    manifest_path = proof_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = _read_json(manifest_path, "proof manifest")
    except (OSError, json.JSONDecodeError, BatchPipelineError):
        return False
    if (
        manifest.get("schema_version") != render_proof.PROOF_SCHEMA_VERSION
        or manifest.get("generator") != render_proof.PROOF_GENERATOR
        or
        manifest.get("original_sha256") != original_hash
        or manifest.get("candidate_sha256") != candidate_hash
    ):
        return False
    files = manifest.get("files")
    hashes = manifest.get("file_sha256")
    views = manifest.get("views")
    view_keys = manifest.get("view_file_keys")
    view_hashes = manifest.get("view_sha256")
    if not all(isinstance(value, dict) for value in (files, hashes, views, view_keys, view_hashes)):
        return False
    resolved_files: dict[str, Path] = {}
    for key, expected_name in render_proof.OUTPUT_NAMES.items():
        raw_path = files.get(key)
        expected_hash = hashes.get(key)
        if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
            return False
        selected = Path(raw_path)
        selected = selected.resolve() if selected.is_absolute() else (proof_dir / selected).resolve()
        if not _is_within(selected, proof_dir.resolve()) or selected.name != expected_name:
            return False
        if not selected.is_file() or sha256_file(selected) != expected_hash:
            return False
        resolved_files[key] = selected
    for view, file_key in render_proof.VIEW_FILE_KEYS.items():
        raw_path = views.get(view)
        if view_keys.get(view) != file_key or not isinstance(raw_path, str):
            return False
        selected = Path(raw_path)
        selected = selected.resolve() if selected.is_absolute() else (proof_dir / selected).resolve()
        if selected != resolved_files[file_key] or view_hashes.get(view) != hashes[file_key]:
            return False
    return True


def _render_proofs_atomically(
    run_dir: Path, original: Path, candidate: Path
) -> dict[str, Any]:
    proof_dir = run_dir / "proof"
    original_hash = sha256_file(original)
    candidate_hash = sha256_file(candidate)
    if proof_dir.exists():
        if _proof_manifest_is_current(proof_dir, original_hash, candidate_hash):
            return {
                "resume_action": "proof_already_current",
                "proof_dir": str(proof_dir),
                "proof_manifest": str(proof_dir / "manifest.json"),
            }
        raise BatchPipelineError(
            "proof directory exists but is incomplete or belongs to another checkpoint"
        )
    temporary = Path(
        tempfile.mkdtemp(prefix=".proof.prepare-", dir=run_dir)
    )
    try:
        proof_manifest = render_proof.render_proofs(original, candidate, temporary)
        proof_manifest["original_sha256"] = original_hash
        proof_manifest["candidate_sha256"] = candidate_hash
        # Paths produced inside the temporary directory must name the final proof
        # directory before the directory is atomically published.
        proof_manifest["files"] = {
            key: str(proof_dir / Path(raw_path).name)
            for key, raw_path in proof_manifest["files"].items()
        }
        proof_manifest["views"] = {
            key: str(proof_dir / Path(raw_path).name)
            for key, raw_path in proof_manifest["views"].items()
        }
        _atomic_write_json(temporary / "manifest.json", proof_manifest)
        os.replace(temporary, proof_dir)
        os.chmod(proof_dir, 0o700)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "resume_action": "proof_generated",
        "proof_dir": str(proof_dir),
        "proof_manifest": str(proof_dir / "manifest.json"),
    }


def _prepare_next_review_artifact(
    run_dir: Path, state: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Generate at most one candidate or proof set, never a review decision."""

    if state.get("status") != "in_progress":
        return {"resume_action": "run_not_in_progress"}
    current_stage = state.get("current_stage")
    if current_stage == "final_review":
        original = _confined_file(run_dir, state["original"].get("path"), "original")
        current = _confined_file(
            run_dir, state["current_checkpoint"].get("path"), "current_checkpoint"
        )
        return _render_proofs_atomically(run_dir, original, current)
    stage_plan = next(
        (
            stage
            for stage in plan.get("stages", [])
            if isinstance(stage, dict) and stage.get("id") == current_stage
        ),
        None,
    )
    if not isinstance(stage_plan, dict):
        raise BatchPipelineError(f"current stage is absent from the plan: {current_stage}")
    if current_stage not in execute_pipeline.DETERMINISTIC_STAGES:
        return {"resume_action": "awaiting_manual_inspection"}
    if stage_plan.get("action") != "apply":
        return {"resume_action": "awaiting_manual_inspection"}
    if _pending_visual_candidate(run_dir, current_stage):
        return {"resume_action": "candidate_already_awaiting_visual_review"}
    result = execute_pipeline.execute_stage(run_dir, str(current_stage))
    return {
        "resume_action": (
            "candidate_generated"
            if result.get("status") == "awaiting_visual_review"
            else "candidate_resolved_without_visual_acceptance"
        ),
        "resume_result": result,
    }


def _explicit_classification(state: dict[str, Any]) -> str | None:
    for key in ("delivery_classification", "classification"):
        value = state.get(key)
        if value in CLASSIFICATIONS:
            return str(value)
    if state.get("status") in {"partial_v3_fallback", "safe_but_subtle"}:
        return str(state["status"])
    events = state.get("events")
    if isinstance(events, list) and any(
        isinstance(event, dict)
        and event.get("type") in {"v3_fallback_delivered", "partial_v3_fallback"}
        for event in events
    ):
        return "partial_v3_fallback"
    return None


def _read_and_validate_run(
    run_dir: Path,
    expected_source_hash: str,
    expected_mode: str,
    expected_plan_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_path = run_dir / "run.json"
    plan_path = run_dir / "plan.json"
    if not run_path.is_file() or not plan_path.is_file():
        raise BatchPipelineError("run directory is incomplete (run.json or plan.json missing)")
    state = _read_json(run_path, "run state")
    plan = _read_json(plan_path, "run plan")
    try:
        pipeline_state.validate_run(run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BatchPipelineError(f"run state invariant failed: {exc}") from exc
    if state.get("schema_version") != 6 or plan.get("schema_version") != 6:
        raise BatchPipelineError("run is not a V6 run")
    if plan.get("mode") != expected_mode or state.get("mode") != expected_mode:
        raise BatchPipelineError("run mode differs from the queue mode")
    actual_plan_hash = sha256_file(plan_path)
    if state.get("plan_sha256") != actual_plan_hash:
        raise BatchPipelineError("run plan hash changed after initialization")
    if actual_plan_hash != expected_plan_hash:
        raise BatchPipelineError("run plan differs from the immutable queue plan")
    if state.get("source_sha256") != expected_source_hash:
        raise BatchPipelineError("run source hash differs from the queue source hash")
    original_record = state.get("original")
    if not isinstance(original_record, dict):
        raise BatchPipelineError("run state is missing the immutable original")
    original = _confined_file(run_dir, original_record.get("path"), "original")
    if original_record.get("sha256") != expected_source_hash:
        raise BatchPipelineError("immutable original record has the wrong hash")
    if sha256_file(original) != expected_source_hash:
        raise BatchPipelineError("immutable original was modified after initialization")
    stages = state.get("stages")
    if not isinstance(stages, dict) or list(stages) != pipeline_state.STAGE_ORDER:
        raise BatchPipelineError("run state does not contain the ordered V6 stages")
    return state, plan


def _validate_completed_run(run_dir: Path, state: dict[str, Any]) -> tuple[Path, str]:
    stages = state["stages"]
    if stages["final_review"].get("status") != "accepted":
        raise BatchPipelineError("complete run has no accepted final review")
    if any(stage.get("status") == "pending" for stage in stages.values()):
        raise BatchPipelineError("complete run still contains pending stages")
    final_record = state.get("final")
    if not isinstance(final_record, dict):
        raise BatchPipelineError("complete run is missing its final record")
    final_path = _confined_file(run_dir, final_record.get("path"), "final")
    final_hash = sha256_file(final_path)
    if final_record.get("sha256") != final_hash:
        raise BatchPipelineError("complete run final hash does not match its record")
    audit_record = stages["final_review"].get("audit")
    if not isinstance(audit_record, dict):
        raise BatchPipelineError("complete run is missing its audit binding")
    audit_path = _confined_file(run_dir, audit_record.get("path"), "audit")
    if audit_record.get("sha256") != sha256_file(audit_path):
        raise BatchPipelineError("complete run audit hash does not match its record")
    audit = _read_json(audit_path, "final audit")
    summary = audit.get("summary", {})
    decision = (
        summary.get("final_decision", summary.get("decision", audit.get("decision")))
        if isinstance(summary, dict)
        else audit.get("decision")
    )
    if decision != "accept":
        raise BatchPipelineError("complete run audit is not accepted")
    if audit.get("inputs", {}).get("candidate", {}).get("sha256") != final_hash:
        raise BatchPipelineError("complete run audit does not match the final output")
    return final_path, final_hash


def _classification_for_run(
    run_dir: Path,
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    explicit = _explicit_classification(state)
    status = state.get("status")
    details: dict[str, Any] = {
        "run_status": status,
        "current_stage": state.get("current_stage"),
        "model_calls_recorded": int(state.get("model", {}).get("calls_recorded", 0))
        if isinstance(state.get("model"), dict)
        else 0,
    }
    model = state.get("model")
    if isinstance(model, dict):
        details["model_status"] = model.get("status")
    if explicit == "partial_v3_fallback":
        return explicit, details
    if status in {"failed", "error", "rolled_back"}:
        return "failed", details
    if status == "complete":
        final_path, final_hash = _validate_completed_run(run_dir, state)
        details["final"] = str(final_path)
        details["final_sha256"] = final_hash
        if explicit in {"complete", "safe_but_subtle"}:
            return explicit, details
        stages = state["stages"]
        substantive = any(
            stages[stage_id].get("status")
            in {"accepted", "model_candidate_accepted"}
            for stage_id in pipeline_state.STAGE_ORDER[:-1]
        )
        changed = final_hash != state["original"]["sha256"]
        return ("complete" if substantive and changed else "safe_but_subtle"), details
    if status != "in_progress":
        raise BatchPipelineError(f"unsupported run status: {status!r}")
    stages = state["stages"]
    any_attempt = any(bool(stage.get("attempts")) for stage in stages.values())
    progressed = any(stage.get("status") != "pending" for stage in stages.values())
    pending_candidate = _pending_visual_candidate(run_dir, state.get("current_stage"))
    details["pending_visual_candidate"] = pending_candidate
    model_awaiting = isinstance(model, dict) and model.get("status") in {
        "reserved",
        "awaiting_review",
    }
    if (
        explicit == "awaiting_review"
        or progressed
        or any_attempt
        or pending_candidate
        or model_awaiting
    ):
        return "awaiting_review", details
    return "prepared", details


def _unchanged_unprepared_failure(
    previous: dict[str, Any] | None, observed_hash: str
) -> tuple[str, str] | None:
    """Return a prior initialization failure for the same unchanged source.

    A failed input preflight intentionally leaves no run directory. A read-only
    status refresh must not replace that precise diagnostic with the generic
    message used for a newly discovered, never-prepared source. The absence of
    ``prepared_at`` distinguishes an initialization failure from a run that was
    prepared successfully and later removed or corrupted.
    """

    if not isinstance(previous, dict) or "prepared_at" in previous:
        return None
    classification = previous.get("classification")
    error = previous.get("error")
    if classification != "failed" or not isinstance(error, str) or not error:
        return None
    if (
        previous.get("source_sha256") != observed_hash
        or previous.get("observed_source_sha256") != observed_hash
    ):
        return None
    return classification, error


def _item_for_source(
    source: Path,
    input_dir: Path,
    output_dir: Path,
    mode: str,
    plan_hash: str,
    plan_path: Path,
    previous: dict[str, Any] | None,
    initialize_missing: bool,
    prepare_review_artifact: bool,
) -> dict[str, Any]:
    relative = source.relative_to(input_dir).as_posix()
    item_id = stable_run_id(relative)
    run_dir = output_dir / "runs" / item_id
    item: dict[str, Any] = {
        "id": item_id,
        "relative_path": relative,
        "source_path": str(source),
        "source_sha256": None,
        "observed_source_sha256": None,
        "run_dir": str(run_dir),
        "classification": "failed",
    }
    if previous and isinstance(previous.get("prepared_at"), str):
        item["prepared_at"] = previous["prepared_at"]
    try:
        observed_hash = sha256_file(source)
        item["source_sha256"] = observed_hash
        item["observed_source_sha256"] = observed_hash
        if not run_dir.exists():
            if not initialize_missing:
                prior_failure = _unchanged_unprepared_failure(previous, observed_hash)
                if prior_failure is not None:
                    item["classification"], item["error"] = prior_failure
                    return item
                raise BatchPipelineError("run has not been prepared")
            _initialize_run_atomically(source, plan_path, run_dir)
            item["prepared_at"] = _now()
        state_path = run_dir / "run.json"
        if not state_path.is_file():
            raise BatchPipelineError("existing run directory is incomplete")
        raw_state = _read_json(state_path, "run state")
        initialized_hash = raw_state.get("source_sha256")
        if not isinstance(initialized_hash, str):
            raise BatchPipelineError("run state is missing its source hash")
        item["source_sha256"] = initialized_hash
        if initialized_hash != observed_hash:
            raise BatchPipelineError(
                "source content changed after preparation; create a new queue instead "
                "of reusing this portrait's review state"
            )
        state, run_plan = _read_and_validate_run(
            run_dir, initialized_hash, mode, plan_hash
        )
        if prepare_review_artifact:
            item.update(_prepare_next_review_artifact(run_dir, state, run_plan))
            # Candidate generation may safely record a mechanical rejection or
            # a zero-pixel no-change. Reload instead of assuming its outcome.
            state, run_plan = _read_and_validate_run(
                run_dir, initialized_hash, mode, plan_hash
            )
        classification, details = _classification_for_run(run_dir, state)
        item.update(details)
        item["classification"] = classification
        item.pop("error", None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        item["classification"] = "failed"
        item["error"] = str(exc)
    return item


def _missing_source_item(
    previous: dict[str, Any], input_dir: Path, output_dir: Path
) -> dict[str, Any]:
    item = dict(previous)
    relative = str(item["relative_path"])
    item["source_path"] = str(input_dir / relative)
    item["run_dir"] = str(output_dir / "runs" / str(item["id"]))
    item["observed_source_sha256"] = None
    item["classification"] = "failed"
    item["error"] = "source file is missing from the input folder"
    return item


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {classification: 0 for classification in CLASSIFICATIONS}
    for item in items:
        classification = item.get("classification")
        if classification not in counts:
            classification = "failed"
        counts[str(classification)] += 1
    return {"total": len(items), **counts}


def _base_manifest(
    input_dir: Path,
    output_dir: Path,
    mode: str,
    overrides: dict[str, float],
    plan_hash: str,
    previous: dict[str, Any] | None,
    phase: str,
) -> dict[str, Any]:
    created_at = previous.get("created_at") if previous else None
    return {
        "queue_schema_version": QUEUE_SCHEMA_VERSION,
        "pipeline_schema_version": 6,
        "pipeline_id": "travel-portrait-v6",
        "phase": phase,
        "created_at": created_at or _now(),
        "updated_at": _now(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "mode": mode,
        "overrides": overrides,
        "plan": PLAN_NAME,
        "plan_sha256": plan_hash,
        "policy": {
            "recursive_discovery": True,
            "auto_accept_visual_gates": False,
            "model_calls_by_queue_runner": 0,
            "reuse_review_decisions": False,
            "per_image_failure_isolation": True,
        },
        "counts": {"total": 0, **{name: 0 for name in CLASSIFICATIONS}},
        "items": [],
    }


def _reconcile_queue(
    input_dir: Path,
    output_dir: Path,
    *,
    phase: str,
    mode: str | None,
    overrides: dict[str, float] | None,
    require_existing: bool,
    initialize_missing: bool,
    prepare_review_artifacts: bool,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if not input_dir.is_dir():
        raise BatchPipelineError(f"input folder does not exist: {input_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise BatchPipelineError(f"output path is not a folder: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    previous = _load_manifest(output_dir)
    if require_existing and previous is None:
        raise BatchPipelineError(f"queue manifest does not exist: {output_dir / MANIFEST_NAME}")
    _validate_queue_location(input_dir, output_dir, previous)

    if previous is None:
        selected_mode = mode or "natural"
        selected_overrides = _canonical_overrides(overrides)
    else:
        selected_mode, selected_overrides = _configuration_from_existing(
            previous, mode, overrides
        )
    plan = _compile_plan(selected_mode, selected_overrides)
    plan_hash = _plan_sha256(plan)
    if previous is not None and previous.get("plan_sha256") != plan_hash:
        raise BatchPipelineError(
            "the currently compiled V6 plan differs from this queue's immutable plan; "
            "use a new output folder for the updated skill"
        )

    plan_path = output_dir / PLAN_NAME
    _atomic_write_json(plan_path, plan)
    if sha256_file(plan_path) != plan_hash:
        raise BatchPipelineError("queue plan could not be written reproducibly")

    excluded = [output_dir] if _is_within(output_dir, input_dir) else []
    sources = discover_images(input_dir, exclude=excluded)
    prior_by_path = {
        str(item["relative_path"]): item
        for item in (previous or {}).get("items", [])
    }
    if not sources and not prior_by_path:
        raise BatchPipelineError("input folder contains no supported images")

    manifest = _base_manifest(
        input_dir,
        output_dir,
        selected_mode,
        selected_overrides,
        plan_hash,
        previous,
        phase,
    )
    manifest_path = output_dir / MANIFEST_NAME
    source_paths: set[str] = set()
    ids: set[str] = set()
    for source in sources:
        relative = source.relative_to(input_dir).as_posix()
        source_paths.add(relative)
        item = _item_for_source(
            source,
            input_dir,
            output_dir,
            selected_mode,
            plan_hash,
            plan_path,
            prior_by_path.get(relative),
            initialize_missing,
            prepare_review_artifacts,
        )
        if item["id"] in ids:
            raise BatchPipelineError(f"stable run id collision: {item['id']}")
        ids.add(str(item["id"]))
        manifest["items"].append(item)
        manifest["counts"] = _counts(manifest["items"])
        manifest["updated_at"] = _now()
        _atomic_write_json(manifest_path, manifest)

    for relative in sorted(
        set(prior_by_path) - source_paths, key=lambda value: (value.casefold(), value)
    ):
        item = _missing_source_item(prior_by_path[relative], input_dir, output_dir)
        if item["id"] in ids:
            raise BatchPipelineError(f"stable run id collision: {item['id']}")
        ids.add(str(item["id"]))
        manifest["items"].append(item)

    manifest["counts"] = _counts(manifest["items"])
    manifest["updated_at"] = _now()
    _atomic_write_json(manifest_path, manifest)
    return manifest


def prepare_queue(
    input_dir: Path,
    output_dir: Path,
    *,
    mode: str = "natural",
    overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Create an idempotent queue and initialize every currently missing run."""

    return _reconcile_queue(
        input_dir,
        output_dir,
        phase="prepare",
        mode=mode,
        overrides=overrides,
        require_existing=False,
        initialize_missing=True,
        prepare_review_artifacts=False,
    )


def resume_queue(
    input_dir: Path,
    output_dir: Path,
    *,
    mode: str | None = None,
    overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Add missing runs and refresh existing runs without advancing a stage."""

    return _reconcile_queue(
        input_dir,
        output_dir,
        phase="resume",
        mode=mode,
        overrides=overrides,
        require_existing=True,
        initialize_missing=True,
        prepare_review_artifacts=True,
    )


def refresh_status(
    input_dir: Path,
    output_dir: Path,
    *,
    mode: str | None = None,
    overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Atomically refresh queue classifications without creating or advancing runs."""

    return _reconcile_queue(
        input_dir,
        output_dir,
        phase="status",
        mode=mode,
        overrides=overrides,
        require_existing=True,
        initialize_missing=False,
        prepare_review_artifacts=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, resume, or refresh a review-gated V6 portrait folder queue. "
            "This command never accepts visual gates and never calls an image model."
        )
    )
    parser.add_argument("phase", choices=("prepare", "resume", "status", "refresh-status"))
    parser.add_argument("input_folder", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--mode",
        choices=("natural", "fresh", "warm-film"),
        help="Queue mode. Prepare defaults to natural; resume/status inherit it.",
    )
    parser.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=None,
        metavar="STAGE.PARAM=VALUE",
        help="Override one bounded V6 parameter. Repeat as needed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        overrides = (
            compile_pipeline.parse_assignments(args.assignments)
            if args.assignments is not None
            else None
        )
        if args.phase == "prepare":
            result = prepare_queue(
                args.input_folder,
                args.output_dir,
                mode=args.mode or "natural",
                overrides=overrides,
            )
        elif args.phase == "resume":
            result = resume_queue(
                args.input_folder,
                args.output_dir,
                mode=args.mode,
                overrides=overrides,
            )
        else:
            result = refresh_status(
                args.input_folder,
                args.output_dir,
                mode=args.mode,
                overrides=overrides,
            )
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (
        BatchPipelineError,
        compile_pipeline.PipelineConfigError,
        pipeline_state.PipelineStateError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
