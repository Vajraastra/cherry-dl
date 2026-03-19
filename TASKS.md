# TASKS — cherry-dl

## Estado actual (2026-03-19)
- [x] Scaffolding del proyecto
- [x] Módulos base (config, hasher)
- [x] Base de datos (catalog, index)
- [x] Engine de descargas async
- [x] Sistema de templates (base, registry, kemono)
- [x] Organizador de archivos externos
- [x] CLI (download, organize, status, relink, config)
- [x] GUI Dear PyGui — DESCARTADA (reemplazar con PySide6)

---

## FASE 2 — Perfiles de Artista + GUI PySide6

### Sesión 1 — Fundación: Dependencias + DB + Perfiles
- [ ] `pyproject.toml` / `requirements.txt`: quitar dearpygui, agregar PySide6 y qasync
- [ ] `index.py`: agregar tablas `profiles` y `profile_urls` + funciones CRUD
- [ ] `index.py`: migración automática de `artists` existentes → perfiles implícitos
- [ ] Nuevo módulo `profiles.py`: lógica de negocio de perfiles
      - create_profile(display_name, primary_url, folder_path)
      - add_url_to_profile(profile_id, url, site, artist_id)
      - get_all_profiles() → list[ProfileData]
      - get_profile(profile_id) → ProfileData
      - delete_profile(profile_id)
      - resolve_artist_name(url) → str  (llama al template correspondiente)
- [ ] Actualizar `run.sh` para las nuevas dependencias

### Sesión 2 — GUI: Scaffold + Vista de Perfiles (pantalla principal)
- [ ] Eliminar `gui/app.py`, `gui/bridge.py`, `gui/native_dialog.py` (Dear PyGui)
- [ ] Nuevo `gui/theme.py`: QSS cherry-dl (paleta oscura, colores cherry)
- [ ] Nuevo `gui/app.py`: QMainWindow + QStackedWidget (máquina de vistas)
- [ ] Nuevo `gui/views/profiles_view.py`: lista de perfiles con tabla
      - Columnas: Artista | Fuentes | Archivos | Tamaño | Estado
      - Botón [+ Nuevo Artista]
      - Botón [⟳ Verificar todo]
      - Barra de actividad global en la parte inferior (descargas activas)
      - Click en fila → navegar a detalle del artista
- [ ] qasync: fusionar event loop asyncio + Qt

### Sesión 3 — GUI: Wizard nuevo artista + Vista de detalle
- [ ] Nuevo `gui/views/new_profile_wizard.py`:
      - Campo URL principal con detección de sitio automática
      - Botón [Obtener nombre desde API] + campo manual
      - Preview de carpeta destino
      - URLs adicionales opcionales
      - Workers, filtro de extensiones
      - [Crear perfil] / [Crear y Descargar]
- [ ] Nuevo `gui/views/artist_detail_view.py`:
      - Header: nombre, carpeta, total archivos/tamaño, última sync
      - Lista de fuentes con toggle activar/desactivar y botón borrar
      - Botón [+ Agregar URL] con detección de sitio
      - Controles: workers, filtro ext, pre-scan folder
      - Botones: [Verificar actualizaciones] [▶ Descargar / Actualizar]
      - Sección de progreso: barra + worker activo + log de actividad
      - [← Volver] a la lista principal

### Sesión 4 — GUI: Configuración + CLI + Estabilización
- [ ] Nuevo `gui/views/settings_view.py`: migrar settings tab actual
- [ ] CLI: nuevos comandos `cherry-dl profile`
      - `profile list`
      - `profile create <url> [--name NAME]`
      - `profile add-url <profile_id> <url>`
      - `profile update <profile_id>`
      - `profile check <profile_id>`
      - `profile update-all`
- [ ] Pruebas end-to-end: descarga real con perfil
- [ ] Actualizar BITACORA.md con decisiones y errores encontrados

---

## Backlog post-Fase 2

### Templates pendientes
- [ ] DeviantArt (tiene API oficial)
- [ ] Pixiv Fanbox
- [ ] Patreon directo

### Features
- [ ] Tags opcionales por template (tabla separada en catalog.db)
- [ ] Exportar índice a CSV/JSON
- [ ] Verificación periódica automática (scheduler interno)

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
cherry-dl gui
```
