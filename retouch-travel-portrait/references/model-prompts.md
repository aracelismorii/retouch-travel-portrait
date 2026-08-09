# V6 optional model prompt contract

Default execution uses zero model calls. Use the image model only when the user
explicitly enables the optional model path. Generate at most one candidate from
the immutable original. Do not retry, do not feed a model result into another
model call, and do not create per-stage model candidates.

The single prompt must describe the required logic in this exact order:

1. `photo_correction`
2. `light_balance`
3. `color_balance`
4. `skin_cleanup`
5. `facial_features`
6. `hair_clothing`
7. `background`
8. `final_review` is review-only and must not request an edit

State the selected mode once. It may affect only `light_balance` and
`color_balance`. Use the shared defaults for every later stage.

Repeat these invariants in every optional model request:

- Image 1 is the immutable original and the only edit target.
- Preserve exact identity, age, facial and body geometry, expression, gaze,
  pose, hands, fingers, crop, aspect ratio, perspective, and subject placement.
- Preserve skin complexion and undertone; retain pores, fine lines, freckles,
  moles, scars, and natural highlights.
- Preserve eye and tooth color, natural lip saturation, hairline, hairstyle,
  garment design, accessories, objects, background structure, straight lines,
  depth of field, lighting direction, and scene content.
- Never whiten skin, reshape a face or body, add makeup, regenerate a
  background, invent or remove material objects, or add text or watermarks.

The prompt must request a conservative professional photo retouch, not a newly
generated portrait. If the candidate is rejected, discard it and use the V3
deterministic fallback. Disclose that V3 supplies only bounded light and skin
work and does not deliver the complete V6 workflow or non-natural style modes.
