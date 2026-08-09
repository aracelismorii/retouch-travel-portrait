# Validation policy for v0.7.0-beta

This document defines the release gates for `retouch-travel-portrait`. Passing
these gates means the repository, deterministic operations, state transitions,
proof bindings, and failure handling behave as specified. It does **not** by
itself establish a 90% real-world aesthetic success rate or production
readiness.

## 1. Supported environment

CI must run the complete automated suite on Python 3.9, 3.10, 3.11, 3.12,
3.13, and 3.14. Core `retouch-travel-portrait/requirements.txt` intentionally
contains only Pillow and NumPy. Repository validation additionally requires
`PyYAML>=6,<7`, declared separately in the root-level
`requirements-validation.txt`; it must not be silently added to the core
runtime requirements. Every interpreter must pass repository validation and
the full `unittest` suite.

Python 3.9 remains in the matrix only while dependency resolution succeeds. A
future dependency release that drops it must trigger an explicit support-policy
change rather than a silent CI removal.

## 2. Input contract gates

Positive fixtures must cover single-frame, 8-bit `RGB` and `L` JPG/JPEG, PNG,
and WebP inputs; displayed short edge at least 768 pixels; matching extension
and encoding; and EXIF Orientation 1, 3, 6, and 8. EXIF normalization must not
modify the immutable source bytes or alter the displayed canvas.

Negative fixtures must reject:

- alpha channels or transparency metadata;
- animated or multi-frame sources;
- CMYK, palette, 16-bit, and other unsupported pixel modes;
- mirrored EXIF orientations 2, 4, 5, and 7;
- extension/encoding mismatches, unreadable files, invalid dimensions, and
  sources below the displayed 768-pixel short-edge minimum;
- malformed or incompatible ICC profiles and inputs above the pixel safety limit.

## 3. Deterministic workflow gates

The test suite must prove that the immutable source and plan hashes cannot be
silently replaced; the eight-stage order cannot be skipped or reordered; and
completed stages form a contiguous, validated checkpoint chain. Every stage
decision requires a non-empty, image-specific note.

An applied stage must create a pending candidate before review. Accepting it
must require a passing exact-canvas geometry gate. Rejecting it must retain the
previous accepted checkpoint. Inspection-only no-change stages must preserve
the checkpoint byte-for-byte. Run mutations must be atomic and protected by the
run lock on supported POSIX systems.

Tests must cover `natural`, `fresh`, and `warm-film`, including bounded and
monotonic parameter behavior, unchanged geometry, and exact no-op behavior for
zero controls. Modes may affect only light and color recipes.

## 4. Proof and audit gates

`render_proof.py` must create the canonical comparison, difference heatmap,
100% detail, 720-pixel thumbnail, and 1080-pixel phone preview. The 100% file
must contain both the upper-body skin-density primary focus and an upper-center
fallback, each shown as an original/candidate pair. This reduces single-focus
misses without claiming face detection. Its schema-v2 manifest must bind:

- immutable-original and current-candidate SHA-256 values;
- every canonical proof filename and file SHA-256;
- every required view to its canonical file and SHA-256.

`audit_result.py` must require the current proof manifest. Missing, stale,
renamed, path-escaping, or hash-modified proof files must fail validation.

All ten manual checks must be objects with a supported `status` and a
non-placeholder, image-specific `note`. On POSIX, create the per-run file with
`install -m 600 references/manual-review-template.json
<run-dir>/manual-review.json`; on other platforms, apply equivalent
current-user-only file permissions. After whitespace normalization, every
note must contain at least 16 characters and naturally identify visible
check-specific evidence: skin texture/pores/fine lines; face-versus-neck tone;
sclera/teeth visibility and color; lip color/saturation/edge; background
lines/geometry; hair edges/flyaways/halos/smearing; subject-versus-background
brightness; 100% and thumbnail coherence; original-versus-candidate change or
heatmap evidence; or phone-view brightness/highlights/shadows, respectively.
Generic verdicts and placeholder text must fail. A `not_applicable` note is
held to the same rule and must identify the relevant feature plus why it is not
visible. Reviewers must describe what the current proof actually shows, not
keyword-stuff a note to satisfy validation. Automatic metrics may hard-reject
but must never auto-accept a semantic check. Finalization must fail unless the
audit decision is `accept`, all ten checks resolve to `pass` or justified
`not_applicable`, and the reviewed pixels, audit, proof manifest, and proof
files still match their recorded hashes.

## 5. Model lifecycle gates

The optional model path must begin before any deterministic stage attempt and
must use this recorded lifecycle:

```text
reserve -> complete -> accept
                    -> reject
        -> fail
```

Reservation must bind the immutable-original hash, compiled request, prompt,
mode, stage order, ten required checks, provider label, and model label. A
candidate cannot be completed without a reservation and cannot be accepted
without a current proof-bound audit and image-specific note.

Success, failure, and rejection permanently consume the single recorded model
allowance. Tests must reject second reservations, retries, chained inputs,
request/prompt tampering, arbitrary unreserved candidates, geometry drift, and
stale audit/proof evidence.

This state machine can demonstrate compliance inside the recorded Skill run.
It cannot independently prove that no external service was called outside the
Skill, so release documentation and downstream integrations must not describe
it as service-level call attestation.

## 6. Batch queue gates

`batch_pipeline_v6.py prepare` must discover supported inputs recursively in a
stable relative-path order, assign collision-resistant run IDs, initialize one
independent run per source, and exclude the output subtree.

`resume` must preserve immutable queue configuration and isolate failures. Per
invocation it may create at most one pending deterministic candidate or one
proof set **per independent run**. It must never accept a candidate, invent a
visual decision or note, invoke a model, accept an audit, or finalize a run.

`status`/`refresh-status` must refresh classifications without creating or
advancing runs. Complete, safe-but-subtle, partial fallback, failed,
awaiting-review, and prepared outcomes must be counted separately. Queue and run
state writes must be atomic and safe to resume after interruption.

## 7. Privacy and release-package gates

On supported POSIX systems, run and queue directories must be private (`0700`)
and sensitive files must be private (`0600`). Tests and documentation must treat
the run directory as sensitive because it contains an immutable source copy and
may retain EXIF/GPS metadata, checkpoints, proofs, notes, and model artifacts.

The default deterministic path must not require a network or image-model call.
The optional model path may upload the immutable source to a third-party service
and must be documented as such.

The CI-built archive must be inspected before release. It must contain no raster
or vector image assets, run/output/work directories, generated checkpoints,
proofs, queue manifests, Python caches, VCS metadata, or leaked workstation
absolute paths. Archive member names must be relative and free of path traversal.

CI must extract the exact archive into a clean directory, rerun repository
validation there, and rerun the complete automated test suite from the extracted
copy. A source-tree-only test is not sufficient for a release artifact.

## 8. Local release verification

From the repository root, where the Skill is located at
`retouch-travel-portrait/`:

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

Do not publish on the basis of automated tests alone. Record real-image
evaluation separately by scene, lighting, pose, occlusion, mode, visual-gate
result, and failure reason. Do not insert an uncompleted sample result into this
policy or infer a 90% rate from historical or technically completed cases.

## 9. Completed focused real-image validation

The `v0.7.0-beta` release candidate completed one 12-image, `natural`-mode
forward validation on public single-woman portrait sources selected for the
requested East-Asian-focused test. Source pages label the subjects as women;
the Skill does not infer or certify age, ethnicity, identity, or consent.

The set covered soft studio and window light, moody low light, saturated red
light, black-and-white, outdoor blossoms and architecture, traditional dress,
vivid bokeh, a full-body subject, and hard low-key lighting. Results were:

- 12/12 inputs passed the public-beta preflight;
- 12/12 runs reached `complete` and 12/12 proof-bound ten-item audits returned
  `accept`;
- 12/12 final encoded canvases matched their displayed originals exactly;
- 0 image-model calls were recorded;
- 36 deterministic candidate review records comprised 33
  `accepted_after_visual_review`, 2 exact `no_pixel_change` fallbacks, and 1
  `rejected_and_rolled_back` skin candidate;
- the rejected case was an outdoor blossom portrait whose amplified
  difference view showed the color-based skin mask touching pink background
  blossoms; the prior light/color checkpoint was retained and the run later
  passed final review;
- the black-and-white case produced exact no-pixel-change color and skin
  candidates, which were recorded as safe no-change rather than credited as
  applied beautification;
- the focused candidate acceptance count was 33/36 (91.7%), while workflow
  completion was 12/12.

These figures describe this small, deliberately diverse, reviewed sample only.
They are not a blinded aesthetic study, do not measure demographic fairness,
do not establish a general 90% real-world success rate, and do not authorize
unattended production use. Perceptible improvement was not independently
scored; the default natural mode intentionally favors subtle or zero change
over forcing a visible effect.

No source portrait, run directory, proof image, queue manifest, or retained
metadata is included in the repository or release archive. Public source pages
used for traceability are:

- [case 01](https://www.pexels.com/photo/portrait-of-woman-18108800/)
- [case 02](https://www.pexels.com/photo/close-up-photo-of-a-beautiful-woman-8062968/)
- [case 03](https://www.pexels.com/photo/a-portrait-of-a-pretty-woman-5974428/)
- [case 04](https://www.pexels.com/photo/portrait-of-woman-16746305/)
- [case 05](https://www.pexels.com/photo/portrait-of-woman-in-red-light-19060188/)
- [case 06](https://www.pexels.com/photo/portrait-of-a-beautiful-brunette-18553609/)
- [case 07](https://www.pexels.com/photo/elegant-black-and-white-portrait-of-a-woman-31058360/)
- [case 08](https://www.pexels.com/photo/portrait-of-woman-20393524/)
- [case 09](https://www.pexels.com/photo/portrait-of-a-woman-10917011/)
- [case 10](https://www.pexels.com/photo/portrait-of-woman-19231326/)
- [case 11](https://www.pexels.com/photo/elegant-woman-in-traditional-japanese-kimono-outdoors-33157580/)
- [case 12](https://www.pexels.com/photo/portrait-of-woman-looking-up-18129955/)
