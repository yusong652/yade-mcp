# YADE Doc Drift Report — 2026-04-11

## Context

After the Level-2 restructure (11 YADE-native categories, parent patch from
runtime ground truth, tree-based browse/query), I spot-checked 14 diverse
classes via `yade_execute_code` using `cls().dict()` to compare the attribute
lists our JSON exposes against YADE runtime.

- **Structure (category / parent / class existence)**: ✅ correct
- **Attribute lists**: 🔶 partial drift — this file tracks what needs a
  fresh scrape against the current YADE build.

This drift is **pre-existing** in the scraped JSON, not introduced by today's
refactor. It will be fixed when we rewrite the scraper (the companion
Phase-B work enabled by the `/docs_output` bind mount in `docker/run.sh`).

## Probe method

Runtime ground truth was obtained like this:

```python
import yade.wrapper as w
cls = getattr(w, "NewtonIntegrator")
attrs = sorted(cls().dict().keys())  # includes inherited attrs
```

To isolate class-specific attrs we subtracted the base class dict:

```python
base = set(w.GlobalEngine().dict().keys())
specific = set(w.NewtonIntegrator().dict().keys()) - base
```

## Sample coverage (14 classes)

| Class | JSON | RT | Match |
|---|---:|---:|---|
| ScGeom | 6 | 6 | ✅ |
| Law2_ScGeom_FrictPhys_CundallStrack | 3 | 3 | ✅ |
| Ip2_FrictMat_FrictMat_FrictPhys | 3 | 3 | ✅ |
| Bo1_Sphere_Aabb | 1 | 1 | ✅ |
| Sphere | 4 | 1 | 🟢 (scraper kept Shape base attrs — desired) |
| Box | 4 | 1 | 🟢 (scraper kept Shape base attrs — desired) |
| Aabb | 6 | 0 | 🟢 (scraper kept Bound base attrs — desired) |
| FrictMat | 4 | 3 | 🟢 (scraper kept Material base attrs — desired) |
| ForceEngine | 2 | 1 | 🟢 (scraper kept PartialEngine base attrs — desired) |
| **VTKRecorder** | 16 | 20 | 🔶 **scraper inconsistent** (missing PeriodicEngine base attrs) |
| **PyRunner** | 8 | 13 | 🔶 **scraper inconsistent** (missing PeriodicEngine base attrs) |
| **NewtonIntegrator** | 4 | 9 | 🟥 **real drift** (missing 5 attrs, has 1 stale) |
| **TriaxialStressController** | 47 | 38 | 🟥 **real drift** (9 attrs no longer exist in runtime) |
| **InsertionSortCollider** | 18 | 17 | 🟥 **real drift** (minor) |

Legend:
- ✅ perfect match
- 🟢 JSON includes base class attrs deliberately (good documentation practice)
- 🔶 scraper inconsistency (treats some base classes differently from others)
- 🟥 real drift (JSON and runtime actually disagree about what exists)

## Categorised findings

### Category A — 4 classes match exactly

ScGeom, Law2_ScGeom_FrictPhys_CundallStrack, Ip2_FrictMat_FrictMat_FrictPhys,
Bo1_Sphere_Aabb.

No action needed.

### Category B — Scraper helpfully includes base class attrs (5 classes)

Sphere, Box, Aabb, FrictMat, ForceEngine all have **more attrs in JSON than
in their "class-specific" runtime set**. The extras come from their immediate
base class (Shape / Bound / Material / PartialEngine).

This is actually good — users looking at `Sphere` want to know they can set
`color` / `highlight` / `wire` without also opening the `Shape` page.

**Action: keep this behaviour when rewriting the scraper.** Do NOT strip base
class attrs aggressively.

### Category C — Scraper inconsistency (2 classes)

VTKRecorder and PyRunner both **exclude PeriodicEngine base attrs**:
`firstIterRun, iterLast, nDone, realLast, virtLast`.

This contradicts Category B — for Shape/Material the scraper kept base
attrs, for PeriodicEngine it dropped them.

**Action in the new scraper**: apply a consistent rule for base class
attr inclusion. Recommended: always include the immediate YADE parent's
attrs, but not the parent's parent's (show one level of inheritance).

### Category D — Real version / build drift (3 classes)

#### NewtonIntegrator (JSON 4, runtime 9)

- **Missing in JSON**: `dampGravity`, `kinSplit`, `mask`, `maxVelocitySq`,
  `prevVelGrad`, `warnNoForceReset`
- **Stale in JSON**: `kinEnergy` (exists in JSON, not in runtime)

Likely explanation: the scraper source was an older YADE version where
`NewtonIntegrator` had a simpler API. Newer attrs like `warnNoForceReset`
and `dampGravity` were added later.

#### TriaxialStressController (JSON 47, runtime 38)

- **Stale in JSON** (9 attrs): `boxVolume`, `legacyStressDamping`,
  `max_vel1`, `max_vel2`, `max_vel3`, plus 4 more — not present in current
  runtime.

Likely explanation: refactoring of the triaxial controller removed these
attrs. Possibly `legacyStressDamping` → `stressDamping`, per-axis
`max_vel1/2/3` → single `max_vel`, etc.

#### InsertionSortCollider (JSON 18, runtime 17)

- **Missing in JSON**: `boundDispatcher`
- **Stale in JSON**: `periodic`, `strideActive`

Minor drift, low impact.

## Cosmetic issues (not accuracy problems)

### TriaxialTest description

`runtime/Serializable/FileGenerator/TriaxialTest.json` has a **7846-char
description** that includes an entire FAQ and usage guide. This inflates
single-class browse responses (verified fits under 131k cap, but ugly).

**Action**: when rewriting docs, consider splitting long descriptions into
`description` (first paragraph) + `long_description` (rest), and default
browse to the short form.

### misc/enum scraper artifact

`AttrFlags.parent = "enum"` where `enum` has no JSON file. Loader handles
this gracefully (`has_docs: false`), but semantically `enum` is the Python
stdlib `enum.Enum`, not a YADE class. The tree shows `misc.enum.AttrFlags`
which is mildly confusing.

**Action in new scraper**: detect when parent is a Python stdlib type
(`enum`, `object`, `instance`) and treat it as "no YADE parent" → AttrFlags
becomes a direct leaf of `misc/`.

### JCFpmState parent mismatch

Our JSON says `parent=ThermalState`, runtime says `parent=State`. Left
unpatched during the runtime-driven parent patch because the patch script
was defensive about mismatches. Possible explanations:

- This build lacks ThermalState support, so JCFpmState falls back to State
- Or scraper source had a different parent

**Action**: on the new scraper run, whichever value runtime reports becomes
the authoritative answer.

## Not yet verified

Things we didn't probe in this round but may need similar drift checks:

- Attribute **default values** (we compared names but not defaults)
- Attribute **descriptions** (whether documentation text matches current
  C++ docstrings)
- **Method lists** (we only spot-checked counts)
- **Method signatures / descriptions**
- Classes in categories other than the sample (e.g. Dispatcher subtypes,
  GlStateFunctor family, whole `iphys/` subtree)

## Next steps (for future scraper rewrite)

1. **Run inside YADE container**, write to `/docs_output/` (bind-mounted
   in `docker/run.sh`)
2. **Use runtime as ground truth** — iterate `yade.wrapper.*`, use
   `cls().dict()` for attrs
3. **Consistent base class policy** — include immediate parent's attrs,
   not the whole chain (Category C fix)
4. **Parse docstrings** via `cls.__doc__` / `type(inst).__doc__` and
   `inspect.getdoc()` for per-attr descriptions
5. **Preserve existing category / tree structure** in
   `src/yade_mcp/knowledge/resources/python_api_docs/` (files stay flat,
   tree comes from the new parent-driven classify)
6. **Split long descriptions** into short (first paragraph) + long
7. **Verify against this drift report** — when all 10 classes in
   categories C and D come back 🟢, the scraper is correct

## Quick re-check script (stub)

```python
# Drop this in scripts/verify_attrs_against_runtime.py after new scraper
import json, os
import yade.wrapper as w
from pathlib import Path

DOCS = Path("/path/to/python_api_docs")
for json_file in DOCS.rglob("*.json"):
    if json_file.name == "index.json": continue
    doc = json.loads(json_file.read_text())
    name = doc["name"]
    cls = getattr(w, name, None)
    if cls is None: continue
    try:
        rt = set(cls().dict().keys())
    except Exception:
        continue
    js = {a["name"] for a in doc.get("attributes", [])}
    if rt != js:
        print(f"{name}: missing={sorted(rt-js)} extra={sorted(js-rt)}")
```
