# BITACORA — cherry-dl

> **Propósito de este archivo:** registro de producción y mapa técnico del proyecto.
> Una sesión nueva debe poder leer este archivo y entender completamente la arquitectura,
> el estado actual y los próximos pasos sin necesidad de contexto previo.

---

## Arquitectura general

cherry-dl es un **mass downloader modular** para Linux/Windows escrito en Python 3.10+.

### Estructura de carpetas del proyecto
```
cherry_dl/
  __init__.py
  __main__.py
  config.py          — configuración del usuario (UserConfig, NetworkConfig, rutas)
  hasher.py          — SHA-256 de bytes/archivos
  catalog.py         — catalog.db por artista (registro de archivos descargados)
  index.py           — index.db central en ~/.cherry-dl/ (registro de artistas)
  engine.py          — DownloadEngine: pool async de workers, retry, DDG bypass
  organizer.py       — incorpora archivos externos descargados fuera de cherry-dl
  cli.py             — comandos CLI con typer
  templates/
    base.py          — clases base: SiteTemplate, ArtistInfo, FileInfo
    _registry.py     — registro global de templates
    __init__.py
    kemono.py        — template para kemono.cr (único template implementado)
  gui/               — GUI (POR REEMPLAZAR en Fase 2 con PySide6)
    app.py           — GUI Dear PyGui (DESCARTADA)
    bridge.py        — bridge asyncio ↔ DPG via queue.Queue (DESCARTADA)
    native_dialog.py — diálogos nativos (DESCARTADA)
    __init__.py
```

### Flujo de descarga (Fase 1)
```
CLI: cherry-dl download <url>
  → template detecta el sitio por URL
  → get_artist_info(url) → ArtistInfo
  → get_or_create_artist() en index.db
  → init_catalog() en carpeta del artista
  → iter_files() → stream de FileInfo
  → por cada FileInfo:
      url_exists() en catalog.db    → skip si ya descargado (DEDUP POR URL)
      hash_exists() en catalog.db   → skip si SHA-256 ya existe (DEDUP POR HASH)
      engine.download()             → descarga a disco
      add_file() en catalog.db      → registrar hash + url_source + metadata
```

### Estructura de carpetas en disco
```
{download_dir}/
  {site}/
    {artist_name}/
      catalog.db      ← viaja con los archivos, colección auto-contenida
      archivo_0001.jpg
      archivo_0002.png
      ...
```

---

## Bases de datos

### catalog.db (por carpeta de artista)
```sql
CREATE TABLE files (
    hash        TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    url_source  TEXT,            -- URL original del archivo (dedup pre-descarga)
    date_added  INTEGER NOT NULL,
    file_size   INTEGER,
    counter     INTEGER          -- número secuencial global del artista
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0  -- key='counter' → contador incremental
);
```
**Nota:** `url_source` y `url_exists()` ya están implementados — dedup por URL funciona hoy.

### index.db (central en ~/.cherry-dl/)
```sql
CREATE TABLE sites (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    url  TEXT
);

CREATE TABLE artists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    name        TEXT NOT NULL,
    artist_id   TEXT NOT NULL,     -- ID en el sitio de origen
    folder_path TEXT NOT NULL,
    UNIQUE(site_id, artist_id)
);
```

### index.db — extensión Fase 2 (PENDIENTE DE IMPLEMENTAR)
```sql
CREATE TABLE profiles (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    folder_path  TEXT NOT NULL UNIQUE,  -- {download_dir}/{site_primario}/{nombre}/
    primary_site TEXT NOT NULL,         -- site de la URL con la que se creó
    created_at   TEXT,
    last_checked TEXT
);

CREATE TABLE profile_urls (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    site        TEXT NOT NULL,
    artist_id   TEXT,
    enabled     INTEGER DEFAULT 1,
    last_synced TEXT,
    file_count  INTEGER DEFAULT 0
);
```
**Migración:** los `artists` existentes se convierten en perfiles implícitos
(un perfil = una URL) automáticamente al ejecutar `init_index()` en Fase 2.

---

## Stack de dependencias

```toml
# pyproject.toml — estado Fase 1
httpx[http2]>=0.27.0   # cliente HTTP async con HTTP/2
aiosqlite>=0.20.0      # SQLite async
typer>=0.12.0          # CLI
rich>=13.7.0           # progress bars y output en CLI
pydantic>=2.7.0        # modelos de configuración
tomli>=2.0.0           # parseo de TOML en Python < 3.11
tenacity>=8.3.0        # retry con backoff exponencial
dearpygui>=1.11.0      # GUI DESCARTADA — reemplazar con PySide6 + qasync
```

**Cambio Fase 2:** quitar `dearpygui`, agregar `PySide6>=6.7.0` y `qasync>=0.23.0`.

---

## Template: kemono.cr

### Quirks críticos de la API (estado verificado 2026-03-18)

**DDoS-Guard bypass:**
- Header obligatorio: `Accept: text/css` (documentado por el creador en el 403)
- Cookies a persistir en `~/.cherry-dl/session.json`: `__ddg1_`, `__ddg8_`, `__ddg9_`, `__ddg10_`

**Endpoints:**
```
GET /api/v1/{service}/user/{id}/posts        ← CORRECTO Y ESTABLE ✓
GET /api/v1/{service}/user/{id}/posts-legacy ← ELIMINADO, retorna 404 ✗
GET /api/v1/creators                         ← lista de creadores ✓
```

**Paginación:**
- Primera llamada: `?o=0` (el bug anterior que perdía 3 posts fue corregido en el servidor)
- Siguientes: `?o=50`, `?o=100`, etc.
- 400 o 404 en paginación = offset fuera de rango → fin normal

**Hash en path de archivo:**
Kemono expone el SHA-256 en la URL: `/data/{2chars}/{2chars}/{sha256_64chars}/{filename}`
Función `_hash_from_path()` en kemono.py lo extrae para dedup pre-descarga.

**Rate limiting:** 429 → esperar 35s + 10s/intento. Engine ya lo maneja.

---

## GUI — Fase 2: PySide6

### Por qué se descartó Dear PyGui
Dear PyGui usa paradigma **immediate-mode**: cada frame se reconstruye la UI completa.
El nuevo scope requiere:
- Navegación entre vistas (profiles → detalle → wizard → settings)
- Profile cards con estado persistente
- Layout complejo por artista
DPG no escala bien a esto. Además, los tests mostraron limitaciones prácticas.

### Por qué PySide6 + qasync
- **qasync** fusiona el event loop de asyncio dentro del event loop de Qt
- Elimina el bridge `queue.Queue + hilo daemon` — `await` funciona directamente en slots
- `QStackedWidget` para navegación de vistas
- QSS para el tema cherry (CSS-like, completo control visual)
- LGPL, maduro, excelente en Linux/KDE

### Arquitectura GUI planeada
```
QMainWindow
  └─ QStackedWidget (router de vistas)
       ├─ view_profiles     ← pantalla principal (lista de artistas)
       ├─ view_artist_detail ← detalle/descarga por artista
       ├─ view_new_profile  ← wizard de creación
       └─ view_settings     ← configuración global
```

### Vista principal (profiles_view)
```
[+ Nuevo Artista]  [⟳ Verificar todo]  Buscar:[_____]
────────────────────────────────────────────────────
Artista    | Fuentes        | Archivos | Estado
────────────────────────────────────────────────────
Yoruichi   | kemono/patreon | 847 arch | ✓ Al día
Artist2    | pixiv · fanbox | 203 arch | ⚠ 14 nuevos
────────────────────────────────────────────────────
▼ Actividad global  [████░░] 2 activas · 143 desc
```
Click en fila → navegar a vista de detalle del artista.

### Vista de detalle (artist_detail_view)
```
[← Volver]                         [✏ Editar] [🗑 Borrar]
NombreArtista
{carpeta} · 847 archivos · 2.3 GB · Última sync: 2026-03-15
─────────────────────────────────────────────────────────
Fuentes:
✓  kemono/patreon  kemono.cr/patreon/user/123  847 arch  [✕]
✓  pixiv           pixiv.net/users/456          sin sync  [✕]
[+ Agregar URL]
─────────────────────────────────────────────────────────
Workers: [3]   Filtro ext: [_______] □ Excluir
Pre-scan: [___________________________] [Examinar] [✕]
[Verificar actualizaciones]    [▶ Descargar / Actualizar]
─────────────────────────────────────────────────────────
[████████░░] 143/847  ↻ thumbnail_0234.jpg
✓ 143  ~ 12 dupl  ✗ 0 err
[log de actividad]
```

### Wizard nuevo artista (new_profile_wizard)
```
URL principal: [https://kemono.cr/patreon/user/123] [Examinar]
               ✓ Kemono · Servicio: Patreon · ID: 123
Nombre: [Obtener desde API →]  "Yoruichi"  □ Editar manual
Carpeta: {download_dir}/kemono/Yoruichi/   [Cambiar]
URLs adicionales: [+ Agregar]
Workers: [3]   Filtro: [_______] □ Excluir
                          [Crear perfil]  [Crear y Descargar]
```

### Sistema de perfiles
- **Carpeta base = primer servicio usado:** `{download_dir}/{site_primario}/{nombre}/`
- Fuentes adicionales descargan a la MISMA carpeta → un solo `catalog.db`
- Dedup automático: `url_source` (pre-descarga) + SHA-256 (post-descarga)
- Perfil implícito: si se usa `cherry-dl download <url>` sin perfil, se crea uno automáticamente

---

## 2026-03-20 — TUI Textual (Fase 3) — COMPLETA

### Motivación
La GUI PySide6 requería un entorno gráfico (X11/Wayland). Se decidió construir
una TUI con **Textual** para mayor portabilidad (funciona en terminal SSH, tmux, etc.)
y menor overhead de dependencias en sistemas headless.

### Arquitectura TUI
```
cherry_dl/tui/
  __init__.py       — módulo
  app.py            — app completa (~1500 líneas)
  theme.tcss        — tema cherry oscuro (misma paleta que GUI PySide6)
```

**Pantallas y clases clave:**
- `ProfilesScreen` — lista de perfiles con DataTable + toolbar de botones
- `ArtistScreen` — detalle/descarga: workers, log, semáforo, contadores, barra de estado docked
- `SettingsScreen` — configuración global con grid 2 columnas
- `NewProfileModal` — wizard completo: resolver URL via API, nombre/carpeta auto, workers, filtro ext, "Crear y descargar"
- `AddUrlModal` — modal agregar URL a perfil existente
- `InputContextMenu` — menú contextual (clic derecho): Pegar / Seleccionar todo / Limpiar
- `ClipInput` — subclase de Input con `action_paste` + `on_paste` usando portapapeles del sistema
- `WorkerRow` — fila de worker con barra de progreso y velocidad

**Dependencia nueva:** `textual>=0.70.0` (instalado: 8.1.1)
**Comando nuevo:** `cherry-dl tui`
**`run.sh`:** sin args → lanza TUI (antes lanzaba GUI PySide6)

### Bugs corregidos en esta sesión

| # | Problema | Solución |
|---|----------|----------|
| 1 | `border-radius` inválido en TCSS | Eliminado — Textual no lo soporta |
| 2 | `status-bar` desaparecía — `1fr` del log consumía todo el espacio | `dock: bottom` en CSS |
| 3 | Workers input invisiblemente pequeño | `width: 6` → `width: 10` |
| 4 | `ProfilesScreen` sin botones — solo teclas | Toolbar con 4 botones añadida |
| 5 | `NewProfileModal` sin opciones (solo 3 campos) | Reescrito: resolver URL, nombre/carpeta auto, workers, filtro, "Crear y descargar" |
| 6 | `_create_profile` pasaba `url=` a `create_profile()` que no lo acepta | Separado en `create_profile()` + `add_profile_url()` + `update_profile_ext_filter()` |
| 7 | Sin soporte de portapapeles | `_read_clipboard()` (wl-paste/xclip/xsel) + `ClipInput` + `InputContextMenu` |

### Portapapeles — arquitectura
- `_read_clipboard()`: llama `wl-paste --no-newline` (Wayland) → xclip → xsel como fallback
- `ClipInput(Input)`: sobreescribe `action_paste` (ctrl+v via Textual) y `on_paste` (bracketed paste del terminal)
- `InputContextMenu`: modal con 3 opciones, se abre con clic derecho en cualquier Input
- App-level `ctrl+v` binding como capa extra de fallback

---

## 2026-03-18 — Inicio del proyecto

### Arquitectura definida
- Descargador modular con templates por sitio
- BD distribuida: `catalog.db` por artista + `index.db` central en `~/.cherry-dl/`
- Engine async con pool de workers (`httpx` + `asyncio`)
- Deduplicación por SHA-256 y URL de origen

### Kemono.cr — Hallazgos críticos de API
**DDoS-Guard:** requiere `Accept: text/css` (documentado por el creador en el 403).
**Bug paginación antiguo:** `?o=0` perdía los primeros 3 posts → CORREGIDO en el servidor.
**Endpoint legacy:** `/posts-legacy` fue eliminado → usar `/posts`.

---

## 2026-03-19 — Decisión: Perfiles de artista + GUI PySide6

### Motivación
El usuario necesita gestionar artistas que publican en múltiples servicios
(ej: Kemono archiva el historial de Patreon, pero si ese historial se elimina
de Kemono el usuario recurre a Pixiv o Fanbox directamente).

Solución: **perfil de artista** que agrupa N URLs de diferentes servicios
bajo una sola carpeta, con dedup cross-service via `url_source` + SHA-256.

### GUI Dear PyGui descartada
Después de pruebas prácticas, el paradigma immediate-mode de DPG no se adapta
al nuevo scope. Se migra a **PySide6 + qasync**.

---

## Errores encontrados y soluciones

### 2026-03-20 — file_count y last_synced nunca se actualizaban en la tabla de fuentes
**Síntoma:** tras completar una descarga, la tabla de fuentes seguía mostrando 0 archivos y "Nunca" en última sync. El estado correcto aparecía solo al reiniciar la aplicación.
**Causa:** `_do_download` no llamaba a `update_profile_url_sync` al terminar cada fuente.
**Solución:** agregar `update_profile_url_sync(INDEX_DB, pu["id"], file_count=new_count)` tras el `asyncio.gather`, y llamar `_refresh_source_row()` para actualizar la UI inmediatamente sin reiniciar.

### 2026-03-20 — Entrada "migrado" duplicada en tabla de fuentes
**Síntoma:** al terminar una descarga, la tabla mostraba dos filas para la misma fuente — una real (con URL) y una migrada (sin URL).
**Causa:** la migración en `init_index` creaba una entrada `url=NULL` al iniciar la app. El wizard ya había creado una entrada real con URL. Al llamar `init_index` en cada descarga, la migración volvía a insertar la entrada nula si la real no tenía `artist_id` aún.
**Solución (doble):**
  1. Llamar `update_profile_url_sync(..., artist_id=artist_info.artist_id)` al inicio de `_do_download`, antes de cualquier paginación. Esto popula `artist_id` en la entrada real.
  2. Agregar en `init_index` un DELETE que elimina entradas `url=NULL` cuando ya existe una entrada real con el mismo `artist_id` + `site`.

### 2026-03-20 — stall_timeout no se persistía en config.toml
**Síntoma:** cambiar el timeout de stall en Settings no tenía efecto tras reiniciar.
**Causa:** `save_config()` en `config.py` no incluía la línea `stall_timeout`.
**Solución:** agregar `f"stall_timeout = {config.network.stall_timeout}\n"` en `save_config`.

### 2026-03-20 — Archivos descargados múltiples veces (mismo contenido, copias con nombres diferentes)
**Síntoma:** un mismo archivo aparecía descargado varias veces con distintos prefijos `Artist_NNNNN_`.
**Causa 1 (inter-sesión):** `local_hashes` se construye al inicio de la fuente escaneando el disco. Si un archivo existía con nombre incorrecto, se intentaba renombrar Y re-descargar en la misma sesión.
**Causa 2 (intra-sesión, race condition):** el productor hacía las verificaciones de catálogo (`hash_exists`, `url_exists`) con `await` entre ellas. Dos workers podían pasar el check antes de que alguno hubiera catalogado el archivo.
**Solución:** refactorizar al patrón **repartidor/workers**:
  - *Repartidor* (producer): solo garantiza URLs únicas con `seen_urls: set[str]`. Sin consultas al catálogo.
  - *Workers*: reciben una URL única cada uno, verifican catálogo por su cuenta, descargan y catalogan.
  - `in_progress_hashes: set[str]` compartido entre workers: se verifica y se agrega sin await intermedio (operación atómica en asyncio), previniendo que dos workers procesen el mismo hash concurrentemente.

---

## Decisiones técnicas

| Fecha      | Decisión | Razón |
|------------|----------|-------|
| 2026-03-18 | `httpx` sobre `aiohttp` | API más simple, soporta HTTP/2 nativo |
| 2026-03-18 | `tenacity` para retries | Backoff exponencial declarativo, menos boilerplate |
| 2026-03-18 | SQLite por artista | Portabilidad: las carpetas se pueden mover sin perder índice |
| 2026-03-18 | Python 3.10+ | `match/case` para respuestas API, amplia disponibilidad |
| 2026-03-19 | Dear PyGui → PySide6 | DPG immediate-mode no escala al nuevo scope. PySide6 + qasync elimina el bridge de queue.Queue + hilo daemon |
| 2026-03-19 | Perfiles de artista | Agrupar N fuentes bajo una carpeta. Carpeta base = primer servicio. Dedup via url_source (ya existe) + SHA-256 |
| 2026-03-19 | url_source ya implementado | catalog.py ya tenía url_source y url_exists(). No requiere cambios en el engine |
| 2026-03-20 | Patrón repartidor/workers | Productor solo dedup de URL, workers hacen consultas al catálogo. Elimina race conditions de dedup entre workers concurrentes |
| 2026-03-20 | `in_progress_hashes` en workers | Set compartido verificado/actualizado sin await intermedio (atómico en asyncio). Previene descarga doble por mismo hash en URLs diferentes |
| 2026-03-20 | `UPDATE … RETURNING value` en next_counter | Operación atómica en SQLite 3.35+. Elimina gap UPDATE→SELECT que podía asignar el mismo contador a dos workers |
| 2026-03-20 | `asyncio.to_thread` para I/O en engine | `write_bytes`, `sha256_bytes` y `b"".join` son síncronos y bloqueaban el event loop. Extraídos a `_finalize_download()` en thread executor |
| 2026-03-20 | Escritura atómica `.tmp` → rename | Previene archivos corruptos si el proceso termina mientras escribe. Rename es atómico en POSIX |
| 2026-03-20 | Limpieza de `.tmp` en `_build_local_hash_map` | Elimina archivos `.tmp` huérfanos de sesiones interrumpidas antes de iniciar descarga |

---

## 2026-03-20 — GUI Fase 2: implementación completa + auditoría

### Implementado en esta sesión

**Nuevas funciones en `artist_detail_view.py`:**
- Botón "⊘ Deduplicar": `_do_deduplicate()` escanea el catálogo, encuentra archivos con mismo hash en disco (nombres distintos), elimina duplicados y reporta espacio liberado
- Semáforo de estado: `_lbl_semaphore` con 5 estados visuales — idle(gris), running(verde), cancelled(amarillo), error(rojo), done(azul)
- Panel de workers en `QScrollArea`: siempre visible, max 5 filas visibles, scrollable si hay más
- Log expandido con `stretch=1` y mensajes detallados con razón de skip/rename/error
- `_refresh_source_row(url_id, file_count)`: actualiza la fila de la fuente en la tabla sin reiniciar la app
- Contadores en barra inferior: descargados, saltados, errores, diferidos

**Correcciones de bugs (auditoría sistemática):**

| # | Archivo | Problema | Fix |
|---|---------|----------|-----|
| 1 | `engine.py` | `write_bytes`+`sha256` bloqueaban el event loop | Extraído a `_finalize_download()` + `asyncio.to_thread()` |
| 2 | `catalog.py` | `next_counter` UPDATE→SELECT con gap vulnerable a race | `UPDATE … RETURNING value` (una sola instrucción atómica) |
| 3 | `catalog.py` | `row[0]` sin validar si meta tabla está vacía | `RuntimeError` con mensaje descriptivo |
| 4 | `artist_detail_view.py` | `unlink(result.dest)` tras rename fallido perdía el archivo | Si rename falla, eliminar `old_path` (duplicado), no `result.dest` |
| 5 | `artist_detail_view.py` | `assert result.file_hash is not None` desactivable con `-O` | Reemplazado por `if/continue` con log de error |
| 6 | `profiles_view.py` | `ensure_future` sin catch → pantalla en blanco infinita si falla | `add_done_callback(_on_task_done)` en todos los fire-and-forget |
| 7 | `catalog.py` | Sin índice en `url_source` → table scan O(n) por archivo | `CREATE INDEX idx_url_source ON files(url_source)` |
| 8 | `artist_detail_view.py` | Doble-clic en "Eliminar URL"/"Agregar URL" → operaciones duplicadas | Botón deshabilitado mientras la tarea asyncio corre |

**Test de integración con URL real (kemono.cr/fanbox/user/55972648):**
- 10/10 checks pasados: init_catalog, next_counter concurrente (5 workers), get_artist_info, iter_files, descarga real (115KB), add_file, hash_exists, url_exists, dedup, sin .tmp huérfanos
