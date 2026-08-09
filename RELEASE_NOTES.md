# Release notes

## v0.7.0-beta

This public beta packages `retouch-travel-portrait` as a review-gated Codex
Skill for restrained, natural-looking travel-portrait finishing. It favors
traceable decisions, immutable inputs, and natural texture over automatic or
aggressive beauty filtering.

### Included

- A fixed eight-stage V6 workflow covering photo inspection, whole-image light
  and color, texture-preserving skin cleanup, conservative semantic inspection,
  and final review.
- Three bounded looks: `natural`, `fresh`, and `warm-film`. Look selection
  changes only the light and color stages.
- A strict public-beta input preflight for single-frame, 8-bit RGB or grayscale
  JPG/JPEG, PNG, and WebP sources with a displayed short edge of at least 768
  pixels and EXIF Orientation 1, 3, 6, or 8.
- A default local deterministic path with zero image-model calls, immutable
  source provenance, ordered checkpoints, pending-candidate review, explicit
  accept/reject decisions, atomic writes, and rollback-safe no-change handling.
- A recursive V6 batch queue with `prepare`, `resume`, and `status` operations.
  One resume may create at most one pending candidate or proof set per
  independent run; it never auto-accepts, invents review notes, invokes a model,
  or finalizes an image.
- A schema-v2 proof manifest binding the original, current candidate, five
  canonical proof files, all required views, and their SHA-256 values. The 100%
  proof now includes a skin-density primary crop plus an upper-center fallback
  original/candidate pair so one misplaced focus cannot silently stand in for
  facial-detail review.
- Ten mandatory manual judgments, each requiring a supported status and a
  non-placeholder, image-specific note that names check-specific visible
  evidence before finalization can succeed.
- An optional one-candidate model lifecycle: reserve before the external call,
  complete the response, then fail, reject, or accept. Success, failure, and
  rejection permanently consume the run's single recorded allowance.
- Private POSIX permissions for run directories and sensitive files, repository
  validation, archive-content validation, and extracted-archive retesting.
- Candidate directories are force-corrected to `0700`; candidate images and
  atomic review manifests are force-corrected to `0600`, including when the
  caller has an open umask or a review file is rewritten after acceptance.
- Batch `status` preserves the precise preflight failure for an unchanged input
  that could not be prepared, while a changed source is retried as a new state.
- A separate root-level `requirements-validation.txt` containing
  `PyYAML>=6,<7`. Core runtime dependencies remain limited to Pillow and NumPy;
  PyYAML is required only by `scripts/validate_repo.py` and release CI.

### Verification status

- The release is protected by 100+ automated tests covering input rejection,
  EXIF display geometry, deterministic parameters, stage-state integrity,
  proof/audit binding, model-call lifecycle, and independent batch queues.
- CI validates and tests Python 3.9 through 3.14, builds a release archive,
  rejects sensitive or generated content in that archive, extracts the exact
  artifact, and reruns repository validation plus the full test suite from the
  extracted copy.
- Repository validation checks required release files, Skill frontmatter and
  metadata, all JSON documents, Python syntax, package paths, caches, generated
  run evidence, image assets, and leaked workstation absolute paths.
- A completed 12-image `natural`-mode forward validation reached 12/12
  proof-bound workflow completions with zero model calls and exact final canvas
  preservation. Of 36 deterministic candidates, 33 were visually accepted, 2
  exact no-pixel-change results became safe no-change, and 1 nonlocalized skin
  candidate was rejected and rolled back. These are focused sample results,
  not a general success-rate claim.

### Beta limitations

- Every pixel-changing candidate and final ten-item audit requires a human or
  vision-capable Agent. This is not an unattended high-volume retouching
  service, and batch status is not an automatic quality decision.
- The release does not claim a 90% real-world aesthetic success rate. Automated
  completion, geometry, and hash checks are not substitutes for identity,
  naturalness, skin-tone, or scene-cohesion review.
- The completed twelve-image study is small, reviewed rather than blinded, and
  does not independently score perceptible improvement, demographic fairness,
  or unattended performance. Its scene, mode, decisions, rollback, and sample
  limitations are documented in `VALIDATION.md`.
- The model state machine enforces the single-call rule inside the recorded
  Skill run, but it cannot independently attest that an operator did not bypass
  the Skill and contact an external service elsewhere.
- The optional model path may upload the immutable original to a third-party
  provider. Consent, provider retention, and regional data-handling obligations
  remain the integrator's responsibility.
- Run directories contain sensitive source material and may preserve EXIF/GPS
  metadata, checkpoints, proof images, review notes, and model artifacts. They
  must not be committed or included in a release archive.
- Face/body reshaping, face swaps, age changes, heavy makeup, background
  replacement, children, multiple primary faces, transparent/animated/CMYK
  inputs, automated identity authentication, and automatic acceptance remain
  out of scope.

### Local verification

From the repository root:

```bash
python3 -m pip install \
  -r retouch-travel-portrait/requirements.txt \
  -r requirements-validation.txt
python3 scripts/validate_repo.py .
python3 -m unittest discover \
  -s retouch-travel-portrait/tests \
  -p "test_*.py" \
  -v
```

Integrations should retain each private run directory until review is complete,
surface every awaiting-review or failed item, and never equate process
completion with an automatically authenticated identity or guaranteed aesthetic
result.
