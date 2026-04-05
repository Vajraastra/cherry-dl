"""
Interfaz de línea de comandos para cherry-dl.

Comandos disponibles:
  download  — Descarga todos los archivos de un artista desde una URL
  organize  — Incorpora archivos externos al catálogo
  status    — Lista las colecciones indexadas
  relink    — Actualiza la ruta de carpeta de un artista movido
  config    — Ver/editar configuración de usuario
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="cherry-dl",
    help="Mass downloader modular con catálogo inteligente.",
    add_completion=False,
)
console = Console()

config_app = typer.Typer(help="Gestión de configuración.")
app.add_typer(config_app, name="config")


# ── Helpers async ──────────────────────────────────────────────────────────────

def run(coro):
    """Ejecuta una coroutine en el event loop."""
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════════════════════
# DOWNLOAD
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def download(
    url: str = typer.Argument(..., help="URL del artista (ej: https://kemono.cr/patreon/user/123)"),
    workers: Optional[int] = typer.Option(None, "--workers", "-w", help="Descargas paralelas (default: config)"),
    prescan: Optional[str] = typer.Option(None, "--prescan", "-p",
        help="Carpeta con archivos existentes a indexar antes de descargar"),
):
    """Descarga todos los archivos de un artista."""
    run(_download(url, workers, prescan))


async def _download(url: str, workers: int | None, prescan: str | None = None) -> None:
    from .config import load_config, ensure_dirs, INDEX_DB
    from .engine import DownloadEngine, make_progress
    from .templates._registry import get_template
    from .catalog import init_catalog, hash_exists, add_file, next_counter
    from .gui.bridge import build_filename
    from .index import init_index, get_or_create_site, get_or_create_artist

    config = load_config()
    ensure_dirs(config)

    # Detectar template
    engine_workers = workers or config.workers
    async with DownloadEngine(config, workers=engine_workers) as engine:
        template = get_template(url, engine)
        if template is None:
            console.print(f"[red]✗ No hay template para esta URL:[/red] {url}")
            console.print(f"  Templates disponibles: {_list_templates()}")
            raise typer.Exit(1)

        console.print(f"[bold green]cherry-dl[/bold green] — Template: [cyan]{template.name}[/cyan]")
        console.print(f"  URL: {url}")

        # Info del artista
        console.print("  Obteniendo información del artista...")
        try:
            artist = await template.get_artist_info(url)
        except Exception as e:
            console.print(f"[red]✗ Error al obtener info del artista:[/red] {e}")
            raise typer.Exit(1)

        console.print(f"  Artista: [bold]{artist.name}[/bold] ({artist.service})")

        # Directorio destino
        artist_dir = config.download_path / _safe_dirname(artist.name)
        artist_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"  Destino: [dim]{artist_dir}[/dim]")

        # Inicializar catálogo e índice
        await init_catalog(artist_dir)
        await init_index(INDEX_DB)
        site_id = await get_or_create_site(INDEX_DB, artist.site)
        await get_or_create_artist(
            db_path=INDEX_DB,
            site_id=site_id,
            artist_id=artist.artist_id,
            name=artist.name,
            folder_path=artist_dir,
        )

        # Pre-scan de carpeta existente (opcional)
        if prescan:
            from pathlib import Path as _Path
            from .organizer import organize
            prescan_path = _Path(prescan)
            if not prescan_path.is_dir():
                console.print(f"[red]✗ La ruta de pre-scan no existe:[/red] {prescan}")
                raise typer.Exit(1)

            console.print(f"\n  [cyan]Pre-scan:[/cyan] {prescan_path}")
            console.print("  Escaneando y renombrando archivos existentes…")

            def _prescan_cb(processed: int, total: int, filename: str) -> None:
                console.print(f"    [{processed}/{total}] {filename[:60]}", highlight=False)

            scan_result, _ = await organize(
                source_dir=prescan_path,
                artist_name=artist.name,
                artist_id=artist.artist_id,
                site=artist.site,
                dest_root=config.download_path,
                progress_cb=_prescan_cb,
            )
            console.print(f"  Pre-scan listo: {scan_result.summary()}\n")

        # Iterar y descargar
        downloaded = skipped = errors = 0

        with make_progress() as progress:
            overall = progress.add_task(
                f"[bold]{artist.name}[/bold]", total=None
            )

            async for file_info in template.iter_files(artist):
                # Obtener contador y construir nombre final antes de descargar
                counter = await next_counter(artist_dir)
                final_name = build_filename(artist.name, counter, file_info.filename)

                result = await engine.download(
                    url=file_info.url,
                    dest_dir=artist_dir,
                    filename=final_name,
                    progress=progress,
                )

                if not result.ok:
                    errors += 1
                    console.print(f"[red]  ✗ {file_info.filename}:[/red] {result.error}")
                    continue

                # Cuando ok=True el engine garantiza file_hash no-None
                assert result.file_hash is not None

                # Verificar hash contra catálogo
                if await hash_exists(artist_dir, result.file_hash):
                    # Duplicado — eliminar el archivo recién descargado
                    if result.dest and result.dest.exists():
                        result.dest.unlink()
                    skipped += 1
                else:
                    # Nuevo — registrar en catálogo
                    await add_file(
                        artist_dir=artist_dir,
                        file_hash=result.file_hash,
                        filename=final_name,
                        url_source=file_info.url,
                        file_size=result.file_size,
                        counter=counter,
                    )
                    downloaded += 1

                progress.advance(overall)

    # Resumen final
    console.print()
    console.print(f"[bold green]✓ Completado[/bold green] — {artist.name}")
    console.print(f"  Descargados: [green]{downloaded}[/green]")
    console.print(f"  Duplicados ignorados: [yellow]{skipped}[/yellow]")
    if errors:
        console.print(f"  Errores: [red]{errors}[/red]")


# ════════════════════════════════════════════════════════════════════════════════
# ORGANIZE
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def organize(
    source: str = typer.Argument(..., help="Ruta de carpeta con archivos a incorporar"),
    site: str = typer.Option(..., "--site", "-s", help="Nombre del sitio (ej: kemono)"),
    artist: str = typer.Option(..., "--artist", "-a", help="ID o nombre del artista"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Nombre legible del artista (opcional)"),
):
    """Incorpora archivos externos al catálogo de cherry-dl."""
    run(_organize(source, site, artist, name or artist))


async def _organize(source: str, site: str, artist_id: str, artist_name: str) -> None:
    from .config import load_config, ensure_dirs
    from .organizer import organize as do_organize

    source_path = Path(source)
    if not source_path.is_dir():
        console.print(f"[red]✗ La ruta no existe o no es un directorio:[/red] {source}")
        raise typer.Exit(1)

    config = load_config()
    ensure_dirs(config)

    console.print(f"[bold green]cherry-dl organize[/bold green]")
    console.print(f"  Fuente: [dim]{source_path}[/dim]")
    console.print(f"  Sitio:  {site} | Artista: {artist_name}")

    result, _ = await do_organize(
        source_dir=source_path,
        site=site,
        artist_id=artist_id,
        artist_name=artist_name,
        dest_root=config.download_path,
    )

    console.print()
    console.print(f"[bold green]✓ Organización completa[/bold green]")
    console.print(f"  {result.summary()}")
    if result.errors:
        console.print(f"[red]  Errores encontrados:[/red]")
        for err in result.errors[:10]:
            console.print(f"    - {err}")


# ════════════════════════════════════════════════════════════════════════════════
# STATUS
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def status():
    """Lista todas las colecciones indexadas."""
    run(_status())


async def _status() -> None:
    from .config import load_config, INDEX_DB
    from .index import init_index, list_all
    from .catalog import get_stats

    config = load_config()
    await init_index(INDEX_DB)

    artists = await list_all(INDEX_DB)

    if not artists:
        console.print("[yellow]No hay colecciones indexadas.[/yellow]")
        console.print(f"  Usa [bold]cherry-dl download <url>[/bold] para iniciar.")
        return

    table = Table(title="Colecciones cherry-dl", show_lines=True)
    table.add_column("Sitio", style="cyan", no_wrap=True)
    table.add_column("Artista", style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Archivos", justify="right")
    table.add_column("Tamaño", justify="right")
    table.add_column("Ruta", style="dim", overflow="fold")

    total_files = 0
    total_size = 0

    for a in artists:
        folder = Path(a["folder_path"])
        stats = await get_stats(folder) if folder.exists() else {"total": 0, "total_size": 0}

        total_files += stats["total"]
        total_size += stats["total_size"]

        table.add_row(
            a["site"],
            a["name"],
            a["artist_id"],
            str(stats["total"]),
            _fmt_size(stats["total_size"]),
            str(folder),
        )

    console.print(table)
    console.print(
        f"\n  Total: [bold]{len(artists)}[/bold] artistas | "
        f"[bold]{total_files:,}[/bold] archivos | "
        f"[bold]{_fmt_size(total_size)}[/bold]"
    )
    console.print(f"  Directorio base: [dim]{config.download_dir}[/dim]")


# ════════════════════════════════════════════════════════════════════════════════
# RELINK
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def relink(
    artist_id: str = typer.Argument(..., help="ID del artista en el sitio"),
    site: str = typer.Option(..., "--site", "-s", help="Nombre del sitio"),
    new_path: str = typer.Option(..., "--path", "-p", help="Nueva ruta de la carpeta del artista"),
):
    """Actualiza la ruta de carpeta de un artista movido."""
    run(_relink(artist_id, site, new_path))


async def _relink(artist_id: str, site: str, new_path: str) -> None:
    from .config import INDEX_DB
    from .index import init_index, relink_artist

    path = Path(new_path)
    if not path.is_dir():
        console.print(f"[red]✗ La ruta no existe:[/red] {new_path}")
        raise typer.Exit(1)

    await init_index(INDEX_DB)
    ok = await relink_artist(INDEX_DB, artist_id, site, path)

    if ok:
        console.print(f"[green]✓ Ruta actualizada[/green] para {site}/{artist_id}")
        console.print(f"  Nueva ruta: [dim]{path}[/dim]")
    else:
        console.print(f"[red]✗ Artista no encontrado:[/red] {site}/{artist_id}")
        console.print("  Usa [bold]cherry-dl status[/bold] para ver artistas indexados.")
        raise typer.Exit(1)


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════

@config_app.command("show")
def config_show():
    """Muestra la configuración actual."""
    from .config import load_config, CONFIG_FILE

    config = load_config()
    table = Table(title="Configuración cherry-dl", show_header=False)
    table.add_column("Clave", style="cyan")
    table.add_column("Valor")

    table.add_row("download_dir", str(config.download_dir))
    table.add_row("workers", str(config.workers))
    table.add_row("timeout", f"{config.timeout}s")
    table.add_row("delay_min", f"{config.network.delay_min}s")
    table.add_row("delay_max", f"{config.network.delay_max}s")
    table.add_row("retries_api", str(config.network.retries_api))
    table.add_row("retries_file", str(config.network.retries_file))

    console.print(table)
    console.print(f"\n  Archivo: [dim]{CONFIG_FILE}[/dim]")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Clave a modificar (download_dir, workers, timeout)"),
    value: str = typer.Argument(..., help="Nuevo valor"),
):
    """Modifica una clave de configuración."""
    from .config import set_config_value

    try:
        config = set_config_value(key, value)
        console.print(f"[green]✓[/green] {key} = {value}")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1)


# ════════════════════════════════════════════════════════════════════════════════
# MIGRATE-STRUCTURE
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def migrate_structure(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Muestra el plan sin ejecutar nada"
    ),
):
    """Migra carpetas al esquema artist-first: {download_dir}/{artista}/."""
    run(_migrate_structure(dry_run))


async def _migrate_structure(dry_run: bool) -> None:
    import shutil
    import aiosqlite
    from .config import load_config, ensure_dirs, INDEX_DB
    from .index import init_index, list_profiles

    config = load_config()
    ensure_dirs(config)
    await init_index(INDEX_DB)

    profiles = await list_profiles(INDEX_DB)
    if not profiles:
        console.print("[yellow]No hay perfiles en el índice.[/yellow]")
        return

    # Calcular plan: solo perfiles cuya ruta actual ≠ ruta nueva
    plan = []
    for p in profiles:
        old_path = Path(p["folder_path"])
        new_path = config.download_path / _safe_dirname(p["display_name"])
        if old_path == new_path:
            continue
        plan.append({
            "id": p["id"], "name": p["display_name"],
            "old": old_path, "new": new_path,
        })

    if not plan:
        console.print(
            "[green]✓ Todos los perfiles ya usan la estructura nueva.[/]"
        )
        return

    # Mostrar plan en tabla
    table = Table(title="Plan de migración", show_lines=True)
    table.add_column("Perfil", style="bold")
    table.add_column("Ruta actual", style="dim", overflow="fold")
    table.add_column("Ruta nueva", style="cyan", overflow="fold")
    table.add_column("Disco")
    for item in plan:
        disk_status = (
            "[green]existe[/]"
            if item["old"].exists()
            else "[yellow]no en disco[/]"
        )
        table.add_row(
            item["name"], str(item["old"]), str(item["new"]), disk_status
        )
    console.print(table)

    if dry_run:
        console.print(
            "\n[yellow]Modo --dry-run: no se realizó ningún cambio.[/]"
        )
        return

    console.print(
        f"\n[bold yellow]⚠ Se moverán {len(plan)} carpetas en disco "
        f"y se actualizará el índice.[/bold yellow]"
    )
    if not typer.confirm("¿Continuar?", default=False):
        console.print("Cancelado.")
        return

    # Ejecutar migración
    migrated = errors = only_db = 0
    async with aiosqlite.connect(INDEX_DB) as db:
        for item in plan:
            old_path: Path = item["old"]
            new_path: Path = item["new"]
            try:
                if old_path.exists():
                    if new_path.exists():
                        console.print(
                            f"[red]  ✗ Conflicto:[/] {new_path} ya existe"
                            f" — saltando [bold]{item['name']}[/]"
                        )
                        errors += 1
                        continue
                    shutil.move(str(old_path), str(new_path))
                    console.print(f"  [green]↦[/green] {item['name']}")
                else:
                    console.print(
                        f"  [yellow]⚠ Solo DB[/] {item['name']}"
                        " (carpeta no existe en disco)"
                    )
                    only_db += 1

                # Actualizar profiles
                await db.execute(
                    "UPDATE profiles SET folder_path = ? WHERE id = ?",
                    (str(new_path), item["id"]),
                )
                # Actualizar artists (todos los que apuntan a la carpeta vieja)
                await db.execute(
                    "UPDATE artists SET folder_path = ? WHERE folder_path = ?",
                    (str(new_path), str(old_path)),
                )
                migrated += 1
            except Exception as e:
                console.print(f"[red]  ✗ Error en {item['name']}:[/red] {e}")
                errors += 1

        await db.commit()

    console.print()
    console.print("[bold green]✓ Migración completa[/]")
    console.print(f"  Migrados: [green]{migrated}[/]")
    if only_db:
        console.print(f"  Solo índice (sin archivos): [yellow]{only_db}[/]")
    if errors:
        console.print(f"  Errores: [red]{errors}[/red]")


# ════════════════════════════════════════════════════════════════════════════════
# MIGRATE-PENDING
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def migrate_pending(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Muestra qué catálogos se migrarían sin tocarlos"
    ),
):
    """
    Inicializa la tabla pending_queue en todos los catálogos existentes.

    Necesario una sola vez al actualizar cherry-dl a la versión con mapa
    persistente de descargas. Es seguro correrlo múltiples veces.
    """
    run(_migrate_pending(dry_run))


async def _migrate_pending(dry_run: bool) -> None:
    import aiosqlite
    from .config import load_config, ensure_dirs, INDEX_DB
    from .index import init_index, list_profiles
    from .catalog import init_catalog, CATALOG_NAME

    config = load_config()
    ensure_dirs(config)
    await init_index(INDEX_DB)

    profiles = await list_profiles(INDEX_DB)
    if not profiles:
        console.print("[yellow]No hay perfiles en el índice.[/]")
        return

    table = Table(
        title="Migración de catálogos — pending_queue",
        show_lines=False,
    )
    table.add_column("Perfil", style="bold")
    table.add_column("Catálogo", style="dim", overflow="fold")
    table.add_column("Estado", justify="center")

    ok = skipped = already = errors = 0

    for p in profiles:
        folder  = Path(p["folder_path"])
        db_path = folder / CATALOG_NAME
        name    = p["display_name"]

        if not db_path.exists():
            table.add_row(name, str(db_path), "[yellow]sin catálogo[/]")
            skipped += 1
            continue

        # Verificar si pending_queue ya existe
        try:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='pending_queue'"
                ) as cur:
                    exists = await cur.fetchone() is not None
        except Exception as exc:
            table.add_row(name, str(db_path), f"[red]error al leer: {exc}[/]")
            errors += 1
            continue

        if exists:
            table.add_row(name, str(db_path), "[dim]ya migrado[/]")
            already += 1
            continue

        if dry_run:
            table.add_row(name, str(db_path), "[cyan]pendiente[/]")
            ok += 1
            continue

        # Aplicar migración
        try:
            await init_catalog(folder)
            table.add_row(name, str(db_path), "[green]✓ migrado[/]")
            ok += 1
        except Exception as exc:
            table.add_row(name, str(db_path), f"[red]✗ {exc}[/]")
            errors += 1

    console.print(table)
    console.print()

    if dry_run:
        console.print(
            f"[yellow]--dry-run:[/] {ok} catálogo(s) por migrar, "
            f"{already} ya migrados, {skipped} sin catálogo."
        )
        console.print(
            "  Corre sin --dry-run para aplicar."
        )
        return

    console.print("[bold green]✓ Migración completa[/]")
    console.print(f"  Migrados:    [green]{ok}[/]")
    console.print(f"  Ya tenían:   [dim]{already}[/]")
    if skipped:
        console.print(f"  Sin catálogo: [yellow]{skipped}[/]")
    if errors:
        console.print(f"  Errores:     [red]{errors}[/red]")


# ════════════════════════════════════════════════════════════════════════════════
# COMPACT
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def compact(
    profile: str = typer.Argument(
        ..., help="Nombre o ID del perfil a compactar"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Muestra el plan sin ejecutar nada"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Omite la confirmación interactiva"
    ),
):
    """Compacta la numeración de archivos, eliminando huecos."""
    run(_compact(profile, dry_run, yes))


async def _compact(
    profile_id_or_name: str, dry_run: bool, yes: bool
) -> None:
    from .config import load_config, ensure_dirs, INDEX_DB
    from .index import init_index, list_profiles
    from .catalog import (
        get_numbered_files, plan_compaction, apply_compaction,
    )

    config = load_config()
    ensure_dirs(config)
    await init_index(INDEX_DB)

    # Buscar perfil por ID o nombre
    profiles = await list_profiles(INDEX_DB)
    matched = next(
        (
            p for p in profiles
            if str(p["id"]) == profile_id_or_name
            or p["display_name"] == profile_id_or_name
        ),
        None,
    )
    if matched is None:
        console.print(f"[red]✗ Perfil no encontrado:[/] {profile_id_or_name}")
        console.print(
            "  Usa [bold]cherry-dl status[/bold] para ver perfiles."
        )
        raise typer.Exit(1)

    folder = Path(matched["folder_path"])
    if not folder.exists():
        console.print(f"[red]✗ Carpeta no encontrada:[/] {folder}")
        raise typer.Exit(1)

    files = await get_numbered_files(folder)
    if not files:
        console.print("[yellow]No hay archivos numerados en el catálogo.[/]")
        return

    plan = plan_compaction(files)
    if not plan:
        console.print(
            f"[green]✓ Numeración ya es continua "
            f"({len(files)} archivos).[/]"
        )
        return

    console.print(
        f"Perfil: [bold]{matched['display_name']}[/bold]"
        f"  |  Carpeta: [dim]{folder}[/dim]"
    )
    console.print(
        f"Total archivos: [cyan]{len(files)}[/]"
        f"  |  A renombrar: [yellow]{len(plan)}[/]"
    )

    if dry_run:
        table = Table(title="Plan de compactación", show_lines=False)
        table.add_column("Antes", style="dim")
        table.add_column("Después", style="cyan")
        limit = 20
        for old_name, new_name, *_ in plan[:limit]:
            table.add_row(old_name, new_name)
        if len(plan) > limit:
            table.add_row("...", f"(+{len(plan) - limit} más)")
        console.print(table)
        console.print(
            "\n[yellow]Modo --dry-run: no se realizó ningún cambio.[/]"
        )
        return

    if not yes:
        console.print(
            f"\n[bold yellow]⚠ Se renombrarán {len(plan)} archivos en disco.[/]"
        )
        console.print("  Los archivos en el catálogo se actualizarán.")
        if not typer.confirm("¿Continuar?", default=False):
            console.print("Cancelado.")
            return

    await apply_compaction(folder, plan, len(files))
    console.print(
        f"[bold green]✓ Compactación completa[/] — "
        f"{len(plan)} archivos renombrados"
    )


# ════════════════════════════════════════════════════════════════════════════════
# Helpers internos
# ════════════════════════════════════════════════════════════════════════════════

def _safe_dirname(name: str) -> str:
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip("._") or "unknown"


def _fmt_size(n: int) -> str:
    """Formatea bytes a unidad legible."""
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _list_templates() -> str:
    from .templates._registry import list_templates
    return ", ".join(list_templates())


# ════════════════════════════════════════════════════════════════════════════════
# GUI
# ════════════════════════════════════════════════════════════════════════════════

@app.command()
def gui():
    """Lanza la interfaz gráfica (PySide6)."""
    try:
        from .gui.app import run_app
    except ImportError:
        console.print("[red]✗ GUI no disponible.[/red]")
        console.print("  Ejecuta: [bold]./run.sh[/bold] para instalar dependencias (PySide6).")
        raise typer.Exit(1)
    run_app()


@app.command()
def tui():
    """Lanza la interfaz de texto (Textual TUI)."""
    try:
        from .tui.app import run
    except ImportError:
        console.print("[red]✗ TUI no disponible.[/red]")
        console.print("  Ejecuta: [bold]./run.sh[/bold] para instalar dependencias (textual).")
        raise typer.Exit(1)
    run()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
