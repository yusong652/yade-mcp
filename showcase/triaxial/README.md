# Drained triaxial test — agent-produced

These scripts were **written by an AI agent driving YADE through
[yade-mcp](../../)** during the demo video. They are the agent's output, not
hand-written API examples — kept here verbatim so the result shown in the
video can be inspected and reproduced.

> Demo video: <https://www.youtube.com/watch?v=3VMOU-FUY4M>

## What it is

A classic soil-mechanics drained triaxial test on a dry sphere assembly:

1. **`triaxial_consolidation.py`** — Stage 1. Generates 3000 spheres
   (r ∈ [0.5, 2.0] mm) in a 50 mm cube, then isotropically consolidates to
   100 kPa with low compaction friction (φ = 1°). Saves the consolidated
   state to `/workspace/triax_consolidated_100kPa.yade.bz2`.

2. **`triaxial_drained_shear.py`** — Stage 2. Loads the consolidated state,
   restores friction to φ = 30°, switches the `TriaxialStressController` to
   strain-rate control on the axial (y) axis with 100 kPa lateral stress, and
   shears to ε_axial = 15 %. Saves the sheared state, a pickle of the raw
   curves, and a 4-panel matplotlib figure (q–εa, εv–εa, stress path, void
   ratio).

3. **`triaxial_reequilibrate.py`** — *optional repair step, not standalone.*
   Used interactively to re-settle the sample after manually erasing
   particles that escaped the box. It references `triax` as a free variable,
   so it only runs in a session where Stage 1 has already populated the YADE
   global namespace (yade-mcp's bridge shares one namespace across calls). It
   overwrites the consolidated `.bz2`.

## Running

The scripts use absolute paths under `/workspace/...` — the mount root inside
the YADE container the agent was driving. To reproduce:

- Run them inside that container (where `/workspace` exists), in order:
  Stage 1 → Stage 2. `triaxial_reequilibrate.py` only if particles escape.
- Or adjust the hardcoded `/workspace/...` paths to a local directory.

Sign convention: `TriaxialStressController` uses continuum-mechanics signs
(tension positive), so compressive targets are negative in the code; console
output flips them back to "compression positive" kPa for readability.

## Expected output

Stage 2 writes `triax_drained_shear_curves.png` — deviator stress peaking
then softening toward a critical state, with dilatant volume change, the
textbook signature of a dense drained sand.
