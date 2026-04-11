"""Refresh YADE API doc JSONs against runtime ground truth.

Runs inside the yade-dev container. Reads and writes /docs_output,
which is bind-mounted to src/yade_mcp/knowledge/resources/python_api_docs
on the host (see docker/run.sh).

Strategy:
- Iterate yade.wrapper.* and default-instantiate every class that can be.
- Extract each attribute from the class MRO's property descriptors, parse
  the boost.python docstring for :ydefault:/:yattrtype:/:yattrflags:, fall
  back to live values for type/default.
- Merge into the existing JSON file (by class name): preserve curated
  class description and per-attr descriptions where they still apply;
  drop stale attrs; add missing ones; refresh types/defaults from runtime.
- Classes that can't be default-constructed are skipped (their JSONs
  remain untouched; see drift todo §Next Steps 8 for the follow-up).

Invoke from a YADE console (interactive or via MCP bridge):

    exec(open("/usr/local/lib/python3.10/dist-packages/yade_mcp_bridge/.."
              "/scripts/refresh_api_docs.py").read())

or (simpler inside the bridged session)::

    import refresh_api_docs; refresh_api_docs.main()
"""

import json
import os
import re
import sys
import traceback

import yade.wrapper as w  # type: ignore

DOC_ROOT = "/docs_output"

# --- docstring parsing ---------------------------------------------------

_FLAG = re.compile(r":yattrflags:`(\d+)`")
_TYPE = re.compile(r":yattrtype:`([^`]*)`")
_DEFAULT = re.compile(r":ydefault:`([^`]*)`")
_YREF = re.compile(r":yref:`([^<`]*)(?:<[^`]*>)?`")
_YUPDATE = re.compile(r"\|yupdate\|")
_STRIP_MARKERS = re.compile(r"\s*:y(?:default|attrtype|attrflags):`[^`]*`")
# Broader rST cleanup (after the YADE-specific markers have been handled).
_RST_REF = re.compile(r":ref:`([^<`]*)(?:<[^`]*>)?`")
_RST_DOC = re.compile(r":doc:`([^<`]*)(?:<[^`]*>)?`")
_RST_CITATION = re.compile(r"\[([A-Z][a-zA-Z]+\d{4}[a-z]?)\]_")
_RST_INLINE_LITERAL = re.compile(r"``([^`]+)``")
_RST_DOUBLE_UNDER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)__")  # rST target suffix

_TYPE_MAP = {
    "Real": "float",
    "double": "float",
    "float": "float",
    "int": "int",
    "unsigned int": "int",
    "unsigned": "int",
    "unsigned long": "int",
    "size_t": "int",
    "long": "int",
    "long long": "int",
    "short": "int",
    "bool": "bool",
    "Vector3r": "Vector3",
    "Vector2r": "Vector2",
    "Vector3i": "Vector3i",
    "Vector2i": "Vector2i",
    "Vector6r": "Vector6",
    "Vector6i": "Vector6i",
    "Matrix3r": "Matrix3",
    "Matrix6r": "Matrix6",
    "MatrixXr": "MatrixX",
    "VectorXr": "VectorX",
    "Quaternionr": "Quaternion",
    "std::string": "str",
    "string": "str",
    "void": "None",
    "NoneType": "None",
}

_DEFAULT_ALIASES = {
    "Vector3r::Zero()": "(0, 0, 0)",
    "Vector2r::Zero()": "(0, 0)",
    "Vector6r::Zero()": "(0, 0, 0, 0, 0, 0)",
    "Matrix3r::Zero()": "Matrix3.Zero",
    "Matrix3r::Identity()": "Matrix3.Identity",
    "Quaternionr::Identity()": "Quaternion.Identity",
}


def _py_type(cpp):
    if not cpp:
        return None
    cpp = cpp.strip()
    if cpp in _TYPE_MAP:
        return _TYPE_MAP[cpp]
    if cpp.startswith("std::vector<") and cpp.endswith(">"):
        inner = cpp[len("std::vector<") : -1].strip()
        return f"list[{_py_type(inner) or inner}]"
    if cpp.startswith("shared_ptr<") and cpp.endswith(">"):
        return cpp[len("shared_ptr<") : -1].strip()
    return cpp


def _clean_desc(doc):
    if not doc:
        return ""
    s = _STRIP_MARKERS.sub("", doc)
    # YADE-specific cross-refs: :yref:`name<qualified::name>` → last segment
    s = _YREF.sub(lambda m: m.group(1).split("::")[-1], s)
    s = _YUPDATE.sub("(auto-updated)", s)
    # Generic rST: :ref:`label<target>` / :doc:`label<target>` → label
    s = _RST_REF.sub(lambda m: m.group(1) or "", s)
    s = _RST_DOC.sub(lambda m: m.group(1) or "", s)
    # ``inline literal`` → plain text (keep the value, drop the markers).
    s = _RST_INLINE_LITERAL.sub(lambda m: m.group(1), s)
    # [Foo2005]_ citation → (Foo 2005) so the reference still carries meaning.
    s = _RST_CITATION.sub(lambda m: f"({m.group(1)})", s)
    # Literal-escape artefacts like "O.timingEnabled\ ==\ True"
    s = re.sub(r"\s*\\\s*", " ", s)
    return " ".join(s.split()).strip()


def _clean_default(raw):
    if not raw:
        return ""
    v = raw.strip()
    for k, rep in _DEFAULT_ALIASES.items():
        v = v.replace(k, rep)
    return v


def _repr_default(v):
    try:
        r = repr(v)
    except Exception:
        return ""
    # Suppress unhelpful object reprs; caller decides what to do.
    if r.startswith("<") and r.endswith(">"):
        return ""
    if len(r) > 80:
        r = r[:77] + "..."
    return r


# --- runtime extraction --------------------------------------------------


_METHOD_ARG = re.compile(
    r"\(\s*(?P<type>[^)]+?)\s*\)\s*(?P<name>\w+)(?:\s*=\s*(?P<default>[^,\[\]]+))?"
)


def _parse_method_doc(raw):
    """Parse a boost.python method docstring.

    Format looks like::

        \\nfuncname( (Class)arg1, (int)id, (str)s='hi' [, (bool)q=False]) -> ReturnType :
            Free-form description that may span lines. A single method may
            have multiple overload blocks — we only parse the first.

    Returns a dict with name/args/returns/description, or None when the
    docstring doesn't match the boost.python convention (e.g. pure Python
    helpers added at module level).
    """
    if not raw:
        return None
    text = raw.lstrip()
    # funcname(
    open_paren = text.find("(")
    if open_paren < 1:
        return None
    name = text[:open_paren].strip()
    if not name.isidentifier():
        return None

    # Paren-matched scan to the closing ) of the signature.
    depth = 0
    i = open_paren
    end = -1
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    if end < 0:
        return None
    args_raw = text[open_paren + 1 : end]
    tail = text[end + 1 :].lstrip()

    ret = "None"
    if tail.startswith("->"):
        tail = tail[2:].lstrip()
        colon = tail.find(":")
        if colon >= 0:
            ret = tail[:colon].strip()
            tail = tail[colon + 1 :]
    elif tail.startswith(":"):
        tail = tail[1:]

    desc = _clean_desc(tail)

    args = []
    for am in _METHOD_ARG.finditer(args_raw):
        entry = {
            "name": am.group("name"),
            "type": _py_type(am.group("type")) or am.group("type"),
        }
        if am.group("default"):
            entry["default"] = am.group("default").strip()
        args.append(entry)
    # Drop the boost.python self-arg (first param is the class instance).
    if args and args[0]["name"] in ("arg1", "self"):
        args = args[1:]

    return {
        "name": name,
        "args": args,
        "returns": _py_type(ret) or ret,
        "description": desc,
    }


def _extract_attr_from_descriptor(name, klass, descr, cls, dict_defaults, inst):
    """Build one attribute entry from a property descriptor.

    dict_defaults / inst may be None when cls() failed — we still get
    type/default/flags from the :ydefault:/:yattrtype:/:yattrflags:
    markers embedded in the boost.python docstring, which is what
    YADE_CLASS_BASE_ATTRS_DEF emits for every serialized attr anyway.
    """
    raw = descr.__doc__ or ""
    flag_m = _FLAG.search(raw)
    type_m = _TYPE.search(raw)
    default_m = _DEFAULT.search(raw)

    flags = int(flag_m.group(1)) if flag_m else 0
    is_readonly = bool(flags & 2) or bool(_YUPDATE.search(raw))
    in_dict = dict_defaults is not None and name in dict_defaults
    is_nosave = bool(flags & 1) or (dict_defaults is not None and not in_dict)

    py_type = _py_type(type_m.group(1)) if type_m else None

    if default_m:
        default = _clean_default(default_m.group(1))
    elif in_dict:
        default = _repr_default(dict_defaults[name])
    elif inst is not None:
        try:
            default = _repr_default(getattr(inst, name))
        except Exception:
            default = ""
    else:
        default = ""

    if not py_type:
        if in_dict:
            try:
                py_type = type(dict_defaults[name]).__name__
            except Exception:
                py_type = "auto"
        elif inst is not None:
            try:
                py_type = type(getattr(inst, name)).__name__
            except Exception:
                py_type = "auto"
        else:
            py_type = "auto"

    attr = {
        "name": name,
        "type": py_type,
        "default": default,
        "description": _clean_desc(raw),
    }
    if is_readonly:
        attr["read_only"] = True
    if is_nosave:
        attr["no_save"] = True
    if klass is not cls:
        attr["inherited_from"] = klass.__name__
    return attr


def extract_class(cls, allow_instantiation=True):
    """Return (class_desc, attrs, methods).

    When ``allow_instantiation`` is False, the extractor does not call
    ``cls()`` at all — useful as a retry path after a segfault, where
    the property-descriptor docstrings are the only safe metadata
    source. Python-side ``:ydefault:/:yattrtype:/:yattrflags:`` markers
    cover every serialized attribute, so this path still produces
    complete output for normal YADE_CLASS_BASE_ATTRS_DEF classes.
    """
    class_desc = ""
    if cls.__doc__:
        for line in cls.__doc__.splitlines():
            line = line.strip()
            if line:
                class_desc = _clean_desc(line)
                break

    inst = None
    dict_defaults = None
    if allow_instantiation:
        try:
            inst = cls()
            dict_defaults = inst.dict()
        except Exception:
            pass

    # MRO walk: first-defining-class wins.
    prop_descriptors = {}
    method_descriptors = {}
    prop_order = []
    method_order = []
    try:
        mro = cls.__mro__
    except Exception:
        return class_desc, [], []

    for klass in mro:
        try:
            items = list(klass.__dict__.items())
        except Exception:
            continue
        for name, descr in items:
            if name.startswith("_"):
                continue
            if isinstance(descr, property):
                if name in prop_descriptors:
                    continue
                prop_descriptors[name] = (klass, descr)
                prop_order.append(name)
            elif callable(descr) and not isinstance(descr, type):
                if name in method_descriptors:
                    continue
                method_descriptors[name] = (klass, descr)
                method_order.append(name)

    attrs = []
    for name in prop_order:
        klass, descr = prop_descriptors[name]
        attrs.append(
            _extract_attr_from_descriptor(name, klass, descr, cls, dict_defaults, inst)
        )

    methods = []
    for name in method_order:
        klass, fn = method_descriptors[name]
        parsed = _parse_method_doc(fn.__doc__)
        if parsed is None:
            # callable with no parseable doc — emit a stub so we at
            # least record existence
            parsed = {"name": name, "args": [], "returns": "auto", "description": ""}
        if klass is not cls:
            parsed["inherited_from"] = klass.__name__
        methods.append(parsed)

    return class_desc, attrs, methods


# --- merge with existing JSON -------------------------------------------


def build_path_index(root):
    """Map ClassName → absolute path of the existing JSON file."""
    index = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".json") or f == "index.json":
                continue
            index[f[:-5]] = os.path.join(dirpath, f)
    return index


def _merge_list(runtime_items, existing_items, preserve_keys):
    """Merge a list of dicts keyed by name, runtime-authoritative.

    For each runtime item, carry the curated description across if the
    existing entry had one. `preserve_keys` are extra fields to copy from
    the prior entry when they aren't clobbered by the runtime entry.
    Curated descriptions get run through _clean_desc so that any rST
    artefacts (:ref:/[Foo2005]_/``literal``) that outlived the previous
    scraper's cleaner finally get normalised.
    """
    existing_by_name = {item.get("name"): item for item in existing_items or []}
    merged = []
    for rt in runtime_items:
        entry = dict(rt)
        prior = existing_by_name.get(rt.get("name"))
        if prior:
            prior_desc = prior.get("description") or ""
            runtime_desc = entry.get("description") or ""
            if prior_desc and len(prior_desc) >= len(runtime_desc) // 2:
                entry["description"] = _clean_desc(prior_desc)
            for k, v in prior.items():
                if k in preserve_keys and k not in entry:
                    entry[k] = v
        merged.append(entry)
    return merged


def merge(cls_name, runtime_desc, runtime_attrs, runtime_methods, existing):
    """Return a refreshed JSON doc dict."""
    out = dict(existing)
    out["name"] = cls_name

    if not out.get("description"):
        out["description"] = runtime_desc

    out["attributes"] = _merge_list(
        runtime_attrs,
        existing.get("attributes", []),
        preserve_keys=("examples", "see_also", "units", "notes"),
    )

    # Only rewrite methods if we actually extracted some — otherwise
    # keep whatever curated list was there.
    if runtime_methods:
        out["methods"] = _merge_list(
            runtime_methods,
            existing.get("methods", []),
            preserve_keys=("examples", "see_also", "notes"),
        )

    return out


# --- fork-isolated extraction -------------------------------------------
#
# Some YADE classes crash with segfaults on default construction (boundary
# controllers, clump bodies, etc.). Running every extraction in a forked
# child keeps a bad class from taking down the whole refresh pass — the
# parent just reaps the dead child and moves on. Fork is fine inside YADE
# because the child only reads the already-initialised process state and
# exits.

import pickle  # noqa: E402


def _extract_in_fork_once(cls, allow_instantiation, timeout_sec):
    """One forked attempt. Returns (desc, attrs, methods) or (_, None, _)
    if the child died.
    """
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # --- child ---
        os.close(r_fd)
        try:
            result = extract_class(cls, allow_instantiation=allow_instantiation)
            payload = pickle.dumps(result)
        except Exception as e:  # noqa: BLE001
            payload = pickle.dumps(("", None, f"extract raised: {e!r}"))
        try:
            os.write(w_fd, payload)
        except OSError:
            pass
        finally:
            os.close(w_fd)
            os._exit(0)

    # --- parent ---
    # Timeout via a threading.Timer — signal.alarm() doesn't work from the
    # bridge's worker thread (signals are main-thread only).
    import signal
    import threading

    os.close(w_fd)

    def _kill():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    killer = threading.Timer(timeout_sec, _kill)
    killer.daemon = True
    killer.start()
    try:
        chunks = []
        while True:
            try:
                chunk = os.read(r_fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            chunks.append(chunk)
        os.waitpid(pid, 0)
    finally:
        killer.cancel()
        os.close(r_fd)

    if not chunks:
        return "", None, []
    try:
        result = pickle.loads(b"".join(chunks))
    except Exception:
        return "", None, []
    if len(result) == 3:
        return result
    if len(result) == 2:
        return result[0], result[1], []
    return "", None, []


def extract_in_fork(cls, timeout_sec=10):
    """Extract with segfault-resilient retry.

    First attempt runs with ``allow_instantiation=True`` so we can pick
    up live type/default values from ``cls().dict()``. If the child
    dies (usually a segfault during ``cls()``), we retry with
    ``allow_instantiation=False`` — pure property-descriptor walk, no
    instance. Returns (desc, attrs, methods); attrs is None only when
    both attempts fail.
    """
    desc, attrs, methods = _extract_in_fork_once(cls, True, timeout_sec)
    if attrs is not None:
        return desc, attrs, methods
    return _extract_in_fork_once(cls, False, timeout_sec)


# --- driver -------------------------------------------------------------


def _clean_doc_inplace(doc):
    """Run _clean_desc over every description field of a loaded JSON doc.

    Used for orphan classes (documented but not present in this YADE build)
    so their descriptions still get the rST polish even though we can't
    regenerate their attribute list from runtime.
    """
    changed = False
    if doc.get("description"):
        new = _clean_desc(doc["description"])
        if new != doc["description"]:
            doc["description"] = new
            changed = True
    for key in ("attributes", "methods"):
        for item in doc.get(key, []) or []:
            desc = item.get("description")
            if desc:
                new = _clean_desc(desc)
                if new != desc:
                    item["description"] = new
                    changed = True
    return changed


def main():
    index = build_path_index(DOC_ROOT)
    sys.stdout.write(f"loaded {len(index)} existing JSONs from {DOC_ROOT}\n")
    sys.stdout.flush()

    seen_in_runtime = set()
    refreshed = 0
    skipped_no_json = 0
    skipped_crashed = 0
    errored = 0
    progress_every = 25
    processed = 0

    for name in sorted(dir(w)):
        if name.startswith("_"):
            continue
        cls = getattr(w, name)
        if not isinstance(cls, type):
            continue
        path = index.get(name)
        if not path:
            skipped_no_json += 1
            continue
        seen_in_runtime.add(name)

        rt_desc, rt_attrs, rt_methods = extract_in_fork(cls)
        processed += 1
        if processed % progress_every == 0:
            sys.stdout.write(
                f"  [{processed}] refreshed={refreshed} "
                f"crashed={skipped_crashed}\n"
            )
            sys.stdout.flush()

        if rt_attrs is None:
            skipped_crashed += 1
            continue
        try:
            with open(path) as fh:
                existing = json.load(fh)
            new_doc = merge(name, rt_desc, rt_attrs, rt_methods, existing)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(new_doc, fh, indent=2)
            os.replace(tmp, path)
            refreshed += 1
            # Heuristic: if no attr has a runtime-repr default (all defaults
            # came from docstring markers), the extractor likely took the
            # no-instantiation path. Only useful as a reporting statistic.
            if rt_attrs and not any("object at 0x" in str(a.get("default", "")) for a in rt_attrs):
                pass  # most classes fall here; can't cheaply distinguish
        except Exception:
            errored += 1
            sys.stdout.write(f"  ERROR merge {name}:\n{traceback.format_exc()}")

    # Orphan sweep: JSON docs for classes that don't exist in this build
    # (e.g. LevelSet / Foam / MPI / Subdomain features compiled out). We
    # can't refresh their attr list without a runtime class, but we can
    # at least run the description cleaner so rST artefacts don't linger.
    orphan_cleaned = 0
    for cls_name, path in index.items():
        if cls_name in seen_in_runtime:
            continue
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if _clean_doc_inplace(doc):
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(doc, fh, indent=2)
            os.replace(tmp, path)
            orphan_cleaned += 1

    sys.stdout.write(
        f"\nrefreshed:                 {refreshed}\n"
        f"orphan descriptions cleaned:{orphan_cleaned}\n"
        f"skipped (no JSON):         {skipped_no_json}\n"
        f"skipped (both forks died): {skipped_crashed}\n"
        f"errored:                   {errored}\n"
    )


if __name__ == "__main__":
    main()
