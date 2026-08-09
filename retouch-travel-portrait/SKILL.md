---
name: retouch-travel-portrait
description: Run an ordered, review-gated portrait-retouch workflow for one adult's travel portrait or a folder of independent portraits. Use for bounded whole-photo light and color balancing, texture-preserving skin cleanup, conservative semantic inspection or one optional isolated model candidate, natural/fresh/warm-film styling, proof rendering, and a mandatory ten-item visual audit. Do not use for face swaps, age changes, face or body reshaping, heavy makeup, background replacement, children, multiple primary faces, transparent or animated images, or unattended auto-acceptance.
---

# Retouch Travel Portrait V6 — v0.7.0-beta

Treat each image as an ordered, reviewable photo-finishing run. Preserve an
immutable original, complete all stages in order, record every safe no-op, and
deliver only after the proof manifest and all ten visual checks are verified.

## Read before running

- Read `references/pipeline-v6.json` for stage order and model policy.
- Read `references/model-parameters.json` before choosing a mode or override.
- Read `references/stage-contracts.md` before editing a stage.
- Read `references/quality-gates.md` and `references/audit-gates.json` before review.
- Use `references/manual-review-template.json` for the ten judgments.
- Read `references/model-prompts.md` only for the optional model path.

## Enforce the beta input contract

Accept exactly one primary adult in a single-frame, 8-bit `RGB` or grayscale
`L` JPG/JPEG, PNG, or WebP. Require a displayed short edge of at least 768
pixels and EXIF Orientation 1, 3, 6, or 8. The filename extension must match
the encoded format.

Reject transparent/alpha or palette-transparency files, animated or multi-frame
files, CMYK/palette/16-bit/other pixel modes, mirrored EXIF orientations 2/4/5/7,
unsupported formats, malformed color profiles, images above the safety pixel
limit, children, multiple equally prominent faces, and requests for geometry or
identity changes. Run the machine preflight before initializing:

```bash
python3 <skill-dir>/scripts/input_preflight.py <source>
```

Also inspect the source with `view_image`. Confirm the subject and scope; note
straight background lines, visible teeth, hair edges, face/neck visibility, and
the original crop. Never overwrite the source.

## Select one bounded mode

- `natural` / 自然 — preserve the scene mood; use by default.
- `fresh` / 清透 — gently lift whole-image midtones and neutral separation without whitening skin.
- `warm-film` / 暖调胶片 — add restrained highlight warmth and soft contrast without orange skin or fake film damage.

Modes may change only `light_balance` and `color_balance`. Keep identity,
geometry, skin, facial, hair/clothing, background, and audit rules identical.

## Compile and initialize

Compile all eight stages, including stages expected to make no pixel change:

```bash
python3 <skill-dir>/scripts/compile_pipeline.py \
  --mode <natural|fresh|warm-film> \
  --output <work-dir>/plan.json

python3 <skill-dir>/scripts/pipeline_state.py init <source> \
  --plan <work-dir>/plan.json \
  --run-dir <new-run-dir>
```

Use repeated `--set STAGE.PARAM=VALUE` overrides only when explicitly requested.
The default path is local and deterministic and makes zero image-model calls.

## Complete the fixed stage order

Run these stages exactly once and in order:

1. `photo_correction` — inspect orientation, horizon, lens/perspective, crop,
   canvas, and straight-line baselines. For the beta, record a justified safe
   no-op rather than inferring a geometric correction.
2. `light_balance` — adjust whole-photo exposure, highlights, shadows, and
   subject/background luminance.
3. `color_balance` — correct global white balance and apply the selected bounded look.
4. `skin_cleanup` — reduce temporary blemishes, redness, and patchy tone while
   preserving pores, fine lines, freckles, moles, scars, and highlights.
5. `facial_features` — inspect under-eye shadow, eyes, brows, teeth, and lips;
   allow only slight non-geometric definition when explicitly requested.
6. `hair_clothing` — inspect flyaways, hair edges, lint, patterns, logos, and
   garment structure without redesigning them.
7. `background` — inspect distractions, straight lines, perspective, depth of
   field, and scene cohesion without replacing or reconstructing content.
8. `final_review` — make no edits; render proof and resolve all ten checks.

For an applied deterministic stage, generate one pending candidate first:

```bash
python3 <skill-dir>/scripts/execute_pipeline.py \
  --run-dir <run-dir> \
  --stage <stage-id>
```

Inspect it against both the current checkpoint and immutable original. Then
make an explicit image-specific decision:

```bash
python3 <skill-dir>/scripts/execute_pipeline.py \
  --run-dir <run-dir> \
  --stage <stage-id> \
  --review-decision <accept|reject> \
  --notes "<what was visibly checked in this image>"
```

Accept only after visual inspection. A reject retains the previous checkpoint
and records a safe no-change. For an inspection-only or planned no-change stage:

```bash
python3 <skill-dir>/scripts/execute_pipeline.py \
  --run-dir <run-dir> \
  --stage <stage-id> \
  --confirm-inspection \
  --notes "<image-specific reason no safe change is needed>"
```

Use the executor only for stages 1–7. Never pass `final_review` as a stage.
Do not accept from geometry or pixel metrics alone. Controls marked
`optional_model_candidate_only` are not applied by the deterministic executor;
inspect those scopes and record a safe no-op unless the optional path is chosen.

## Use the optional isolated model path only when requested

Default to zero model calls. Use one image-model candidate only for a semantic
cleanup the deterministic path cannot localize. Treat it as an alternative
end-to-end candidate generated from the immutable original, never as an extra
stage or a chained input.

Compile and reserve the one allowance before contacting the external service:

```bash
python3 <skill-dir>/scripts/compile_model_prompt.py \
  --enable-model \
  --mode <natural|fresh|warm-film> \
  --plan-output <work-dir>/model-request.json \
  --prompt-output <work-dir>/model-prompt.txt

python3 <skill-dir>/scripts/pipeline_state.py reserve-model-attempt \
  --run-dir <run-dir> \
  --request <work-dir>/model-request.json \
  --prompt <work-dir>/model-prompt.txt \
  --provider "<provider>" \
  --model-name "<model>"
```

Use only the immutable original, reserved request, and reserved prompt.
Generate exactly one candidate with exactly one external call. Bind a
successful response before reviewing it:

```bash
python3 <skill-dir>/scripts/pipeline_state.py complete-model-attempt \
  --run-dir <run-dir> \
  --candidate <candidate>
```

If the service call fails, consume the allowance permanently and continue only
with the deterministic path:

```bash
python3 <skill-dir>/scripts/pipeline_state.py fail-model-attempt \
  --run-dir <run-dir> \
  --reason "<service failure>"
```

Render and audit the completed candidate using the same proof procedure as a
deterministic result. Reject identity, geometry, hands, clothing, straight-line,
background-content, complexion, or texture drift without retrying:

Never resize a model candidate to force a geometry or resolution pass.

```bash
python3 <skill-dir>/scripts/pipeline_state.py reject-model-attempt \
  --run-dir <run-dir> \
  --notes "<image-specific rejection evidence>"
```

Accept only a proof-bound candidate whose ten-item audit is already accepted:

```bash
python3 <skill-dir>/scripts/pipeline_state.py accept-model-candidate \
  --run-dir <run-dir> \
  --audit <run-dir>/audit.json \
  --notes "<image-specific acceptance evidence>"
```

The state machine blocks a second recorded reservation after success, failure,
or rejection and binds request, prompt, input, candidate, proof, and audit
hashes. It cannot independently prove that an operator did not bypass this
Skill and call an external service outside the recorded workflow. Disclose that
boundary in any regulated or high-assurance integration.

## Mandatory ten-item self-audit

Render the required comparison, difference heatmap, 100% detail, 720-pixel
thumbnail, and 1080-pixel phone preview. The 100% proof contains two
original/candidate detail rows: a conservative upper-body skin-density focus
and an upper-center fallback. Inspect both rows; they reduce missed-face risk
but are not face detection or identity authentication. `render_proof.py` writes
a versioned manifest that binds original/candidate hashes, every proof file
hash, and every required view:

```bash
python3 <skill-dir>/scripts/render_proof.py <immutable-original> <candidate> \
  --output-dir <run-dir>/proof
```

Inspect the manifest-bound views. Create the per-run review file without making
its future image-specific notes readable to other local users. On POSIX systems:

```bash
install -m 600 <skill-dir>/references/manual-review-template.json \
  <run-dir>/manual-review.json
```

On other platforms, copy `references/manual-review-template.json` into the run
work area and restrict it to the current user with the platform's equivalent
file permissions. For every check, write an object with `status` and a
non-placeholder, image-specific `note`. Use `pass` or `fail`; use
`not_applicable` only when the feature is truly absent and explain why. After
whitespace normalization, every note must be at least 16 characters and must
naturally name visible evidence relevant to that check. Never reuse one
portrait's notes for another.

Resolve these exact checks:

1. `skin_texture_natural`
2. `face_neck_color_consistent`
3. `sclera_teeth_not_blue`
4. `lip_saturation_natural`
5. `background_lines_straight`
6. `hair_edges_clean`
7. `subject_background_brightness_cohesive`
8. `zoom_and_thumbnail_coherent`
9. `change_not_excessive_vs_original`
10. `phone_viewing_comfortable`

Do not make the reviewer guess validator keywords. Write one complete
observation using the corresponding visible concept:

- skin texture, pores, or fine lines;
- face-versus-neck tone or undertone;
- sclera/eye-white or teeth visibility and color;
- lip color, saturation, or edge;
- a background line, door/wall frame, horizon, or geometry;
- hair edge, flyaway, halo, or smearing;
- subject-versus-background brightness, lighting, or exposure;
- both the 100% detail and thumbnail/reduced view;
- original-versus-candidate comparison, change, or difference heatmap;
- phone/mobile preview brightness, highlight, or shadow comfort.

For example, write `100% detail retains visible cheek pores and fine lines,
without waxy smearing`, not `looks good`. For `not_applicable`, name the absent
feature and the evidence, such as `Eyes are closed and no teeth are visible, so
sclera and tooth color cannot be evaluated`. Describe only what the current
proof actually shows; never copy the list or stuff terms into a false note.

Bind the review to the current proof manifest:

```bash
python3 <skill-dir>/scripts/audit_result.py <immutable-original> <candidate> \
  --mode <mode> \
  --manual-review <manual-review.json> \
  --proof-manifest <run-dir>/proof/manifest.json \
  --output <run-dir>/audit.json
```

Automatic metrics can reject extreme problems but never auto-accept semantic
quality. Finalize only when the audit decision is `accept`, proof files still
match their manifest hashes, and all ten manual statuses are `pass` or justified
`not_applicable`:

```bash
python3 <skill-dir>/scripts/pipeline_state.py finalize \
  --run-dir <run-dir> \
  --audit <run-dir>/audit.json
```

## Run a folder as a review queue

Treat every source as an independent V6 run. Prepare a stable, recursive queue:

```bash
python3 <skill-dir>/scripts/batch_pipeline_v6.py prepare \
  <input-folder> <output-folder> \
  --mode <natural|fresh|warm-film>
```

After reviewing and recording the currently pending item in each run, resume:

```bash
python3 <skill-dir>/scripts/batch_pipeline_v6.py resume \
  <input-folder> <output-folder>
```

Each `resume` invocation generates at most one pending deterministic candidate
or one final proof set per image. It never accepts a candidate, invents review
notes, calls an image model, or finalizes an audit. Refresh classifications
without creating or advancing work:

```bash
python3 <skill-dir>/scripts/batch_pipeline_v6.py status \
  <input-folder> <output-folder>
```

Report `complete`, `safe_but_subtle`, `partial_v3_fallback`, `failed`,
`awaiting_review`, and `prepared` separately. Never label a queue complete from
automatic checks alone. `batch_retouch.py` is a legacy partial V3 fallback, not
a complete V6 batch workflow.

## Protect privacy and report beta limits

- Keep run and queue directories private (`0700`) and their files private
  (`0600`) on supported POSIX systems.
- Treat every run directory as sensitive. It contains an immutable source copy
  and may retain camera metadata, including EXIF/GPS, plus checkpoints, proofs,
  review notes, and model artifacts.
- Treat the optional model path as a possible upload to a third-party provider;
  obtain appropriate consent and follow that provider's retention policy.
- Do not publish real portraits, run directories, proofs, metadata, or queue
  manifests in the source repository or release archive.
- Do not claim 90% success, unattended operation, automated identity
  authentication, or production readiness. This release is a public beta and
  requires a human or vision-capable Agent at every visual gate.
- Report mode, stage decisions, parameters, model-call state, dimensions,
  audit decision, proof path, run path, and unresolved or failed items.
