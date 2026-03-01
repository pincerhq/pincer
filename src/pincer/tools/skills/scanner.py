"""
Skill security scanner — static analysis to detect dangerous patterns.

Uses AST walking for call/import detection and regex for URL, filesystem,
and environment variable access patterns. Produces a score from 0-100
where higher is safer. Default pass threshold: 50.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.tools.skills.loader import SkillManifest

DANGEROUS_CALLS: dict[str, tuple[int, str]] = {
    "eval": (30, "critical"),
    "exec": (30, "critical"),
    "os.system": (25, "critical"),
    "os.popen": (25, "critical"),
    "subprocess.run": (20, "critical"),
    "subprocess.Popen": (20, "critical"),
    "subprocess.call": (20, "critical"),
    "subprocess.check_output": (15, "warning"),
    "subprocess.check_call": (15, "warning"),
    "__import__": (20, "critical"),
    "ctypes.cdll": (20, "critical"),
    "ctypes.CDLL": (20, "critical"),
    "compile": (15, "warning"),
    "pickle.loads": (15, "warning"),
    "shutil.rmtree": (15, "warning"),
    "shutil.move": (10, "warning"),
    "importlib.import_module": (10, "warning"),
    "open": (5, "info"),
}

DANGEROUS_IMPORTS: dict[str, tuple[int, str]] = {
    "subprocess": (15, "critical"),
    "ctypes": (20, "critical"),
    "pty": (20, "critical"),
    "code": (15, "critical"),
    "codeop": (15, "critical"),
    "pickle": (10, "warning"),
    "marshal": (10, "warning"),
    "socket": (10, "warning"),
    "http.server": (15, "critical"),
    "multiprocessing": (10, "warning"),
    "threading": (5, "info"),
}

FILESYSTEM_ESCAPE_PATTERNS = [
    (re.compile(r"\.\./"), "Parent directory traversal (../)"),
    (re.compile(r"/etc/"), "Access to /etc/"),
    (re.compile(r"/root/"), "Access to /root/"),
    (re.compile(r"/home/"), "Access to /home/"),
    (re.compile(r"~/\."), "Access to hidden dotfiles"),
    (re.compile(r"/proc/"), "Access to /proc/"),
    (re.compile(r"/sys/"), "Access to /sys/"),
    (re.compile(r"/dev/"), "Access to /dev/"),
]

_URL_PATTERN = re.compile(r"https?://([a-zA-Z0-9.-]+)")

_ENV_PATTERNS = [
    re.compile(r'os\.environ\[(["\'])(.+?)\1\]'),
    re.compile(r'os\.environ\.get\((["\'])(.+?)\1'),
    re.compile(r'os\.getenv\((["\'])(.+?)\1'),
]

_SAFE_ENV_VARS = {"PATH", "HOME", "LANG", "PYTHONPATH", "PINCER_DB_PATH"}


@dataclass
class Finding:
    """A single security finding from the scanner."""

    severity: str  # critical, warning, info
    category: str  # dangerous_call, dangerous_import, network, filesystem, env
    description: str
    line: int
    penalty: int


@dataclass
class ScanResult:
    """Result of scanning a skill."""

    skill_name: str
    score: int
    findings: list[Finding] = field(default_factory=list)
    passed: bool = True
    error: str | None = None

    def summary(self) -> str:
        """Human-readable scan report."""
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Scan: {self.skill_name} — {status} (score: {self.score}/100)"]
        if self.error:
            lines.append(f"  Error: {self.error}")
        for f in self.findings:
            icon = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}.get(
                f.severity, "?"
            )
            lines.append(
                f"  {icon} L{f.line}: [{f.category}] {f.description} (-{f.penalty})"
            )
        return "\n".join(lines)


def _resolve_call_name(node: ast.Call) -> str | None:
    """Resolve a Call node to a dotted name like 'os.system'."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = [func.attr]
        current = func.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def _is_string_literal_arg(node: ast.Call) -> bool:
    """Check if the first argument to a call is a string literal."""
    return bool(
        node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    )


class SkillScanner:
    """Static security scanner for skill source code."""

    def __init__(self, pass_threshold: int = 50) -> None:
        self._threshold = pass_threshold

    def scan_file(
        self,
        file_path: str,
        manifest: SkillManifest | None = None,
    ) -> ScanResult:
        """Scan a skill.py file and return a ScanResult."""
        skill_name = manifest.name if manifest else "unknown"
        try:
            with open(file_path, encoding="utf-8") as f:
                source = f.read()
        except FileNotFoundError:
            return ScanResult(
                skill_name=skill_name, score=0, passed=False,
                error=f"File not found: {file_path}",
            )

        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as e:
            return ScanResult(
                skill_name=skill_name, score=0, passed=False,
                error=f"Syntax error: {e}",
            )

        findings: list[Finding] = []

        self._check_calls(tree, findings)
        self._check_imports(tree, findings)
        self._check_network(source, manifest, findings)
        self._check_filesystem(source, findings)
        self._check_env_access(source, manifest, findings)

        total_penalty = sum(f.penalty for f in findings)
        score = max(0, 100 - total_penalty)
        passed = score >= self._threshold

        return ScanResult(
            skill_name=skill_name,
            score=score,
            findings=findings,
            passed=passed,
        )

    def scan_directory(self, dir_path: str, manifest: SkillManifest | None = None) -> ScanResult:
        """Scan a skill directory (finds skill.py inside)."""
        from pathlib import Path
        skill_py = Path(dir_path) / "skill.py"
        if not skill_py.is_file():
            name = manifest.name if manifest else "unknown"
            return ScanResult(
                skill_name=name, score=0, passed=False,
                error=f"No skill.py found in {dir_path}",
            )

        if manifest is None:
            manifest_path = Path(dir_path) / "manifest.json"
            if manifest_path.is_file():
                import json

                from pincer.tools.skills.loader import SkillManifest

                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = SkillManifest.from_dict(data, dir_path)
                except Exception:
                    pass

        return self.scan_file(str(skill_py), manifest=manifest)

    def _check_calls(self, tree: ast.AST, findings: list[Finding]) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _resolve_call_name(node)
            if name is None:
                continue
            entry = DANGEROUS_CALLS.get(name)
            if entry is None:
                continue
            penalty, severity = entry
            extra = ""
            if name in ("eval", "exec") and _is_string_literal_arg(node):
                penalty += 10
                extra = " with string literal argument"
            findings.append(Finding(
                severity=severity,
                category="dangerous_call",
                description=f"Call to {name}(){extra}",
                line=getattr(node, "lineno", 0),
                penalty=penalty,
            ))

    def _check_imports(self, tree: ast.AST, findings: list[Finding]) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    entry = DANGEROUS_IMPORTS.get(top)
                    if entry is None and alias.name in DANGEROUS_IMPORTS:
                        entry = DANGEROUS_IMPORTS[alias.name]
                    if entry:
                        penalty, severity = entry
                        findings.append(Finding(
                            severity=severity,
                            category="dangerous_import",
                            description=f"Import of '{alias.name}'",
                            line=getattr(node, "lineno", 0),
                            penalty=penalty,
                        ))
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                entry = DANGEROUS_IMPORTS.get(top)
                if entry is None and node.module in DANGEROUS_IMPORTS:
                    entry = DANGEROUS_IMPORTS[node.module]
                if entry:
                    penalty, severity = entry
                    findings.append(Finding(
                        severity=severity,
                        category="dangerous_import",
                        description=f"Import from '{node.module}'",
                        line=getattr(node, "lineno", 0),
                        penalty=penalty,
                    ))

    def _check_network(
        self, source: str, manifest: SkillManifest | None, findings: list[Finding]
    ) -> None:
        declared_domains: set[str] = set()
        if manifest:
            for perm in manifest.permissions:
                if perm.startswith("network:"):
                    declared_domains.add(perm.split(":", 1)[1])

        seen_domains: set[str] = set()
        for line_num, line in enumerate(source.splitlines(), 1):
            for match in _URL_PATTERN.finditer(line):
                domain = match.group(1)
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                if self._domain_is_declared(domain, declared_domains):
                    continue
                findings.append(Finding(
                    severity="warning",
                    category="network",
                    description=f"Undeclared network access to '{domain}'",
                    line=line_num,
                    penalty=10,
                ))

    def _check_filesystem(self, source: str, findings: list[Finding]) -> None:
        for line_num, line in enumerate(source.splitlines(), 1):
            for pattern, desc in FILESYSTEM_ESCAPE_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(
                        severity="warning",
                        category="filesystem",
                        description=desc,
                        line=line_num,
                        penalty=10,
                    ))

    def _check_env_access(
        self, source: str, manifest: SkillManifest | None, findings: list[Finding]
    ) -> None:
        declared_vars: set[str] = set()
        if manifest:
            declared_vars.update(manifest.env_required)
        declared_vars.update(_SAFE_ENV_VARS)

        for line_num, line in enumerate(source.splitlines(), 1):
            for pat in _ENV_PATTERNS:
                for match in pat.finditer(line):
                    var_name = match.group(2)
                    if var_name not in declared_vars:
                        findings.append(Finding(
                            severity="warning",
                            category="env",
                            description=f"Undeclared env access: '{var_name}'",
                            line=line_num,
                            penalty=5,
                        ))

    @staticmethod
    def _domain_is_declared(domain: str, declared: set[str]) -> bool:
        if "*" in declared:
            return True
        return any(domain == d or domain.endswith("." + d) for d in declared)
