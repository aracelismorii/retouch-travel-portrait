# V6 stage contracts

Run the eight stages in the exact order defined by `pipeline-v6.json`. Record a
safe no-op with a reason when a stage needs no change. Never silently skip or
reorder a stage.

| Stage | Allowed work | Reject or leave unchanged when |
|---|---|---|
| `photo_correction` | Inspect EXIF orientation, horizon, lens distortion, perspective, crop, and canvas. Apply only geometry-safe correction. | Correction would bend straight lines, invent edges, crop unexpectedly, or move the subject. |
| `light_balance` | Balance whole-image exposure, highlights, shadows, and subject/background luminance. | The person becomes cut out, lighting direction changes, or highlights clip. |
| `color_balance` | Correct global white balance and apply the selected bounded mode recipe. | Skin undertone changes, neutral whites turn blue, lips oversaturate, or the scene is recolored aggressively. |
| `skin_cleanup` | Reduce temporary blemishes, redness, and patchy tone; apply texture-preserving smoothing. | Pores, fine lines, moles, freckles, scars, or normal highlights disappear. |
| `facial_features` | Slightly soften under-eye shadow and add restrained non-geometric feature definition. | Eye, nose, lip, jaw, tooth, expression, makeup, or apparent age changes. |
| `hair_clothing` | Tidy isolated flyaways, lint, and small temporary clothing distractions. | Hairline, hairstyle, hair volume, body outline, garment design, pattern, or texture changes. |
| `background` | Reduce only minor distractions and maintain background balance. | Background replacement, object invention, local warping, line bending, or depth-of-field change is required. |
| `final_review` | Compare original and result at 100%, fit view, thumbnail, and phone-normal-brightness view; evaluate all ten audit keys. | Any required audit key is missing or failed. No edits are allowed in this stage. |

## Execution boundary

The stage list is the required retouching logic, not permission to chain image
models. Default execution uses zero model calls. When an optional model edit is
explicitly enabled, compile all requested work into one candidate generated
from the immutable original. Never use a model output as another model input,
never retry the model, and never accept more than one candidate.

Compare every intermediate or candidate to the immutable original. Preserve
identity, age, facial and body geometry, expression, gaze, pose, hands,
clothing, accessories, crop, aspect ratio, perspective, and scene content.

For deterministic applied stages, generating pixels and accepting them are two
separate actions. Generation creates a hash-bound pending candidate and cannot
advance the state. Visual acceptance advances it; visual rejection records the
failure and rolls back to the prior accepted checkpoint as a safe no-change.

The default zero-model executor does not apply the non-zero intent parameters
listed for facial features, hair/clothing, or background. Those values are
available only to the one optional model candidate and are labeled accordingly
in the compiled plan.

If the optional model candidate fails, discard it. The deterministic V3
fallback may deliver conservative light and skin work only; label it as partial
delivery rather than a complete V6 result.
