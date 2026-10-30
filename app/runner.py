"""Run a repo's test suite against a set of file overrides and report per-test results.

LocalRunner runs pytest in a subprocess with a timeout and reads per-test results
from JUnit XML. make_runner() is the single switch point for an isolated backend
such as Nebius Token Factory Sandboxes; tarball() packages repo + overrides for upload.
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

TIMEOUT = 90
PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "--junitxml=/tmp/report.xml", "-o", "junit_family=xunit1"]


def parse_junit(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    passed, failed, details = [], [], {}
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name')}"
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        if case.find("skipped") is not None:
            continue
        if bad is not None:
            failed.append(name)
            details[name] = ((bad.get("message") or "") + "\n" + (bad.text or ""))[-900:]
        else:
            passed.append(name)
    return {"passed": passed, "failed": failed, "details": details}


class LocalRunner:
    name = "local"

    def run(self, root: Path, overrides: dict[str, str], tests: str = ".") -> dict:
        t0 = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(root, work)
            for path, content in overrides.items():
                (work / path).parent.mkdir(parents=True, exist_ok=True)
                (work / path).write_text(content)
            report = Path(tmp) / "report.xml"
            args = [a.replace("/tmp/report.xml", str(report)) for a in PYTEST_ARGS]
            env = {**os.environ, "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
            try:
                proc = subprocess.run([sys.executable, "-m", "pytest", *args, tests], cwd=work, env=env,
                                      capture_output=True, text=True, timeout=TIMEOUT)
                out = proc.stdout[-3000:]
            except subprocess.TimeoutExpired:
                return {"error": "Tests timed out", "passed": [], "failed": [], "details": {}, "runner": self.name}
            if not report.exists():
                return {"error": out or proc.stderr[-2000:], "passed": [], "failed": [], "details": {}, "runner": self.name}
            res = parse_junit(report.read_text())
        return {**res, "output": out, "seconds": round(time.time() - t0, 2), "runner": self.name}


def make_runner():
    """Single switch point for the test backend (Nebius Sandboxes will plug in here)."""
    return LocalRunner()


def tarball(root: Path, overrides: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and rel not in overrides:
                tar.add(p, arcname=f"repo/{rel}")
        for rel, content in overrides.items():
            data = content.encode()
            info = tarfile.TarInfo(f"repo/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
