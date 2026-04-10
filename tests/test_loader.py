"""Tests for the knowledge APILoader with the YADE-native tree layout.

Verifies tree navigation, self-class surfacing, metadata promotion
(parent / attribute_count / method_count), and path resolution against
the 11-top-level-category YADE structure.
"""

from yade_mcp.knowledge.loader import APILoader


# =============================================================================
# Categories & basic structure
# =============================================================================


class TestListCategories:
    def test_returns_eleven_top_level_categories(self):
        cats = APILoader.list_categories()
        names = {c["name"] for c in cats}
        # Every YADE-native category must exist.
        expected = {
            "engine", "functor", "material", "shape",
            "iphys", "igeom", "state", "body",
            "bound", "runtime", "misc",
        }
        assert names == expected, f"missing={expected - names}, extra={names - expected}"

    def test_each_category_has_description_and_count(self):
        cats = APILoader.list_categories()
        for c in cats:
            assert isinstance(c.get("description"), str) and c["description"]
            assert isinstance(c.get("class_count"), int) and c["class_count"] >= 1

    def test_large_categories_have_top_level_nodes(self):
        """engine and functor have YADE-native sub-trees at depth 1."""
        cats = {c["name"]: c for c in APILoader.list_categories()}
        engine_nodes = cats["engine"].get("top_level_nodes", [])
        assert {"Dispatcher", "GlobalEngine", "PartialEngine"} <= set(engine_nodes), (
            f"engine top-level nodes: {engine_nodes}"
        )
        functor_nodes = cats["functor"].get("top_level_nodes", [])
        # Functor's main children: the dispatcher-callback bases.
        for expected in ("LawFunctor", "IGeomFunctor", "IPhysFunctor", "BoundFunctor"):
            assert expected in functor_nodes, f"missing {expected} in {functor_nodes}"


# =============================================================================
# Tree navigation
# =============================================================================


class TestTreeNavigation:
    def test_get_root_node(self):
        node = APILoader.get_tree_node("engine", [])
        assert node is not None
        assert "children" in node
        assert {"Dispatcher", "GlobalEngine", "PartialEngine"} <= set(node["children"])

    def test_get_nested_node(self):
        node = APILoader.get_tree_node("engine", ["GlobalEngine"])
        assert node is not None
        # GlobalEngine has BoundaryController, Collider, PeriodicEngine, etc.
        assert "BoundaryController" in node["children"]
        assert "PeriodicEngine" in node["children"]

    def test_get_deep_node(self):
        node = APILoader.get_tree_node("engine", ["GlobalEngine", "PeriodicEngine"])
        assert node is not None
        # PeriodicEngine has Recorder as a sub-tree.
        assert "Recorder" in node["children"]

    def test_nonexistent_category(self):
        assert APILoader.get_tree_node("nonsense", []) is None

    def test_nonexistent_node_in_valid_category(self):
        assert APILoader.get_tree_node("engine", ["NonsenseNode"]) is None

    def test_broken_mid_path(self):
        assert APILoader.get_tree_node("engine", ["GlobalEngine", "Nonsense"]) is None


# =============================================================================
# Node contents with self_class + entries
# =============================================================================


class TestListNodeContents:
    def test_root_node_has_no_self_class(self):
        contents = APILoader.list_node_contents("engine", [])
        assert contents is not None
        assert contents["self_class"] is None
        # engine root has Dispatcher/GlobalEngine/PartialEngine as children
        # plus Engine and ParallelEngine as direct leaves.
        names = {e["name"] for e in contents["entries"]}
        assert {"Dispatcher", "GlobalEngine", "PartialEngine"} <= names
        assert "Engine" in names  # direct leaf
        assert "ParallelEngine" in names  # direct leaf

    def test_internal_node_surfaces_self_class(self):
        contents = APILoader.list_node_contents("engine", ["GlobalEngine"])
        assert contents is not None
        self_class = contents["self_class"]
        assert self_class is not None
        assert self_class["name"] == "GlobalEngine"
        assert self_class.get("parent") == "Engine"

    def test_sub_trees_carry_descendant_count(self):
        contents = APILoader.list_node_contents("engine", ["GlobalEngine"])
        by_name = {e["name"]: e for e in contents["entries"]}
        bc = by_name.get("BoundaryController")
        assert bc is not None
        assert bc.get("has_children") is True
        assert bc.get("descendant_count", 0) >= 1

    def test_leaf_entries_have_class_metadata(self):
        contents = APILoader.list_node_contents("engine", ["GlobalEngine"])
        by_name = {e["name"]: e for e in contents["entries"]}
        # NewtonIntegrator is a direct leaf of GlobalEngine.
        ni = by_name.get("NewtonIntegrator")
        assert ni is not None
        assert ni.get("has_children") is not True  # leaf, not a sub-tree
        assert ni.get("parent") == "GlobalEngine"
        assert isinstance(ni.get("attribute_count"), int)

    def test_no_duplicate_between_children_and_leaves(self):
        """A class that is both a tree node AND was originally in
        direct_classes must appear exactly once (as the tree node)."""
        contents = APILoader.list_node_contents("engine", [])
        names = [e["name"] for e in contents["entries"]]
        assert len(names) == len(set(names)), f"duplicates: {names}"


# =============================================================================
# Path resolution
# =============================================================================


class TestResolvePath:
    def test_empty_path_is_root(self):
        assert APILoader.resolve_path("")["level"] == "root"

    def test_category_only(self):
        r = APILoader.resolve_path("engine")
        assert r["level"] == "tree_node"
        assert r["category"] == "engine"
        assert r["tree_path"] == []

    def test_one_level_deep(self):
        r = APILoader.resolve_path("engine.GlobalEngine")
        assert r["level"] == "tree_node"
        assert r["tree_path"] == ["GlobalEngine"]

    def test_two_levels_deep(self):
        r = APILoader.resolve_path("engine.GlobalEngine.PeriodicEngine")
        assert r["level"] == "tree_node"
        assert r["tree_path"] == ["GlobalEngine", "PeriodicEngine"]

    def test_leaf_class(self):
        r = APILoader.resolve_path("engine.GlobalEngine.NewtonIntegrator")
        assert r["level"] == "class"
        assert r["category"] == "engine"
        assert r["tree_path"] == ["GlobalEngine"]
        assert r["class_name"] == "NewtonIntegrator"

    def test_functor_leaf(self):
        # Law2_ScGeom_BubblePhys_Bubble is a pure LawFunctor leaf (no own
        # subclasses) — Law2_ScGeom_FrictPhys_CundallStrack is itself a
        # tree node because it has subclasses of its own.
        r = APILoader.resolve_path(
            "functor.LawFunctor.Law2_ScGeom_BubblePhys_Bubble"
        )
        assert r["level"] == "class"
        assert r["class_name"] == "Law2_ScGeom_BubblePhys_Bubble"

    def test_class_that_is_also_a_tree_node_resolves_as_tree_node(self):
        """YADE classes that have their own subclasses (e.g.
        Law2_ScGeom_FrictPhys_CundallStrack → Law2_..._ViscoFrictPhys_...)
        are internal tree nodes. Resolving their path returns tree_node,
        not class; the class's own docs come via the node's self_class."""
        r = APILoader.resolve_path(
            "functor.LawFunctor.Law2_ScGeom_FrictPhys_CundallStrack"
        )
        assert r["level"] == "tree_node"
        assert r["tree_path"][-1] == "Law2_ScGeom_FrictPhys_CundallStrack"

    def test_no_class_name_fallback(self):
        """Reaching a leaf class requires its full parent chain. The bare
        class name at the category root must NOT resolve."""
        r = APILoader.resolve_path("engine.NewtonIntegrator")
        assert r["level"] == "error", "class-name fallback should not exist"

    def test_nonexistent_category(self):
        r = APILoader.resolve_path("nonexistent")
        assert r["level"] == "error"
        assert "available" in r

    def test_segment_after_leaf_is_error(self):
        r = APILoader.resolve_path("engine.GlobalEngine.NewtonIntegrator.extra")
        assert r["level"] == "error"


# =============================================================================
# Query -> browse handoff paths
# =============================================================================


class TestBuildBrowsePath:
    def test_leaf_class_returns_full_browse_path(self):
        path = APILoader.build_browse_path("engine", "NewtonIntegrator")
        assert path == "engine.GlobalEngine.NewtonIntegrator"

    def test_tree_node_class_returns_valid_tree_path(self):
        path = APILoader.build_browse_path("engine", "GravityEngine")
        assert path == "engine.GlobalEngine.FieldApplier.GravityEngine"

    def test_unknown_class_returns_none(self):
        assert APILoader.build_browse_path("engine", "DefinitelyNotAClass") is None


# =============================================================================
# Patched parent data — regression guard (unchanged from pre-restructure)
# =============================================================================


class TestPatchedParentData:
    """The runtime-driven parent patch must survive the restructure: specific
    classes whose parent was wrong or empty in the scraper output must still
    point to the correct YADE ancestor after reorganisation."""

    def _load_doc(self, category, name):
        return APILoader.load_class(category, name)

    def test_top_of_hierarchy_classes(self):
        # Engine and Functor now live under engine/ and functor/ respectively.
        engine_doc = self._load_doc("engine", "Engine")
        assert engine_doc["parent"] == "Serializable"
        functor_doc = self._load_doc("functor", "Functor")
        assert functor_doc["parent"] == "Serializable"

    def test_shape_and_material_roots(self):
        assert self._load_doc("shape", "Shape")["parent"] == "Serializable"
        assert self._load_doc("material", "Material")["parent"] == "Serializable"

    def test_previously_empty_classes(self):
        # Body, State, Scene, etc. — patched to Serializable from empty.
        assert self._load_doc("body", "Body")["parent"] == "Serializable"
        assert self._load_doc("state", "State")["parent"] == "Serializable"
        assert self._load_doc("runtime", "Scene")["parent"] == "Serializable"

    def test_engine_subtree_parents_match_yade(self):
        # Sanity check a sample of well-known engines.
        assert self._load_doc("engine", "NewtonIntegrator")["parent"] == "GlobalEngine"
        assert self._load_doc("engine", "GravityEngine")["parent"] == "FieldApplier"
        assert self._load_doc("engine", "TriaxialStressController")["parent"] == "BoundaryController"

    def test_functor_subtree_parents_match_yade(self):
        # Law2_* should be LawFunctor children.
        assert self._load_doc(
            "functor", "Law2_ScGeom_FrictPhys_CundallStrack"
        )["parent"] == "LawFunctor"
        assert self._load_doc("functor", "Bo1_Sphere_Aabb")["parent"] == "BoundFunctor"


# =============================================================================
# Listing description cap (unchanged guarantee)
# =============================================================================


class TestDescriptionCap:
    def test_listing_description_is_capped(self):
        contents = APILoader.list_node_contents("engine", ["GlobalEngine"])
        for e in contents["entries"]:
            assert len(e["description"]) <= 120
