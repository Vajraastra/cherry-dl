# TASKS — cherry-dl

## Estado actual (2026-03-20)

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

## Backlog post-Fase 2

### CLI — pendiente
- [ ] `cherry-dl profile list`
- [ ] `cherry-dl profile create <url> [--name NAME]`
- [ ] `cherry-dl profile add-url <profile_id> <url>`
- [ ] `cherry-dl profile update <profile_id>`
- [ ] `cherry-dl profile check <profile_id>`
- [ ] `cherry-dl profile update-all`

### Templates pendientes
- [ ] Pixiv Fanbox (directo, no via Kemono)
- [ ] DeviantArt (tiene API oficial)
- [ ] Patreon directo

### Features
- [ ] Verificación periódica automática (scheduler interno)
- [ ] Tags opcionales por template (tabla separada en catalog.db)
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
