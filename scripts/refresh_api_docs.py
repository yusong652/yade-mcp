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

_TYPE_MAP = {
    "Real": "float",
    "int": "int",
    "unsigned int": "int",
    "size_t": "int",
    "long": "int",
    "bool": "bool",
    "Vector3r": "Vector3",
    "Vector2r": "Vector2",
    "Vector3i": "Vector3i",
    "Vector2i": "Vector2i",
    "Vector6r": "Vector6",
    "Matrix3r": "Matrix3",
    "Matrix6r": "Matrix6",
    "Quaternionr": "Quaternion",
    "std::string": "str",
    "string": "str",
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
    s = _YREF.sub(lambda m: m.group(1).split("::")[-1], s)
    s = _YUPDATE.sub("(auto-updated)", s)
    # collapse literal-escape artefacts like "O.timingEnabled\ ==\ ``True``"
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


def extract_class(cls):
    """Return (class_desc, list[attr_dict]) or (class_desc, None).

    None signals the class can't be default-constructed and should be
    left alone by the refresh pass.
    """
    class_desc = ""
    if cls.__doc__:
        for line in cls.__doc__.splitlines():
            line = line.strip()
            if line:
                class_desc = _clean_desc(line)
                break

    try:
        inst = cls()
        dict_defaults = inst.dict()
    except Exception:
        return class_desc, None

    # MRO walk: first-defining-class wins.
    descriptors = {}
    order = []
    for klass in cls.__mro__:
        for name, descr in klass.__dict__.items():
            if name.startswith("_") or name in descriptors:
                continue
            if not isinstance(descr, property):
                continue
            descriptors[name] = (klass, descr)
            order.append(name)

    out = []
    for name in order:
        klass, descr = descriptors[name]
        raw = descr.__doc__ or ""
        flag_m = _FLAG.search(raw)
        type_m = _TYPE.search(raw)
        default_m = _DEFAULT.search(raw)

        flags = int(flag_m.group(1)) if flag_m else 0
        is_readonly = bool(flags & 2) or bool(_YUPDATE.search(raw))
        is_nosave = bool(flags & 1) or (name not in dict_defaults)

        py_type = _py_type(type_m.group(1)) if type_m else None

        if default_m:
            default = _clean_default(default_m.group(1))
        elif name in dict_defaults:
            default = _repr_default(dict_defaults[name])
        else:
            try:
                default = _repr_default(getattr(inst, name))
            except Exception:
                default = ""

        if not py_type:
            try:
                v = dict_defaults[name] if name in dict_defaults else getattr(inst, name)
                py_type = type(v).__name__
            except Exception:
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
        out.append(attr)

    return class_desc, out


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


def merge(cls_name, runtime_desc, runtime_attrs, existing):
    """Return a refreshed JSON doc dict."""
    out = dict(existing)  # shallow copy preserves key order + extra fields
    out["name"] = cls_name

    # Description: prefer curated non-empty value, fall back to runtime.
    if not out.get("description"):
        out["description"] = runtime_desc

    # Attributes: runtime-authoritative; carry curated descriptions forward.
    existing_by_name = {a.get("name"): a for a in existing.get("attributes", [])}
    merged_attrs = []
    for rt in runtime_attrs:
        prior = existing_by_name.get(rt["name"])
        attr = dict(rt)
        if prior:
            prior_desc = prior.get("description") or ""
            # Keep curated description if it's non-trivially richer than runtime.
            if prior_desc and len(prior_desc) >= len(attr["description"]) // 2:
                attr["description"] = prior_desc
            # Preserve any non-reserved extras the curation layer added.
            for k, v in prior.items():
                if k not in attr and k not in ("name", "type", "default", "description"):
                    attr[k] = v
        merged_attrs.append(attr)
    out["attributes"] = merged_attrs
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


def extract_in_fork(cls, timeout_sec=10):
    """Run extract_class(cls) in a forked child.

    Returns (class_desc, attrs) on success, ("", None) if the child died
    (segfault / timeout) or the class isn't default-constructable.
    """
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # --- child ---
        os.close(r_fd)
        try:
            result = extract_class(cls)
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
        return "", None
    try:
        result = pickle.loads(b"".join(chunks))
    except Exception:
        return "", None
    if len(result) == 3:  # extract raised inside child
        return result[0], None
    return result


# --- driver -------------------------------------------------------------


def main():
    index = build_path_index(DOC_ROOT)
    sys.stdout.write(f"loaded {len(index)} existing JSONs from {DOC_ROOT}\n")
    sys.stdout.flush()

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

        rt_desc, rt_attrs = extract_in_fork(cls)
        processed += 1
        if processed % progress_every == 0:
            sys.stdout.write(
                f"  [{processed}] refreshed={refreshed} "
                f"crashed={skipped_crashed} errored={errored}\n"
            )
            sys.stdout.flush()

        if rt_attrs is None:
            skipped_crashed += 1
            continue
        try:
            with open(path) as fh:
                existing = json.load(fh)
            new_doc = merge(name, rt_desc, rt_attrs, existing)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(new_doc, fh, indent=2)
            os.replace(tmp, path)
            refreshed += 1
        except Exception:
            errored += 1
            sys.stdout.write(f"  ERROR merge {name}:\n{traceback.format_exc()}")

    sys.stdout.write(
        f"\nrefreshed:                 {refreshed}\n"
        f"skipped (no JSON):         {skipped_no_json}\n"
        f"skipped (cls crashed):     {skipped_crashed}\n"
        f"errored:                   {errored}\n"
    )


if __name__ == "__main__":
    main()
