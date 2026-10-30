"""Fetch a GitHub repo and build a static index of it with `ast` (no code is executed here)."""
import ast
import io
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

CACHE = Path(os.getenv("REPO_CACHE", Path(__file__).resolve().parent.parent / "cache"))
SKIP_DIRS = {".git", "docs", "doc", "examples", "bench", "benchmarks", ".github", "build", "dist"}
MAX_PY_BYTES = 600_000

# Repos whose test suites are known to run quickly with no extra dependencies.
CURATED = {
    "dbader/schedule": {"title": "schedule", "blurb": "Python job scheduling for humans (81 tests)", "tests": "."},
    "pytoolz/toolz": {"title": "toolz", "blurb": "Functional utilities for iterables, functions and dicts (192 tests)", "tests": "toolz"},
}


@dataclass
class Func:
    name: str
    qualname: str
    path: str
    start: int
    end: int
    doc: str
    signature: str


@dataclass
class Repo:
    slug: str
    root: Path
    files: dict[str, str] = field(default_factory=dict)       # path -> source (Python only)
    other: list[str] = field(default_factory=list)            # non-Python paths, for the tree
    readme: str = ""
    funcs: list[Func] = field(default_factory=list)
    classes: dict[str, tuple[str, int]] = field(default_factory=dict)  # name -> (path, line)
    imports: dict[str, set[str]] = field(default_factory=dict)  # module path -> imported module paths
    tests_dir: str = "."

    def is_test(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}"

    @property
    def test_files(self):
        return [p for p in self.files if self.is_test(p)]

    @property
    def source_files(self):
        return [p for p in self.files if not self.is_test(p)]


def parse_slug(text: str) -> str:
    m = re.search(r"(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", text.strip())
    if not m:
        raise ValueError("Enter a GitHub repo like owner/name or https://github.com/owner/name")
    return f"{m.group(1)}/{m.group(2)}"


def fetch(slug: str) -> Path:
    dest = CACHE / slug.replace("/", "__")
    if dest.exists():
        return dest
    r = httpx.get(f"https://api.github.com/repos/{slug}/tarball", follow_redirects=True, timeout=60)
    if r.status_code != 200:
        raise ValueError(f"Couldn't download {slug} from GitHub ({r.status_code}).")
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not (m.isfile() or m.isdir()) or ".." in parts[1]:
                continue
            m.name = parts[1]
            tar.extract(m, dest, filter="data")
    return dest


def load(slug: str) -> Repo:
    root = fetch(slug)
    repo = Repo(slug=slug, root=root, tests_dir=CURATED.get(slug, {}).get("tests", "."))
    total = 0
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or set(rel.split("/")[:-1]) & SKIP_DIRS or rel.startswith("."):
            continue
        if p.suffix == ".py" and rel != "setup.py" and not rel.endswith("conf.py"):
            src = p.read_text(errors="replace")
            total += len(src)
            repo.files[rel] = src
        else:
            repo.other.append(rel)
        if p.name.lower().startswith("readme") and not repo.readme:
            repo.readme = p.read_text(errors="replace")[:3000]
    if not repo.files:
        raise ValueError("This repo has no Python files.")
    if total > MAX_PY_BYTES:
        raise ValueError("This repo is too large for a 60-minute exercise. Try a smaller one.")
    index(repo)
    return repo


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[:-9] if mod.endswith(".__init__") else mod


def index(repo: Repo):
    modules = {module_name(p): p for p in repo.files}
    for path, src in repo.files.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = module_name(path).split(".")
                    pkg = pkg[: len(pkg) - node.level + (1 if path.endswith("__init__.py") else 0)]
                    base = ".".join(pkg + ([base] if base else []))
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            else:
                continue
            for n in names:
                while n:
                    if n in modules and modules[n] != path:
                        deps.add(modules[n])
                        break
                    n = n.rpartition(".")[0]
        repo.imports[path] = deps
        if repo.is_test(path):
            continue
        for node in tree.body:
            targets = [(node, None)]
            if isinstance(node, ast.ClassDef):
                repo.classes[node.name] = (path, node.lineno)
                targets = [(n, node.name) for n in node.body]
            for fn, cls in targets:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = (ast.get_docstring(fn) or "").strip().split("\n")[0][:120]
                    args = ", ".join(a.arg for a in fn.args.args)
                    repo.funcs.append(Func(fn.name, f"{cls}.{fn.name}" if cls else fn.name, path,
                                           fn.lineno, fn.end_lineno, doc, f"{fn.name}({args})"))


def outline(repo: Repo, limit: int = 14000) -> str:
    """Compact text map of the repo for prompts: files, sizes, functions and imports."""
    lines = [f"Repository: {repo.slug}", "", "README (start):", repo.readme[:1200], "", "Source files:"]
    for path in repo.source_files:
        n = repo.files[path].count("\n") + 1
        deps = ", ".join(sorted(repo.imports.get(path, ()))) or "-"
        lines.append(f"\n{path} ({n} lines; imports: {deps})")
        for f in repo.funcs:
            if f.path == path:
                lines.append(f"  L{f.start}-{f.end} {f.qualname}({f.signature.split('(', 1)[1]}  {f.doc}")
    lines.append("\nTest files: " + ", ".join(repo.test_files))
    return "\n".join(lines)[:limit]


def tested_functions(repo: Repo) -> list[tuple[Func, int]]:
    """Functions that the test suite mentions by name, with a mention count."""
    tests = "\n".join(repo.files[p] for p in repo.test_files)
    out = []
    for f in repo.funcs:
        if f.name.startswith("__") or not (4 <= f.end - f.start <= 45):
            continue
        hits = len(re.findall(rf"\b{re.escape(f.name)}\b", tests))
        if hits:
            out.append((f, hits))
    return sorted(out, key=lambda x: -x[1])


def reverse_deps(repo: Repo, path: str) -> list[str]:
    """Every file that imports `path`, directly or transitively."""
    seen, frontier = set(), {path}
    while frontier:
        nxt = {p for p, deps in repo.imports.items() if deps & frontier and p not in seen and p != path}
        seen |= nxt
        frontier = nxt
    return sorted(seen)


def numbered(src: str, start: int = 1, end: int | None = None) -> str:
    lines = src.split("\n")
    end = end or len(lines)
    return "\n".join(f"{i:>4}  {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
