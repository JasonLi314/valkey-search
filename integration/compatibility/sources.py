import ast
import hashlib
import os

_COMPAT_DIR = os.path.dirname(os.path.abspath(__file__))


def _module_file(package_dir, parts):
    """File for module `parts` under `package_dir`, or None if it is not a module."""
    stem = os.path.join(package_dir, *parts)
    for candidate in (stem + ".py", os.path.join(stem, "__init__.py")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _below_package(dotted, compat_dir):
    """Module parts of `dotted` below the package, or None if it is another package."""
    parts = dotted.split(".")
    return parts[1:] if parts[0] == os.path.basename(compat_dir) else None


def _import_targets(node, path, compat_dir):
    """Candidate files named by one import statement."""
    if isinstance(node, ast.Import):
        parts = [_below_package(a.name, compat_dir) for a in node.names]
        return [_module_file(compat_dir, p) for p in parts if p is not None]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        package_dir = os.path.dirname(path)
        for _ in range(node.level - 1):
            package_dir = os.path.dirname(package_dir)
        parts = node.module.split(".") if node.module else []
    else:
        package_dir, parts = compat_dir, _below_package(node.module, compat_dir)
        if parts is None:
            return []
    targets = []
    for alias in node.names:
        target = _module_file(package_dir, parts + [alias.name])
        targets.append(target if target is not None else _module_file(package_dir, parts))
    return targets


def _local_imports(path, compat_dir):
    """Files under `compat_dir` that `path` imports, relatively or by package name."""
    with open(path, "rb") as f:
        tree = ast.parse(f.read(), path)
    found = set()
    for node in ast.walk(tree):
        for target in _import_targets(node, path, compat_dir):
            if target is not None and target.startswith(compat_dir + os.sep):
                found.add(os.path.relpath(target, compat_dir))
    return found


def sources_for(generator, compat_dir=_COMPAT_DIR):
    """The generator plus every module here it imports, transitively, sorted."""
    closure, todo = set(), [generator]
    while todo:
        rel = todo.pop()
        if rel in closure:
            continue
        closure.add(rel)
        todo.extend(_local_imports(os.path.join(compat_dir, rel), compat_dir))
    return sorted(closure)


def compute_sources_hash(generator):
    """SHA256 of the generator's sources; stored in its pickle and checked on replay."""
    h = hashlib.sha256()
    for rel in sources_for(generator):
        h.update(rel.replace(os.sep, "/").encode("utf-8"))
        h.update(b"\0")
        with open(os.path.join(_COMPAT_DIR, rel), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()
