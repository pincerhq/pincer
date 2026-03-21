"""
AST security scanner for Pincer skills and MCP packages.

Scans Python source files for dangerous patterns before installation.
Returns a safety score (0–100) and a list of flagged findings.

Score interpretation:
  >= 80  Safe to install
  40–79  Warning — confirm before installing
  < 40   Blocked — critical risk
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("pincer.skills.scanner")

# ── Finding categories ────────────────────────────────────────────────────────


class RiskLevel(StrEnum):
    CRITICAL = "critical"   # Block install (score -= 40)
    HIGH = "high"           # Strong warning (score -= 25)
    MEDIUM = "medium"       # Warn (score -= 10)
    INFO = "info"           # Informational (score -= 0)


@dataclass
class ScanFinding:
    rule: str
    risk: RiskLevel
    message: str
    file: str = ""
    line: int = 0

    @property
    def score_penalty(self) -> int:
        return {
            RiskLevel.CRITICAL: 40,
            RiskLevel.HIGH: 25,
            RiskLevel.MEDIUM: 10,
            RiskLevel.INFO: 0,
        }[self.risk]


@dataclass
class ScanResult:
    score: int
    findings: list[ScanFinding] = field(default_factory=list)
    files_scanned: int = 0
    error: str | None = None

    @property
    def safe(self) -> bool:
        return self.score >= 80

    @property
    def blocked(self) -> bool:
        return self.score < 40

    def summary(self) -> str:
        counts = {level: 0 for level in RiskLevel}
        for f in self.findings:
            counts[f.risk] += 1
        parts = []
        if counts[RiskLevel.CRITICAL]:
            parts.append(f"{counts[RiskLevel.CRITICAL]} critical")
        if counts[RiskLevel.HIGH]:
            parts.append(f"{counts[RiskLevel.HIGH]} high")
        if counts[RiskLevel.MEDIUM]:
            parts.append(f"{counts[RiskLevel.MEDIUM]} medium")
        if not parts:
            return f"Score {self.score}/100 — no issues found"
        return f"Score {self.score}/100 — {', '.join(parts)}"


# ── AST visitor ───────────────────────────────────────────────────────────────


class _SecurityVisitor(ast.NodeVisitor):
    """Walk a Python AST and collect security findings."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.findings: list[ScanFinding] = []

    def _add(self, rule: str, risk: RiskLevel, msg: str, node: ast.AST) -> None:
        self.findings.append(ScanFinding(
            rule=rule,
            risk=risk,
            message=msg,
            file=self.filename,
            line=getattr(node, "lineno", 0),
        ))

    # ── eval / exec / compile ─────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name in ("eval", "exec", "compile"):
            self._add(
                "dynamic_exec",
                RiskLevel.CRITICAL,
                f"Dynamic code execution: {name}()",
                node,
            )
        elif name == "__import__":
            self._add("dynamic_import", RiskLevel.HIGH, "Dynamic import via __import__()", node)
        elif (
            name in ("system", "popen")
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            self._add(
                "os_shell",
                RiskLevel.HIGH,
                f"Shell execution: os.{name}()",
                node,
            )
        self.generic_visit(node)

    # ── subprocess ────────────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "subprocess":
                self._add("subprocess_import", RiskLevel.HIGH, "Imports subprocess module", node)
            elif alias.name == "socket":
                self._add("socket_import", RiskLevel.HIGH, "Imports socket module", node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        if module == "subprocess" or module.startswith("subprocess."):
            self._add("subprocess_import", RiskLevel.HIGH, f"Imports from subprocess: {module}", node)
        elif module == "socket":
            self._add("socket_import", RiskLevel.HIGH, "Imports from socket module", node)
        elif module == "os" and any(a.name in ("system", "popen", "execv", "execvp") for a in node.names):
            self._add("os_exec_import", RiskLevel.HIGH, f"Imports shell execution from os: {module}", node)
        self.generic_visit(node)

    # ── os.environ / os.getenv ────────────────────────────────────────────────

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if (
            node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            self._add("env_access", RiskLevel.MEDIUM, "Accesses os.environ (potential credential theft)", node)
        self.generic_visit(node)


# ── Scanner ───────────────────────────────────────────────────────────────────


class PackageScanner:
    """
    Scans a directory or single file of Python source code.

    Usage::

        scanner = PackageScanner()
        result = scanner.scan_path(Path("/tmp/my-package"))
        if result.blocked:
            print("Blocked:", result.summary())
    """

    def scan_path(self, path: Path, max_files: int = 200) -> ScanResult:
        """Recursively scan all Python files under *path*."""
        files = [path] if path.is_file() else list(path.rglob("*.py"))[:max_files]

        if not files:
            return ScanResult(score=100, files_scanned=0)

        all_findings: list[ScanFinding] = []
        for py_file in files:
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            except Exception as e:
                logger.debug("Scanner error on %s: %s", py_file, e)
                continue

            visitor = _SecurityVisitor(str(py_file.name))
            visitor.visit(tree)
            all_findings.extend(visitor.findings)

        # Deduplicate by (rule, file) to avoid noise from repeated patterns
        seen: set[tuple[str, str]] = set()
        deduped: list[ScanFinding] = []
        for f in all_findings:
            key = (f.rule, f.file)
            if key not in seen:
                seen.add(key)
                deduped.append(f)

        score = max(0, 100 - sum(f.score_penalty for f in deduped))
        return ScanResult(score=score, findings=deduped, files_scanned=len(files))

    def scan_source(self, source: str, filename: str = "<string>") -> ScanResult:
        """Scan a single Python source string."""
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            return ScanResult(score=50, error=str(e))

        visitor = _SecurityVisitor(filename)
        visitor.visit(tree)
        score = max(0, 100 - sum(f.score_penalty for f in visitor.findings))
        return ScanResult(score=score, findings=visitor.findings, files_scanned=1)
