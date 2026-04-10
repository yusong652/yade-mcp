#!/usr/bin/env python3
"""One-shot migration: restructure docs to YADE-native 11-category tree.

Moves every class JSON from the current 5-top-level layout
(engines/bodies/materials/shapes/interactions/...) to a YADE-native
11-top-level layout rooted in YADE's class hierarchy:

    engine/    functor/   material/  shape/    iphys/
    igeom/     state/     body/      bound/    runtime/   misc/

Files stay FLAT within each category. The tree structure is stored as
metadata in index.json, NOT reflected in the filesystem.

Runs from the repo root. Uses ``git mv`` so history is preserved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "src/yade_mcp/knowledge/resources/python_api_docs"

TOP_LEVEL_ROOTS = {
    "Engine": "engine",
    "Functor": "functor",
    "Material": "material",
    "Shape": "shape",
    "IPhys": "iphys",
    "IGeom": "igeom",
    "State": "state",
    "Bound": "bound",
}
BODY_LIKE = {"Body", "Cell", "Interaction", "EnergyTracker", "MatchMaker"}
RUNTIME_WRAPPERS = {
    "Serializable", "Omega",
    "BodyContainer", "BodyIterator",
    "InteractionContainer", "InteractionIterator",
    "MaterialContainer", "ForceContainer",
    "STLImporter", "TagsWrapper", "TimingDeltas",
}

# Human-readable descriptions for each top-level category in the new index
CATEGORY_DESCRIPTIONS = {
    "engine": "Simulation engines (Engine subtree) — global/partial/periodic/dispatchers",
    "functor": "Pluggable functors invoked by dispatchers — bound, contact geometry, contact physics, contact laws, rendering",
    "material": "Material types (Material subtree) — friction, cohesive, visco-elastic, etc.",
    "shape": "Body geometry types (Shape subtree) — sphere, box, wall, facet, polyhedron, etc.",
    "iphys": "Contact physics data types (IPhys subtree) — FrictPhys, NormShearPhys, etc.",
    "igeom": "Contact geometry data types (IGeom subtree) — ScGeom, L3Geom, PolyhedraGeom, etc.",
    "state": "Body state variants (State subtree) — CpmState, ThermalState, etc.",
    "body": "Core body-related classes — Body, Cell, Interaction, EnergyTracker, MatchMaker",
    "bound": "Body bounding volumes (Bound subtree) — Aabb",
    "runtime": "YADE runtime wrappers and global singletons — Omega, Serializable, containers, iterators",
    "misc": "Miscellaneous types that don't fit a specific subtree",
}


def load_all_classes() -> dict[str, dict[str, Any]]:
    """Read every class JSON under DOCS_ROOT."""
    result: dict[str, dict[str, Any]] = {}
    for dirpath, _, files in os.walk(DOCS_ROOT):
        for f in files:
            if not f.endswith(".json") or f == "index.json":
                continue
            full = Path(dirpath) / f
            with open(full) as fp:
                doc = json.load(fp)
            name = doc.get("name") or full.stem
            result[name] = {
                "parent": doc.get("parent", ""),
                "current_path": full,
                "doc": doc,
            }
    return result


def classify(
    name: str,
    all_classes: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (category_name, ancestor_chain) for a class.

    The ancestor_chain lists YADE parent classes between the category root
    and the class itself, excluding both endpoints. For NewtonIntegrator →
    ("engine", ["GlobalEngine"]) because the path within engine/ is
    engine.GlobalEngine.NewtonIntegrator.
    """
    if seen is None:
        seen = set()
    if name in seen:
        return ("misc", [])
    seen.add(name)

    if name in TOP_LEVEL_ROOTS:
        return (TOP_LEVEL_ROOTS[name], [])
    if name in BODY_LIKE:
        return ("body", [])
    if name in RUNTIME_WRAPPERS:
        return ("runtime", [])

    info = all_classes.get(name)
    if info is None:
        return ("misc", [])

    parent = info["parent"]
    if parent in ("", "instance", None):
        return ("misc", [])

    cat, parent_chain = classify(parent, all_classes, seen)
    if parent in TOP_LEVEL_ROOTS:
        return (cat, [])
    if parent in BODY_LIKE or parent in RUNTIME_WRAPPERS:
        return (cat, [parent])
    return (cat, parent_chain + [parent])


def build_tree(
    classification: dict[str, tuple[str, list[str]]],
) -> dict[str, dict[str, Any]]:
    """Build the per-category nested tree structure from classifications.

    Each category maps to a nested dict of the form::

        {
            "direct_classes": [names of classes attached directly at this node],
            "children": {
                "NodeName": { "direct_classes": [...], "children": {...} },
                ...
            },
        }

    The root of each category represents the category itself.
    """
    trees: dict[str, dict[str, Any]] = {}
    for name, (cat, chain) in classification.items():
        if cat not in trees:
            trees[cat] = {"direct_classes": [], "children": {}}
        node = trees[cat]
        for link in chain:
            if link not in node["children"]:
                node["children"][link] = {"direct_classes": [], "children": {}}
            node = node["children"][link]
        node["direct_classes"].append(name)
    # Sort direct_classes and recursively sort children keys for stable output
    def _sort(node: dict[str, Any]) -> None:
        node["direct_classes"].sort()
        new_children: dict[str, dict[str, Any]] = {}
        for k in sorted(node["children"].keys()):
            new_children[k] = node["children"][k]
            _sort(new_children[k])
        node["children"] = new_children

    for cat in trees:
        _sort(trees[cat])
    return trees


def git_mv(src: Path, dst: Path) -> None:
    """Rename src -> dst using ``git mv`` so history is preserved."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "mv", str(src), str(dst)],
        check=True,
        cwd=REPO_ROOT,
    )


def main() -> int:
    all_classes = load_all_classes()
    print(f"Loaded {len(all_classes)} classes", file=sys.stderr)

    classification: dict[str, tuple[str, list[str]]] = {}
    for name in all_classes:
        classification[name] = classify(name, all_classes)

    cat_counts: dict[str, int] = defaultdict(int)
    for cat, _ in classification.values():
        cat_counts[cat] += 1
    print("New category counts:", file=sys.stderr)
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:12} {n:4}", file=sys.stderr)

    # --- Phase 1: move files ---
    moves_done = 0
    for name, info in all_classes.items():
        src: Path = info["current_path"]
        cat = classification[name][0]
        dst = DOCS_ROOT / cat / src.name
        if src == dst:
            continue
        git_mv(src, dst)
        moves_done += 1
    print(f"Phase 1 done: {moves_done} files moved", file=sys.stderr)

    # --- Phase 2: remove empty old dirs ---
    old_dirs = [
        DOCS_ROOT / "engines",
        DOCS_ROOT / "bodies",
        DOCS_ROOT / "materials",
        DOCS_ROOT / "shapes",
        DOCS_ROOT / "omega",
        DOCS_ROOT / "utils",
        DOCS_ROOT / "interactions" / "laws",
        DOCS_ROOT / "interactions" / "geometry",
        DOCS_ROOT / "interactions" / "physics",
        DOCS_ROOT / "interactions",
    ]
    for d in old_dirs:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
            print(f"Removed empty {d.relative_to(REPO_ROOT)}", file=sys.stderr)

    # --- Phase 3: regenerate index.json ---
    trees = build_tree(classification)
    new_index: dict[str, Any] = {"categories": {}}
    for cat in sorted(trees.keys()):
        tree = trees[cat]
        # Flat list for quick iteration / backward compatibility
        flat_classes: list[str] = []

        def _collect(node: dict[str, Any]) -> None:
            flat_classes.extend(node["direct_classes"])
            for child in node["children"].values():
                _collect(child)

        _collect(tree)
        new_index["categories"][cat] = {
            "description": CATEGORY_DESCRIPTIONS.get(cat, ""),
            "classes": sorted(flat_classes),
            "tree": tree,
        }

    index_path = DOCS_ROOT / "index.json"
    with open(index_path, "w", encoding="utf-8") as fp:
        json.dump(new_index, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    print(f"Phase 3 done: rewrote {index_path.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
