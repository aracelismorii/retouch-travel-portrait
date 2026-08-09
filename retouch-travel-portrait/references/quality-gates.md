# V6 quality gates

Compare all results with the immutable original. A completed tool call is never
an acceptance signal.

## Preflight and canvas

- Require one primary adult and a readable JPG, PNG, or WebP.
- Reject an optional model candidate whose relative aspect-ratio error exceeds
  `0.005` or whose short edge is below 768 pixels.
- Passing canvas checks means only `visual_review_required`; it never proves
  identity, local geometry, or naturalness.
- Preserve exact dimensions for deterministic outputs.
- A deterministic pixel edit remains a pending candidate after mechanical
  checks. Advance its checkpoint only after an explicit visual `accept` with
  image-specific notes. A visual `reject` must retain the prior checkpoint.

## Identity and stage locks

Reject a candidate when identity, age, facial or body geometry, expression,
gaze, pose, hands, fingers, crop, perspective, subject placement, clothing,
accessories, objects, or scene identity changes. Apply the stage-specific locks
in `stage-contracts.md` before moving to the next stage.

The mode may affect only `light_balance` and `color_balance`:

- `natural`: preserve the original scene mood and neutral color character.
- `fresh`: allow a small midtone lift and clean neutral separation without
  whitening skin, clipping highlights, or washing the background pastel.
- `warm-film`: allow mild warm highlights, slightly muted saturation, and soft
  contrast without orange skin, teal-orange grading, crushed blacks, or fake
  film damage.

Parameters for `facial_features`, `hair_clothing`, and `background` are optional-
model intent only. The zero-model executor must not present them as applied
pixel edits; it performs visual inspection and a reasoned safe no-change.

## Final ten-item audit

Use the exact keys and decision policy in `audit-gates.json`. Review at 100%, fit
view, thumbnail size, side-by-side with the original, and phone-normal-
brightness view. Every visible concern must pass. `not_applicable` is permitted
only when the feature is genuinely absent or not visible and the reviewer adds
a reason.

Acceptance requires:

- all ten keys and all required views are present
- no audit key is `fail`
- automated canvas checks pass
- the result is visibly improved but remains conservative against the original

## Decision and fallback

- `deterministic_accepted`: zero-model V6 output passes all applicable gates.
- `model_accepted`: the single optional model candidate passes all gates.
- `deterministic_fallback`: the optional candidate is absent or rejected and
  the safe V3 result is returned as partial delivery.
- `failed`: no safe usable result exists.

Never retry or chain an image model. Never call a V3 fallback result a complete
V6 delivery: V3 is limited to conservative light and skin processing and does
not deliver under-eye, facial-feature, hair/clothing, background, fresh, or
warm-film work.
