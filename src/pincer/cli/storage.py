"""`pincer storage` — object storage maintenance: encrypting existing data."""

from __future__ import annotations

from typing import Any

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

storage_app = AsyncTyper(name="storage", help="Manage object storage (encryption with a customer key)")


def _locations() -> list[str]:
    from pincer.config import get_settings_relaxed
    from pincer.storage.encryption_state import locations

    return locations(get_settings_relaxed())


@storage_app.command(name="encrypt")
def storage_encrypt(
    dry_run: bool = typer.Option(False, "--dry-run", help="Only count what is not encrypted yet; change nothing"),
) -> None:
    """Encrypt every object stored before PINCER_STORAGE_ENCRYPTION_KEY was set.

    Covers the storage root and a cloud skills dir outside it. Each plain object
    is rewritten in place with the key, unless it changed meanwhile. Safe to run
    again at any time; a running `pincer run` does the same in the background.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from pincer.config import get_settings_relaxed
    from pincer.storage.encrypt_job import Lease, encrypt_location
    from pincer.storage.encryption import load_key
    from pincer.storage.errors import StorageError

    settings = get_settings_relaxed()
    if load_key(settings) is None:
        console.print("[red]PINCER_STORAGE_ENCRYPTION_KEY is not set: there is nothing to encrypt with.[/red]")
        raise typer.Exit(code=1)
    locations = _locations()
    if not locations:
        console.print("[yellow]No cloud storage is configured: encryption applies to s3://, az:// and gs:// only.[/]")
        raise typer.Exit(code=1)

    from pincer.storage import clients
    from pincer.storage.encryption_state import check_location

    failed = False
    for location in locations:
        try:
            # A dry run verifies the lock but never writes it: trying a key out
            # must not make it permanent.
            root = clients.open_cloud_uri(
                location, settings, None, error=StorageError, what="storage location", check=False
            )
            check_location(root, settings, thorough=True, create=not dry_run)
        except StorageError as e:
            console.print(f"[red]{location}: {e}[/red]")
            failed = True
            continue
        except Exception as e:
            console.print(f"[red]{location}: could not check ({type(e).__name__}: {e})[/red]")
            failed = True
            continue
        lease = Lease(root)
        if not dry_run and not lease.acquire():
            console.print(f"[yellow]{location}: another process is encrypting it; try again later.[/yellow]")
            failed = True
            continue
        try:
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as bar:
                task = bar.add_task(f"{location}: scanning", total=None)
                counts: dict[str, int] = {}

                def seen(
                    key: str,
                    outcome: str,
                    task: Any = task,
                    bar: Progress = bar,
                    counts: dict[str, int] = counts,
                    location: str = location,
                ) -> None:
                    counts[outcome] = counts.get(outcome, 0) + 1
                    summary = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
                    bar.update(task, description=f"{location}: {summary}")

                progress = encrypt_location(
                    root, dry_run=dry_run, on_object=seen, heartbeat=None if dry_run else lease.refresh
                )
        except StorageError as e:
            console.print(f"[red]{location}: {e}[/red]")
            failed = True
            continue
        except Exception as e:
            console.print(f"[red]{location}: stopped ({type(e).__name__}: {e})[/red]")
            failed = True
            continue
        finally:
            if not dry_run:
                lease.release()
        result = progress.as_dict()
        if dry_run:
            colour = "yellow" if progress.failed else "default"
            console.print(
                f"[{colour}]{location}: {result['pending']} object(s) to encrypt, {result['already_encrypted']} "
                f"already encrypted, {result['failed']} could not be checked[/{colour}]"
            )
            failed = failed or bool(progress.failed)
        else:
            colour = "green" if progress.state == "complete" else "yellow"
            console.print(
                f"[{colour}]{location}: {result['encrypted']} encrypted, {result['already_encrypted']} already "
                f"encrypted, {result['changed_meanwhile']} changed meanwhile, {result['failed']} failed "
                f"— {progress.state}[/{colour}]"
            )
            # Stopped early (lost lease) or failed objects: not done, whatever the counts say.
            failed = failed or progress.state != "complete"
        if progress.old_versions:
            console.print(
                f"[yellow]{location}: {progress.old_versions}. Delete the old versions to remove the plain "
                "copies (see docs/guides/storage-encryption.md#versioned-buckets).[/yellow]"
            )
    if failed:
        raise typer.Exit(code=1)


@storage_app.command(name="encryption-status")
def storage_encryption_status() -> None:
    """Show, for each cloud storage location, whether it is encrypted and how far encrypting existing data got."""
    from rich.table import Table

    from pincer.config import get_settings_relaxed
    from pincer.storage import clients, encrypt_job
    from pincer.storage.encryption import load_key
    from pincer.storage.encryption_state import check_location, status
    from pincer.storage.errors import StorageEncryptionError, StorageError

    settings = get_settings_relaxed()
    key = load_key(settings)
    table = Table(show_header=True)
    table.add_column("Location", style="bold")
    table.add_column("Encryption")
    table.add_column("Existing data")
    table.add_column("Updated", style="dim")
    for location in _locations():
        try:
            root = clients.open_cloud_uri(
                location, settings, None, error=StorageError, what="storage location", check=False
            )
            # Refuses a turned-off or changed key; writes nothing.
            check_location(root, settings, thorough=True, create=False)
            marker = status(root)
        except StorageEncryptionError as e:
            table.add_row(location, f"[red]refused: {e}[/red]", "", "")
            continue
        except Exception as e:
            table.add_row(location, f"[yellow]could not check: {type(e).__name__}: {e}[/yellow]", "", "")
            continue
        if marker is None:
            table.add_row(location, "off" if key is None else "[yellow]enabled on the next start[/yellow]", "", "")
            continue
        migration = marker.get("migration", {})
        state = migration.get("state", "pending")
        colour = "green" if state == "complete" else "yellow"
        # Checked again now (read-only); see `old_versions_now` for when the warning ends.
        old_versions = encrypt_job.old_versions_now(root, migration)
        table.add_row(
            location,
            f"[green]on since {marker.get('enabled_at', '?')}[/green]",
            f"[{colour}]{state}[/{colour}] ({migration.get('done', 0)} object(s))"
            + (f"\n[yellow]old plain versions may remain: {old_versions}[/yellow]" if old_versions else ""),
            str(migration.get("updated_at", "")),
        )
    console.print(table)


@storage_app.command(name="old-versions-deleted")
def storage_old_versions_deleted() -> None:
    """Record that the plain versions left by encrypting in place were deleted.

    On a bucket with versioning (or soft delete), encrypting rewrites each
    object and keeps the plain one as an old version, so `pincer doctor` and
    `encryption-status` warn about it while versioning stays on. Once you have
    deleted those versions (docs/guides/storage-encryption.md#versioned-buckets),
    run this to end the warning; it comes back when the job rewrites another
    object. Pincer cannot list which old versions predate encryption, so it
    takes your word for it.
    """
    from pincer.config import get_settings_relaxed
    from pincer.storage import clients, encrypt_job
    from pincer.storage.encryption import load_key
    from pincer.storage.encryption_state import check_location
    from pincer.storage.errors import StorageError

    settings = get_settings_relaxed()
    if load_key(settings) is None:
        console.print("[red]PINCER_STORAGE_ENCRYPTION_KEY is not set: storage encryption is off.[/red]")
        raise typer.Exit(code=1)
    failed = False
    for location in _locations():
        try:
            root = clients.open_cloud_uri(
                location, settings, None, error=StorageError, what="storage location", check=False
            )
            check_location(root, settings, thorough=True, create=False)
            encrypt_job.record_old_versions_deleted(root)
        except Exception as e:
            console.print(f"[red]{location}: {e}[/red]")
            failed = True
            continue
        console.print(f"[green]{location}: recorded; the old-versions warning ends until the next rewrite.[/green]")
    if failed:
        raise typer.Exit(code=1)
