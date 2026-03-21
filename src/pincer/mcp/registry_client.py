"""
MCP Registry Client — discover and install MCP servers.

Queries the official MCP Registry and ClawHub for available servers,
normalizes results, optionally scans packages before install, and
generates pincer.toml config entries.

Registry URLs:
  MCP Registry: https://registry.modelcontextprotocol.io/api/v1/
  ClawHub:      https://clawhub.io/api/v1/   (OpenClaw skill registry)
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pincer.mcp.config import MCPServerConfig, MCPTransport

logger = logging.getLogger("pincer.mcp.registry")

_MCP_REGISTRY_URL = "https://registry.modelcontextprotocol.io/api/v1"
_CLAWHUB_URL = "https://clawhub.io/api/v1"

# Minimum scan score required to proceed without user confirmation
_SCORE_WARN_THRESHOLD = 80
# Minimum scan score to allow install at all
_SCORE_BLOCK_THRESHOLD = 40


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class MCPRegistryEntry:
    """Normalized entry from any MCP package registry."""

    package_name: str          # e.g. "@modelcontextprotocol/server-github"
    name: str                  # Config-friendly name, e.g. "github"
    description: str
    registry: str              # "mcp" | "clawhub"
    transport: MCPTransport = MCPTransport.STDIO

    # Package manager and invocation
    package_manager: str = "npm"  # "npm" | "pip"
    command: str = "npx"
    args: list[str] = field(default_factory=list)

    # Environment vars required (inferred from metadata)
    required_env: list[str] = field(default_factory=list)

    # Raw metadata from registry
    version: str = ""
    homepage: str = ""
    downloads: int = 0


# ── Registry client ───────────────────────────────────────────────────────────


class MCPRegistryClient:
    """
    Discovers, evaluates, and installs MCP servers from public registries.

    Usage::

        client = MCPRegistryClient()
        results = await client.search("github")
        for entry in results:
            print(entry.name, entry.description)

        config = await client.install("@modelcontextprotocol/server-github")
    """

    def __init__(self, staging_dir: Path | None = None) -> None:
        self._staging = staging_dir or (Path.home() / ".pincer" / "mcp" / "packages")

    async def search(
        self,
        query: str,
        registry: str = "all",
    ) -> list[MCPRegistryEntry]:
        """
        Search MCP Registry and/or ClawHub for matching servers.

        registry: "mcp" | "clawhub" | "all"
        Returns results sorted by relevance (downloads desc).
        """
        results: list[MCPRegistryEntry] = []

        try:
            import httpx
        except ImportError:
            logger.warning("httpx required for registry search (already a core dep)")
            return results

        async with httpx.AsyncClient(timeout=10.0) as client:
            if registry in ("mcp", "all"):
                mcp_results = await self._search_mcp_registry(client, query)
                results.extend(mcp_results)

            if registry in ("clawhub", "all"):
                clawhub_results = await self._search_clawhub(client, query)
                results.extend(clawhub_results)

        # Sort by downloads descending
        results.sort(key=lambda e: e.downloads, reverse=True)
        return results

    async def fetch_metadata(self, package_name: str) -> MCPRegistryEntry | None:
        """Fetch full metadata for a specific package."""
        try:
            import httpx
        except ImportError:
            return None

        encoded = package_name.lstrip("@").replace("/", "%2F")
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try MCP registry first
            try:
                resp = await client.get(f"{_MCP_REGISTRY_URL}/servers/{encoded}")
                if resp.status_code == 200:
                    return self._parse_mcp_entry(resp.json())
            except Exception:
                pass

            # Try ClawHub
            try:
                resp = await client.get(f"{_CLAWHUB_URL}/packages/{encoded}")
                if resp.status_code == 200:
                    return self._parse_clawhub_entry(resp.json())
            except Exception:
                pass

        return None

    async def install(
        self,
        package_name: str,
        scan: bool = True,
        name_override: str | None = None,
    ) -> tuple[MCPServerConfig, dict[str, Any]]:
        """
        Download, scan, and generate a config for an MCP server package.

        Returns (MCPServerConfig, scan_info_dict).
        Raises ValueError if scan score is below block threshold.
        """
        # Resolve metadata
        entry = await self.fetch_metadata(package_name)
        if entry is None:
            # Build a best-effort entry from the package name
            entry = _infer_entry(package_name)

        if name_override:
            entry.name = name_override

        # Scan before generating config
        scan_info: dict[str, Any] = {"score": 100, "findings": [], "skipped": not scan}
        if scan:
            scan_result = await self._scan_package(entry)
            scan_info = {
                "score": scan_result.score,
                "findings": [
                    {"rule": f.rule, "risk": f.risk, "message": f.message, "file": f.file}
                    for f in scan_result.findings
                ],
                "skipped": False,
                "summary": scan_result.summary(),
            }
            if scan_result.blocked:
                raise ValueError(
                    f"Install blocked: scan score {scan_result.score}/100 — {scan_result.summary()}"
                )

        config = self._generate_config(entry)
        logger.info("MCP install: generated config for '%s' (scan score: %d)", entry.name, scan_info["score"])
        return config, scan_info

    def uninstall(self, server_name: str, config_path: Path | None = None) -> bool:
        """
        Remove a server from pincer.toml and clean up staging files.

        Returns True if the entry was found and removed, False if not found.
        """
        toml_path = (config_path or Path.cwd()) / "pincer.toml"
        if not toml_path.exists():
            return False

        text = toml_path.read_text()
        # Find and remove the [[mcp.servers]] block for this server name
        lines = text.splitlines(keepends=True)
        new_lines: list[str] = []
        removed = False

        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == "[[mcp.servers]]":
                # Look ahead to check if this block matches
                block: list[str] = [line]
                j = i + 1
                while j < len(lines) and not lines[j].strip().startswith("[["):
                    block.append(lines[j])
                    j += 1
                block_text = "".join(block)
                if f'name = "{server_name}"' in block_text or f"name = '{server_name}'" in block_text:
                    removed = True
                    i = j  # Skip the entire block
                    continue
                else:
                    new_lines.extend(block)
                    i = j
                    continue
            new_lines.append(line)
            i += 1

        if removed:
            toml_path.write_text("".join(new_lines))

        # Clean staging files
        staging_path = self._staging / server_name
        if staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)

        return removed

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _search_mcp_registry(
        self, client: Any, query: str
    ) -> list[MCPRegistryEntry]:
        try:
            resp = await client.get(
                f"{_MCP_REGISTRY_URL}/servers",
                params={"search": query, "limit": 20},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data if isinstance(data, list) else data.get("servers", data.get("results", []))
            return [self._parse_mcp_entry(item) for item in items if item]
        except Exception as exc:
            logger.debug("MCP registry search error: %s", exc)
            return []

    async def _search_clawhub(
        self, client: Any, query: str
    ) -> list[MCPRegistryEntry]:
        try:
            resp = await client.get(
                f"{_CLAWHUB_URL}/packages",
                params={"q": query, "type": "mcp", "limit": 20},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data if isinstance(data, list) else data.get("packages", data.get("results", []))
            return [self._parse_clawhub_entry(item) for item in items if item]
        except Exception as exc:
            logger.debug("ClawHub search error: %s", exc)
            return []

    def _parse_mcp_entry(self, raw: dict[str, Any]) -> MCPRegistryEntry:
        pkg = raw.get("qualifiedName", raw.get("name", ""))
        name = _to_config_name(raw.get("name", pkg))
        runtime = raw.get("runtime", "node")
        pm, cmd = ("npm", "npx") if runtime in ("node", "nodejs") else ("pip", "python")
        args = raw.get("args", [])
        if pm == "npm" and not args:
            args = ["-y", pkg] if pkg else []
        elif pm == "pip" and not args:
            args = ["-m", name]
        return MCPRegistryEntry(
            package_name=pkg,
            name=name,
            description=raw.get("description", ""),
            registry="mcp",
            transport=MCPTransport.STDIO,
            package_manager=pm,
            command=cmd,
            args=args,
            required_env=raw.get("requiredEnvVars", raw.get("env_vars", [])),
            version=raw.get("version", ""),
            homepage=raw.get("homepage", raw.get("url", "")),
            downloads=int(raw.get("downloads", raw.get("downloadCount", 0))),
        )

    def _parse_clawhub_entry(self, raw: dict[str, Any]) -> MCPRegistryEntry:
        pkg = raw.get("package", raw.get("name", ""))
        name = _to_config_name(raw.get("slug", pkg))
        is_py = raw.get("language", "javascript").lower() == "python"
        pm, cmd = ("pip", "python") if is_py else ("npm", "npx")
        args = raw.get("args", [])
        if not args:
            args = ["-m", name] if is_py else ["-y", pkg]
        return MCPRegistryEntry(
            package_name=pkg,
            name=name,
            description=raw.get("description", ""),
            registry="clawhub",
            transport=MCPTransport.STDIO,
            package_manager=pm,
            command=cmd,
            args=args,
            required_env=raw.get("env_vars", []),
            version=raw.get("version", ""),
            homepage=raw.get("url", ""),
            downloads=int(raw.get("downloads", 0)),
        )

    def _generate_config(self, entry: MCPRegistryEntry) -> MCPServerConfig:
        """Generate MCPServerConfig from a registry entry."""
        env: dict[str, str] = {}
        for var in entry.required_env:
            env[var] = f"${{{var}}}"  # Placeholder: user fills in real value

        return MCPServerConfig(
            name=entry.name,
            transport=entry.transport,
            command=entry.command,
            args=entry.args,
            env=env,
            sandbox=True,
            approval_required=["*"],
        )

    async def _scan_package(self, entry: MCPRegistryEntry) -> Any:
        """Download and scan a package. Returns a ScanResult."""
        from pincer.skills.scanner import PackageScanner, ScanResult

        staging_dir = self._staging / entry.name
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            downloaded = await self._download_stdio_package(entry, staging_dir)
        except Exception as exc:
            logger.warning("Could not download '%s' for scanning: %s", entry.package_name, exc)
            # Return neutral score — can't scan without source
            return ScanResult(score=70, error=str(exc))

        scanner = PackageScanner()
        return scanner.scan_path(downloaded)

    async def _download_stdio_package(self, entry: MCPRegistryEntry, dest: Path) -> Path:
        """
        Download package source for scanning.

        For npm: use `npm pack` to get the tarball, extract to dest.
        For pip: use `pip download --no-deps` to get the wheel/sdist.

        Returns the directory containing the source files.
        """
        if entry.package_manager == "npm":
            return await self._npm_pack(entry.package_name, dest)
        return await self._pip_download(entry.package_name, dest)

    async def _npm_pack(self, package: str, dest: Path) -> Path:
        """Run `npm pack <package>` and extract the tarball."""
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm not found in PATH — cannot scan npm package")

        import asyncio
        proc = await asyncio.create_subprocess_exec(
            npm, "pack", package, "--pack-destination", str(dest),
            cwd=str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"npm pack failed for {package!r}")

        # Extract the tarball
        tarball = stdout.decode().strip().splitlines()[-1].strip()
        tarball_path = dest / tarball
        if tarball_path.exists():
            import tarfile
            with tarfile.open(tarball_path) as tf:
                tf.extractall(dest)  # noqa: S202

        # npm pack extracts to 'package/' subdirectory
        extracted = dest / "package"
        return extracted if extracted.exists() else dest

    async def _pip_download(self, package: str, dest: Path) -> Path:
        """Run `pip download --no-deps <package>` and extract."""
        pip = shutil.which("pip3") or shutil.which("pip")
        if not pip:
            raise RuntimeError("pip not found in PATH — cannot scan Python package")

        import asyncio
        proc = await asyncio.create_subprocess_exec(
            pip, "download", "--no-deps", "--dest", str(dest), package,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)

        # Find and extract the downloaded file
        for f in dest.iterdir():
            if f.suffix in (".whl", ".tar.gz", ".zip"):
                _extract_archive(f, dest)
                break

        return dest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_config_name(raw: str) -> str:
    """Convert a package name to a config-safe alphanumeric_underscore name."""
    import re
    # Strip scope (@org/name → name)
    name = raw.split("/")[-1] if "/" in raw else raw
    # Remove "server-" prefix (common in MCP packages)
    name = re.sub(r"^(?:mcp-)?server[-_]?", "", name)
    # Replace non-alphanumeric with underscore
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Trim leading/trailing underscores
    name = name.strip("_") or "mcp_server"
    return name


def _infer_entry(package_name: str) -> MCPRegistryEntry:
    """Best-effort entry for a package not found in any registry."""
    is_py = (
        not package_name.startswith("@")
        and "/" not in package_name
        and package_name.replace("-", "_").replace(".", "_").isidentifier()
    )
    pm, cmd = ("pip", "python") if is_py else ("npm", "npx")
    args = ["-m", package_name] if is_py else ["-y", package_name]
    return MCPRegistryEntry(
        package_name=package_name,
        name=_to_config_name(package_name),
        description="",
        registry="unknown",
        transport=MCPTransport.STDIO,
        package_manager=pm,
        command=cmd,
        args=args,
    )


def _extract_archive(path: Path, dest: Path) -> None:
    """Extract a wheel/tarball/zip to dest."""
    import tarfile
    import zipfile

    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            zf.extractall(dest)  # noqa: S202
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as tf:
            tf.extractall(dest)  # noqa: S202
