# TASKS — cherry-dl

## Estado actual (2026-03-24)

### Fase 1 — COMPLETA ✓
- [x] Scaffolding del proyecto
- [x] Módulos base (config, hasher)
- [x] Base de datos (catalog, index)
- [x] Engine de descargas async
- [x] Sistema de templates (base, registry, kemono)
- [x] Organizador de archivos externos
- [x] CLI (download, organize, status, relink, config)
- [x] GUI Dear PyGui — DESCARTADA (reemplazada con PySide6)

### Fase 2 — GUI PySide6: COMPLETA ✓ (mantenida como legado)
- [x] Dependencias: PySide6, qasync (dearpygui removido)
- [x] index.py: tablas `profiles` + `profile_urls` + CRUD completo
- [x] index.py: migración automática de artists → perfiles implícitos
- [x] gui/theme.py: QSS oscuro cherry-dl
- [x] gui/app.py: QMainWindow + QStackedWidget (router de 4 vistas)
- [x] gui/views/profiles_view.py: lista de perfiles con tabla
- [x] gui/views/new_profile_wizard.py: wizard creación de perfil
- [x] gui/views/artist_detail_view.py: detalle/descarga por artista
- [x] gui/views/settings_view.py: configuración global
- [x] stall_timeout persistido correctamente en config.toml
- [x] file_count y last_synced actualizan la UI inmediatamente sin reiniciar
- [x] Entradas "migrado" duplicadas en fuentes corregidas
- [x] Semáforo de estado (idle/running/cancelled/error/done)
- [x] Panel de workers en QScrollArea (max 5 visible, scrollable)
- [x] Log expandido con mensajes detallados (razón de skip/rename/error)
- [x] Botón deduplicador ("⊘ Deduplicar")
- [x] Contadores en barra inferior (descargados, saltados, errores, diferidos)

### Fase 3 — TUI Textual: COMPLETA ✓
- [x] `textual>=0.70.0` agregado a dependencias (instalado: 8.1.1)
- [x] `tui/app.py`: app completa con Textual (ProfilesScreen + ArtistScreen + SettingsScreen + modales)
- [x] `tui/theme.tcss`: tema cherry oscuro (misma paleta que GUI PySide6)
- [x] `cherry-dl tui` comando en CLI
- [x] `run.sh` actualizado para lanzar TUI por defecto
- [x] `CSS_PATH` con `Path(__file__).parent` — resuelve desde cualquier directorio
- [x] `border-radius` eliminado del TCSS (no soportado en Textual)
- [x] Barra de estado (`status-bar`) con `dock: bottom` — siempre visible
- [x] Workers input: `width: 6` → `width: 10` — campo visible
- [x] Rules redundantes eliminadas — más espacio útil en pantalla
- [x] `ProfilesScreen`: toolbar con botones + Nuevo / ⟳ Refresh / ⌫ Eliminar / ⚙ Config
- [x] `NewProfileModal` ampliado: resolver URL via API, nombre auto, carpeta auto, workers, filtro ext, botón "Crear y descargar"
- [x] Bug `_create_profile`: `create_profile()` no acepta `url=` — separado en `create_profile()` + `add_profile_url()`
- [x] Portapapeles: `_read_clipboard()` (wl-paste / xclip / xsel), `ClipInput` con `action_paste` + `on_paste`
- [x] Menú contextual `InputContextMenu`: clic derecho en cualquier Input → Pegar / Seleccionar todo / Limpiar
- [x] Prueba de arranque confirmada — TUI funcional

### Auditoría y correcciones de bugs — COMPLETA ✓
- [x] I/O bloqueante en engine → `asyncio.to_thread` + `_finalize_download()`
- [x] Race condition en `next_counter` → `UPDATE … RETURNING value`
- [x] `row[0]` sin validar en `next_counter` → RuntimeError descriptivo
- [x] Unlink de archivo válido tras rename fallido → lógica invertida
- [x] `assert` reemplazado por validación explícita con log
- [x] Fire-and-forget sin catch en profiles_view → `add_done_callback`
- [x] Índice faltante en `url_source` → `idx_url_source` en catalog.py
- [x] Doble-clic en botones de URL → deshabilitado mientras tarea corre
- [x] Race condition descargas duplicadas → patrón repartidor/workers + `in_progress_hashes`
- [x] Escritura atómica de archivos → `.tmp` → rename
- [x] Limpieza de `.tmp` huérfanos en `_build_local_hash_map`
- [x] Test de integración end-to-end con URL real (10/10 ✓)

---

### Fase 4 — Template Patreon: COMPLETA ✓
- [x] `FileInfo.url_source` + `FileInfo.dedup_key` en `base.py`
- [x] `cherry_dl/auth/__init__.py` + `auth/patreon.py` — browser-cookie3 + NeedsManualAuth
- [x] `cherry_dl/templates/patreon.py` — PatreonTemplate completo
- [x] `_registry.py` — PatreonTemplate registrado
- [x] TUI: `fi.dedup_key` en 6 puntos (url_exists + add_file)
- [x] TUI: cap de workers por `template.max_workers` (Patreon = 2)
- [x] TUI: `PatreonAuthModal` — abre browser del sistema + detecta cookies post-login
- [x] TUI: `_resolve_url` y `_do_download` manejan `NeedsManualAuth`
- [x] `browser-cookie3>=0.19.0` en pyproject.toml (instalado: 0.20.1)
- [x] Playwright removido — sin deps de browser binaries

---

### Fase 5 — Sistema multi-URL + fixes (2026-03-21): COMPLETA ✓
- [x] Bug: `_del_url_async` usaba `tbl.get_row_index()` inexistente → `AttributeError` silencioso — corregido
- [x] Bug: regex Patreon rechazaba `/cw/` URLs (`patreon.com/cw/username`) — `(?:c/)?` → `(?:[a-z]{1,4}/)?`
- [x] `SiteTemplate.provides_file_hashes: bool = False` — atributo base para templates
- [x] `KemonoTemplate.provides_file_hashes = True` — único template con hash pre-descarga
- [x] Dedup post-descarga: `hash_exists()` tras descarga exitosa → descarta duplicados cross-site
- [x] `AddUrlModal`: validación en tiempo real con `on_input_changed` — detecta template o muestra error
- [x] `AddUrlModal`: bloquea guardado si no hay template — no guarda URLs `"unknown"`
- [x] `AddUrlModal`: advertencia visible cuando template no provee hashes (primer scan descarga todo)
- [x] Test manual completo — scrapping Kemono + Patreon confirmado funcional end-to-end

---

## Backlog post-Fase 2

### CLI — pendiente
- [ ] `cherry-dl profile list`
- [ ] `cherry-dl profile create <url> [--name NAME]`
- [ ] `cherry-dl profile add-url <profile_id> <url>`
- [ ] `cherry-dl profile update <profile_id>`
- [ ] `cherry-dl profile check <profile_id>`
- [ ] `cherry-dl profile update-all`

### Fase 6 — Template Pixiv: COMPLETA ✓
- [x] `cherry_dl/auth/pixiv.py` — cookies browser-cookie3 (idéntico a Patreon), `NeedsPixivAuth`
- [x] `cherry_dl/templates/pixiv.py` — web AJAX API: profile/all → lotes → pages/ugoira_meta
- [x] `cherry_dl/templates/base.py` — `FileInfo.extra_headers` para headers por-archivo
- [x] `cherry_dl/engine.py` — `download(extra_headers=)` fusiona headers al stream
- [x] `cherry_dl/templates/_registry.py` — PixivTemplate registrado
- [x] TUI: `PixivAuthModal` — estilo PatreonAuthModal (abrir browser → confirmar)
- [x] TUI: `_resolve_url` y `_do_download` manejan `NeedsPixivAuth`
- [x] TUI: `worker_task` y cola diferida pasan `fi.extra_headers` al engine
- [x] Bug: `body.get("works", {})` → endpoint devuelve IDs directamente en `body` (no en `body.works`)
- [x] Bug: `urls.original` vacío en batch → siempre llamar `/pages` para todas las obras no-ugoira
- [x] Bug: `engine._session_cookies` incluía dicts anidados de Pixiv/Patreon → filtrar `isinstance(v, str)`
- [x] Test de integración end-to-end: 5 descargas reales ✓ (JPGs originales + multi-página)

### Fase 7 — Reestructuración de carpetas (artist-first): COMPLETA ✓

**Objetivo:** cambiar la estructura de `{download_dir}/{site}/{artista}/` a `{download_dir}/{artista}/`.
Un artista con Kemono + Patreon + Pixiv tendrá una sola carpeta unificada con un solo `catalog.db`.

#### Fase 7a — Cambio de estructura base ✓
- [x] `tui/app.py:_auto_folder()` — `cfg.download_path / self._site / name` → `cfg.download_path / name`
- [x] `cli.py:_download()` — `config.download_path / artist.site / ...` → `config.download_path / ...`

#### Fase 7b — Comando de migración ✓
- [x] `cli.py`: comando `cherry-dl migrate-structure`
  - Lista todos los perfiles en `index.db`
  - Por cada perfil: calcula ruta destino `{download_dir}/{display_name}/`
  - Si ya está en estructura nueva → skip
  - Muestra tabla de plan antes de ejecutar
  - Si carpeta existe → `shutil.move()` + UPDATE `profiles` y `artists` en `index.db`
  - Si carpeta no existe → solo actualiza DB
  - Resumen: migrados / solo DB / errores
- [x] Flag `--dry-run`: muestra el plan sin ejecutar
- [x] Confirmación interactiva antes de ejecutar

---

### Fase 8 — Compactación de numeración: COMPLETA ✓

**Objetivo:** renombrar archivos para eliminar huecos en la numeración causados al purgar archivos
no deseados (memes, imágenes de texto, etc.). Los registros de archivos purgados se preservan
en `catalog.db` para evitar re-descargas.

**Nota:** los archivos purgados ya no se re-descargan — el registro en `catalog.db` persiste y
`url_exists()` / `hash_exists()` lo detectan aunque el archivo no exista en disco.
La compactación es puramente de organización (numeración sin huecos).

#### Fase 8a — Lógica de compactación en `catalog.py`
- [x] `get_numbered_files(folder)` → lista ordenada de `(counter, filename, hash)` — counter extraído del nombre físico (no del campo DB)
- [x] `plan_compaction(files)` → lista de `(old_name, new_name, hash, new_counter)` — compara nombres, no contadores
- [x] `apply_compaction(folder, plan, new_total)` → rename en dos fases + UPDATE DB en transacción atómica:
  - Fase 1: `old_name` → `old_name.tmp` (todos)
  - Fase 2: `old_name.tmp` → `new_name` (todos)
  - Paso 1 DB: `UPDATE SET filename='_purged_'||hash WHERE filename=new_name AND hash!=moving_hash` (neutraliza registros fantasma)
  - Paso 2 DB: `UPDATE SET filename=new_name, counter=new_counter WHERE hash=?` (por clave primaria)
  - UPDATE `meta` SET `value = new_total` WHERE `key = 'counter'`
- [x] Test en producción: Hoovesart 2026 archivos OK, RuiDX 6331 archivos OK — verificación SHA-256 al 100%

#### Fase 8b — CLI
- [x] `cli.py`: comando `cherry-dl compact <profile_name_or_id>`
  - Muestra plan: "N archivos a renombrar, M total"
  - Solicita confirmación interactiva: `¿Continuar? [s/N]`
  - Flag `--yes` para omitir confirmación (uso en scripts)
  - Flag `--dry-run` para ver el plan (primeros 20) sin ejecutar

#### Fase 8c — TUI
- [x] `tui/app.py`: botón "⊟ Compactar" en `ArtistScreen` (junto al botón dedup)
- [x] `CompactConfirmModal`: muestra total + N a renombrar, Cancelar con focus, Confirmar
- [x] `_start_compact` / `_do_compact`: aplica compactación + mensaje en log
- [x] Mensaje final: "Compactación completa — N archivos renombrados"

#### Test en producción (2026-03-22)
- [x] Migración ejecutada: 13 perfiles → estructura `{download_dir}/{artista}/`
- [x] Compactación ejecutada en Hoovesart (1703 renombrados) y RuiDX (6346 OK)
- [x] Verificación SHA-256 completa: 8372 archivos — 0 mismatches, 0 faltantes
- [x] Bug `apply_compaction`: columna `counter` en DB no se actualizaba → plan fantasma en segunda ejecución — corregido
- [x] Bug TUI SettingsScreen: `cfg.download_path = ...` → `download_path` es `@property` sin setter → corregido a `cfg.download_dir = ...`
- [x] Bug TUI ArtistScreen: eliminar URL lanzaba `TypeError: int() argument must be a string... not 'RowKey'` → `int(keys[row_idx].value)`
- [x] Bug organizer.py: pre-scan usaba `dest_root/site/artista/` (estructura vieja) creando carpeta duplicada separada de la de descarga → eliminado segmento `/ site /`
- [x] Bug TUI semáforo invisible: `dock: bottom` competía con `Footer` (ambos en borde inferior, el panel del OS ocultaba las últimas filas) → semáforo movido inline con label "ACTIVIDAD", `Footer` eliminado de `ArtistScreen`
- [x] Bug TUI semáforo siempre gris: `#semaphore { color: #8888aa }` (ID) sobreescribía `.status-running` (clase) por mayor especificidad CSS → `color` eliminado del rule de ID
- [x] Bug `database is locked` con 3+ workers: SQLite fallaba inmediatamente en escrituras concurrentes → `aiosqlite.connect(timeout=30)` + `PRAGMA journal_mode=WAL` en `init_catalog`
- [x] Bug producer silencioso: errores de red durante paginación eran tragados por `gather(return_exceptions=True)` → resultado del producer validado post-gather, error logueado + semáforo en rojo
- [x] Semáforo: estado "done" azul (todo correcto) vs "cancelled" amarillo (pendientes o errores parciales)

---

### Fase 9 — Descarga incremental: COMPLETA ✓ (2026-03-24)
- [x] `base.py`: `iter_files(since: datetime | None)` + helper `parse_date_utc()`
- [x] `kemono.py`: para paginación cuando `post["published"] < since` (newest-first)
- [x] `patreon.py`: para paginación cuando `post["published_at"] < since` (newest-first)
- [x] `pixiv.py`: omite obras con `createDate < since` en `_process_batch` (sin llamadas extras)
- [x] TUI: botón "↑ Actualizar" — pasa `last_synced` de cada URL como `since` al template
- [x] Smoke test: imports, `parse_date_utc` (7 casos), firmas, TUI

---

### Features
- [ ] Verificación periódica automática (scheduler interno)
- [ ] Exportar índice a CSV/JSON
- [ ] Vista de estadísticas globales (total artistas, archivos, tamaño en disco)

---

## Comandos CLI actuales (Fase 1)
```bash
cherry-dl download <url>
cherry-dl download <url> --workers 5
cherry-dl organize --site kemono --artist foo /ruta
cherry-dl relink --artist foo /nueva/ruta
cherry-dl status
cherry-dl config set download_dir /mi/coleccion
cherry-dl config show
cherry-dl gui      # GUI PySide6 (legado)
cherry-dl tui      # TUI Textual (nueva interfaz por defecto)
```
