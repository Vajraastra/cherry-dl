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
  {artist_name}/          ← artist-first desde Fase 7 (sin subcarpeta de sitio)
    catalog.db            ← viaja con los archivos, colección auto-contenida
    archivo_0001.jpg
    archivo_0002.png
    ...
```
Migración a estructura nueva: `cherry-dl migrate-structure [--dry-run]`

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

## 2026-03-21 — Templates por Tags (Booru): DESCARTADO

**Razón:** Un usuario que busca en boorus no busca una descarga masiva — busca cosas muy
específicas. Descargar masivamente un booru genera trabajo extra de filtrado post-descarga
y uso innecesario de espacio en disco. La feature va en contra del flujo natural de uso.
El soporte de boorus queda fuera del scope del proyecto.


---

## 2026-03-21 — Fanbox descartado

**Razón:** La API de Fanbox cambió significativamente. Los endpoints `post.get` y `post.listCreator` ya no incluyen los campos `type` ni `body` con el contenido descargable (imágenes/archivos). El endpoint solo devuelve metadata del post. No se encontró un endpoint alternativo que expusiera el contenido de forma programática.

Adicionalmente, se observó que muchos creadores en Fanbox no usan la plataforma para distribuir archivos — en cambio, postean links a Dropbox, MEGA y servicios externos. Esto hace que el valor de un template de Fanbox sea bajo respecto al esfuerzo de ingeniería reversa de la API.

**Eliminado:** `auth/fanbox.py`, `templates/fanbox.py`, referencias en `_registry.py` y `tui/app.py`.

---

## 2026-03-21 — Template Pixiv (Fase 6)

### Decisión de arquitectura: web AJAX API + cookies (no OAuth)

Pixiv tiene dos APIs:
- **App API** (`app-api.pixiv.net`): requiere OAuth refresh_token. El usuario
  necesita instalar `gppt` o hacer un flujo PKCE manual — fricción innecesaria.
- **Web AJAX API** (`www.pixiv.net/ajax/`): funciona con la cookie PHPSESSID del
  browser. Misma mecánica que Patreon. Estable desde 2018, usada por gallery-dl.

**Decisión:** usar la web AJAX API con browser-cookie3. Cero instalaciones extras.
El usuario solo inicia sesión en Pixiv en su browser habitual (que probablemente
ya tiene abierto) y cherry-dl lee la cookie automáticamente.

### Arquitectura implementada

**`cherry_dl/auth/pixiv.py`**
- Patrón idéntico a `auth/patreon.py` — misma estructura, distinto sitio
- `ensure_pixiv_session()` → 3 pasos: session.json → browser_cookie3 → NeedsPixivAuth
- `load_from_browser()` — lee cookies de Firefox/Chrome/Brave/Edge via browser_cookie3
- `NeedsPixivAuth` — excepción que señaliza a la TUI para mostrar PixivAuthModal
- Cookie clave: `PHPSESSID`. También persiste: `device_token`, `p_ab_id*`, `p_ab_d_id`
- TTL de sesión guardada: 30 días. Al recibir 401/403 → `clear_pixiv_session()`

**`cherry_dl/templates/pixiv.py`** — web AJAX API
- `can_handle()` — detecta `pixiv.net/users/{id}`, `/en/users/{id}`, `member.php?id=`
- `get_artist_info()` → `GET /ajax/user/{id}?lang=en` — nombre del artista
- `iter_files()` → `_iter_all()`:
  1. `GET /ajax/user/{id}/profile/all` — todos los IDs de obras (illusts + manga)
  2. Por lotes de 48: `GET /ajax/user/{id}/illusts?ids[]=...` — detalles
  3. Por cada obra:
     - `illustType == 2` (ugoira) → `GET /ajax/illust/{id}/ugoira_meta` → ZIP
     - `pageCount == 1` → `urls.original` directo (sin request extra)
     - `pageCount > 1` → `GET /ajax/illust/{id}/pages` → lista de URLs originales
- `max_workers = 2`, `provides_file_hashes = False`
- Referer inyectado en `FileInfo.extra_headers` → engine lo usa al descargar

**`cherry_dl/templates/base.py`**
- `FileInfo.extra_headers: dict = field(default_factory=dict)` — headers por-archivo
- Permite que templates inyecten headers específicos sin cambiar el cliente base del engine

**`cherry_dl/engine.py`**
- `download(extra_headers=None)` + `_do_download(extra_headers=None)`
- httpx fusiona los headers del request con los del cliente (Referer + DDG coexisten)

**TUI (`tui/app.py`)**
- `PixivAuthModal` — mismo diseño que `PatreonAuthModal`:
  - Botón "Abrir pixiv.net/login" → `webbrowser.open()`
  - Botón "Ya inicié sesión" → `browser_cookie3` en thread → guarda cookies
- `_resolve_url` y `_do_download`: atrapan `NeedsPixivAuth` → PixivAuthModal → reintentan
- `worker_task` y cola diferida: pasan `fi.extra_headers` a `engine.download()`

### Dedup Pixiv
- `url_source = "pixiv://illust/{id}/p{n}"` — estable, no contiene tokens que expiran
- `url_source = "pixiv://ugoira/{id}"` — para ZIPs de animación
- Primera sync: descarga todo (no hay hash pre-check — Pixiv no expone SHA-256)
- Updates: `url_exists(dedup_key)` → skip O(1)

### Referer obligatorio en descargas
- i.pximg.net devuelve 403 sin `Referer: https://www.pixiv.net/`
- Solución: `FileInfo.extra_headers` transporta el Referer desde el template al engine
- El engine lo pasa como per-request header — compatible con los DDG headers del cliente

### Tipos de contenido manejados
| Tipo | illustType | Cómo | Archivo |
|------|-----------|------|---------|
| illust/manga (1 o N págs) | 0 ó 1 | `/ajax/illust/{id}/pages` | `{id}_p{i}.jpg` |
| ugoira | 2 | `/ajax/illust/{id}/ugoira_meta` → originalSrc | `{id}_ugoira.zip` |
| novel | — | NO soportado | — |

### Bugs encontrados en pruebas de integración (2026-03-21)

| # | Problema | Causa raíz | Solución |
|---|----------|------------|----------|
| 1 | `iter_files` devolvía 0 archivos | Endpoint `GET /ajax/user/{id}/illusts?ids[]=...` devuelve IDs en `body` directamente, no en `body["works"]`. La línea `body.get("body", {}).get("works", {})` siempre retornaba `{}` | Cambiar a `body.get("body", {})` |
| 2 | `iter_files` devolvía 0 archivos (causa 2) | El campo `urls.original` del endpoint batch está **vacío** — el batch solo devuelve metadatos (illustType, pageCount). No hay URL original disponible para obras de 1 página | Eliminar el branch `elif page_count == 1`; siempre llamar `/pages` para todas las obras no-ugoira |
| 3 | `engine.download()` lanzaba `TypeError: expected string or bytes-like object, got 'dict'` | `load_session()` devuelve el session.json completo. Ahora tiene `{"pixiv": {...nested dict...}}`. El engine lo pasaba íntegro a httpx como cookies. httpx falla al procesar el valor dict | En `DownloadEngine.__init__`, filtrar `_session_cookies` para incluir solo valores `isinstance(v, str)` — ignora los bloques de servicio anidados |

---

## 2026-03-21 — Sistema multi-URL + fixes (Fase 5)

### Análisis del sistema de URLs extras en perfiles

Se auditó el flujo completo de múltiples URLs por perfil. La arquitectura es correcta:
- `profiles` (1) → `profile_urls` (N): cada URL tiene su propio `site`, `enabled`, `file_count`, `last_synced`
- `_do_download` itera `for pu in profile["urls"]` secuencialmente — cada URL usa su propio template
- Todas las fuentes convergen en la misma carpeta y el mismo `catalog.db` → dedup automático cross-site
- `update_profile_url_sync` actualiza contadores por URL individual tras cada fuente

### Bugs corregidos

| # | Problema | Causa | Solución |
|---|----------|-------|----------|
| 1 | Botón "- Eliminar URL" no hacía nada | `tbl.get_row_index()` no existe en Textual DataTable → `AttributeError` silencioso en worker | Eliminadas las dos líneas rotas; se usa directamente `keys = list(tbl.rows.keys())` |
| 2 | URL `patreon.com/cw/RuiDX` no reconocida | Regex `(?:c/)?` solo acepta prefijo `c/`, no `cw/` ni otros | Cambiado a `(?:[a-z]{1,4}/)?` — acepta cualquier prefijo corto (c, cw, cr, etc.) |

### Nuevas funcionalidades

**`SiteTemplate.provides_file_hashes`** (`base.py`)
- Atributo de clase `bool`, por defecto `False`
- `KemonoTemplate`: `True` (expone SHA-256 en el path del CDN)
- `PatreonTemplate`: `False` (no expone hashes — primer scan descarga todo)
- Usado por `AddUrlModal` para mostrar advertencia al usuario

**Dedup post-descarga cross-site** (`tui/app.py` — `worker_task`)
- Tras descarga exitosa, antes de catalogar: `await hash_exists(folder, result.file_hash)`
- Si el hash ya existe en el catálogo (ej. mismo archivo descargado antes vía Kemono): borra el archivo descargado, incrementa saltados, log `≡ [duplicado — hash ya catalogado]`
- Cubre el caso: perfil con Kemono + Patreon directo donde ambos sirven el mismo contenido

**Validación en tiempo real en `AddUrlModal`**
- `on_input_changed`: llama `find_template(url)` mientras el usuario escribe
- Template encontrado + `provides_file_hashes=True`: `✓ Template: kemono`
- Template encontrado + `provides_file_hashes=False`: `✓ Template: patreon  ⚠ Este sitio no expone hashes — el primer scan descargará todo para deduplicar por hash local.`
- Sin template: `✗ No hay template para este sitio`
- `on_button_pressed`: bloquea guardado si no hay template — no guarda URLs `"unknown"`

### Test manual — resultado
Scrapping completo de perfil con Kemono + Patreon directo confirmado funcional end-to-end.
Dedup cross-site operativo: archivos de Patreon ya presentes vía Kemono se descartan
automáticamente por hash sin intervención manual.

---

## 2026-03-20 — Template Patreon (Fase 4)

### Arquitectura implementada

**`cherry_dl/auth/patreon.py`**
- `ensure_patreon_session()` — 3 pasos: session.json → browser-cookie3 → NeedsManualAuth
- `load_from_browser()` — lee cookies de Firefox/Chrome/Brave/Edge via browser-cookie3
- `NeedsManualAuth` — excepción que señaliza a la TUI para mostrar PatreonAuthModal
- `load/save/clear_patreon_cookies()` — persistencia en `session.json["patreon"]` con TTL 30 días
- `refresh_patreon_cookies()` — actualiza cookies de corta duración (__cf_bm) sin resetear session_id

**`cherry_dl/templates/patreon.py`**
- `can_handle()` — detecta `patreon.com/{username}` y variantes `/c/` y `/posts`
- `get_artist_info()` → `_resolve_campaign()` — username → campaign_id via `/api/campaigns?filter[vanity]=`
- `iter_files()` → `_iter_posts()` — paginación cursor-based via `links.next`
- `_extract_files_from_post()` — extrae `included[type=media]` + `included[type=attachment]`
- `max_workers = 2` — límite rígido, el TUI lo respeta automáticamente
- 401/403 → `clear_patreon_session()` + RuntimeError con instrucción de re-login
- 429 → espera `Retry-After` header (o 60s) y reintenta la misma página

**`cherry_dl/templates/base.py`**
- `FileInfo.url_source: str = ""` — URI canónica para dedup (opcional)
- `FileInfo.dedup_key` — property: `url_source if url_source else url`

**TUI (`tui/app.py`)**
- 6 puntos cambiados: `fi.url` → `fi.dedup_key` en `url_exists()` y `add_file(url_source=)`
- `_do_download`: cap de workers via `cls.max_workers` antes de crear el engine
- `PatreonAuthModal`: abre browser del sistema con `webbrowser.open()`, detecta cookies
  con `browser_cookie3` en thread, guarda en session.json, dismiss(True/False)
- `_resolve_url`: atrapa `NeedsManualAuth` → muestra modal → reintenta si auth OK
- `_do_download`: atrapa `NeedsManualAuth` → muestra modal → reintenta `get_artist_info`

### Dedup Patreon
- `url_source = "patreon://media/{id}"` o `"patreon://attachment/{id}"`
- IDs estables de la API — no contienen tokens que expiran
- Primera descarga: full (Patreon no expone hash en URL)
- Updates: `url_exists(dedup_key)` → skip O(1) via índice `idx_url_source`

### Decisión: browser-cookie3 sobre Playwright
**Motivación:** Playwright requería descargar Chromium (~167MB) y tenía problemas de
instalación en Fedora Atomic/Silverblue (EDQUOT en Btrfs). Además, usar el browser
real del usuario es más seguro: mismas cookies, mismo fingerprint, mismo device_id.
**Implementación:** `browser-cookie3` lee directamente del profile del browser del
sistema. Si no encuentra sesión activa, `PatreonAuthModal` abre `patreon.com/login`
en el browser del usuario con `webbrowser.open()` (stdlib) y relee las cookies al
confirmar. Sin binarios adicionales, sin cuotas, sin ventanas de Playwright.

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

---

## 2026-03-21 — Fase 7: Reestructuración de carpetas (artist-first) — COMPLETA

### Cambios implementados

- `tui/app.py` → `_auto_folder()`: eliminado `/ self._site /` del path de carpeta nueva
- `cli.py` → `_download()`: eliminado `/ artist.site /` del path de descarga
- `cli.py` → nuevo comando `cherry-dl migrate-structure [--dry-run]`

### Test en producción (2026-03-21)

Ejecución exitosa sobre 13 perfiles reales (~136 GB):
- Dry-run confirmó plan correcto antes de ejecutar
- Todos los perfiles migrados en una sola pasada (0 errores)
- `cherry-dl status` posterior confirmó rutas actualizadas y conteos idénticos
- RuiDX (Kemono + Patreon) comparte correctamente una sola carpeta unificada

---

## 2026-03-21 — Planificación Fase 7: Reestructuración de carpetas (artist-first)

### Problema

La estructura actual `{download_dir}/{site}/{artista}/` atomiza la colección cuando un artista
publica en múltiples sitios. El sistema de perfiles multi-URL ya reconoce que un artista es
una entidad unificada — la carpeta debería reflejarlo.

**Ejemplo actual (ineficiente):**
```
{download_dir}/
  kemono/
    NombreArtista/
      catalog.db
      artista_0001.jpg ...
  patreon/
    NombreArtista/
      catalog.db
      artista_0001.jpg ...
  pixiv/
    NombreArtista/
      catalog.db
      artista_0001.jpg ...
```

**Estructura objetivo:**
```
{download_dir}/
  NombreArtista/
    catalog.db    ← un solo catalog.db para todas las fuentes
    artista_0001.jpg
    artista_0002.jpg ...
```

### Diseño

**Cambio en construcción de paths:**
- Actualmente: `folder_path = {download_dir}/{site_del_primer_url}/{nombre_perfil}/`
- Nuevo: `folder_path = {download_dir}/{nombre_perfil}/`
- El segmento de sitio se elimina completamente — nombres de perfil son únicos (definidos por usuario)

**Uniqueness:** los perfiles tienen nombre definido por el usuario → no puede haber dos perfiles
con el mismo nombre → no hay conflictos de carpeta.

**Sin cambios en dedup:** un solo `catalog.db` por perfil ya era el diseño actual para multi-URL.
El cambio es solo de estructura de directorios.

### Migración de librerías existentes

Comando `cherry-dl migrate-structure`:
1. Lee todos los perfiles de `index.db`
2. Por cada perfil: calcula `nueva_ruta = {download_dir}/{nombre_perfil}/`
3. Si `carpeta_actual == nueva_ruta` → skip (ya está migrado)
4. Si `carpeta_actual` existe → mueve todos los archivos + `catalog.db` a `nueva_ruta`
5. Actualiza `folder_path` en `index.db`
6. Reporte final: N migrados, M ya en estructura nueva, K errores

**Flags:**
- `--dry-run` → muestra tabla `viejo → nuevo` sin ejecutar ningún movimiento
- Sin flags → muestra el plan y pide confirmación interactiva antes de ejecutar

---

## 2026-03-22 — Test de producción: Fase 7 + Fase 8

### Migración de carpetas (Fase 7)

Ejecutada `cherry-dl migrate-structure` sobre la colección real (~136 GB, 13 perfiles).

- Dry-run revisado antes de ejecutar — plan correcto en todos los perfiles
- Resultado: **13/13 migrados**, 0 errores, estructura `{download_dir}/{artista}/`
- `cherry-dl status` posterior confirmó conteos y rutas actualizadas
- RuiDX (Kemono + Patreon) comparte una sola carpeta unificada correctamente

### Compactación (Fase 8)

Ejecutada `cherry-dl compact "Hoovesart"` como primer test:

- Hoovesart: 2026 archivos, 1703 renombrados — completado sin errores

**Bug detectado post-compactación:**
Segunda ejecución del dry-run seguía mostrando 1703 archivos "a renombrar" con
Antes = Después (plan fantasma). Diagnóstico: `apply_compaction` actualizaba
`filename` en la DB pero NO actualizaba la columna `counter`. La siguiente llamada
a `get_numbered_files` extraía el counter del nombre físico (correcto), pero
`plan_compaction` en la sesión anterior comparaba counter DB con nuevo counter
secuencial — como el counter DB era stale, todo el plan se regeneraba como
"pendiente" aunque los archivos ya estuvieran en su posición correcta.

**Fix aplicado:** en `apply_compaction`, extraer el nuevo counter del `new_name`
con regex y hacer `UPDATE files SET filename=?, counter=? WHERE hash=?`.

### Verificación de integridad de hashes

Script de verificación SHA-256 ejecutado sobre los dos perfiles compactados:

| Perfil    | Archivos | OK   | Mismatch | Faltantes |
|-----------|----------|------|----------|-----------|
| Hoovesart | 2026     | 2026 | 0        | 0         |
| RuiDX     | 6346     | 6346 | 0        | 0         |

**Conclusión:** el rename en disco y el UPDATE en DB son correctos — ningún archivo
fue corrompido ni desvinculado de su hash durante la compactación.

### Bug TUI — ArtistScreen: eliminar URL lanzaba `TypeError` con `RowKey`

**Síntoma:** clic en "- Eliminar" en la tabla de fuentes lanzaba
`TypeError: int() argument must be a string... not 'RowKey'`.

**Causa:** `_del_url_async` hacía `int(keys[row_idx])` donde `keys` es `list[RowKey]`.
`RowKey` es un objeto de Textual, no un string — `int()` no lo acepta directamente.

**Fix:** `int(keys[row_idx].value)` — acceder al valor string del RowKey.

---

### Bug organizer.py — pre-scan creaba carpeta duplicada con estructura vieja

**Síntoma:** pre-scan creaba `/images/cherry-dl/kemono/RuiDX/` con los archivos
y `/images/cherry-dl/RuiDX/` solo con catalog.db — dos carpetas para el mismo artista.

**Causa:** `organizer.py:85` construía `artist_dir = dest_root / site / _safe_dirname(...)`.
El segmento `/ site /` era la estructura anterior (`{dest_root}/{site}/{artista}/`).
El download ya usaba la nueva estructura (`{dest_root}/{artista}/`), creando rutas distintas.

**Fix:** `artist_dir = dest_root / _safe_dirname(artist_name or artist_id)` — eliminar `/ site /`.

---

### Bug TUI — Semáforo invisible: `dock: bottom` vs panel del OS

**Síntoma:** el semáforo `#status-bar` con `dock: bottom` y el `Footer` (también
`dock: bottom`) eran ambos ocultados por el panel del sistema operativo (Bazzite/KDE)
que recortaba las últimas filas del terminal.

**Causa:** el terminal ocupaba pantalla completa y el panel del OS tapaba 1-2 filas.
Ni el Footer ni el status-bar eran visibles.

**Fix:** semáforo movido inline como segundo hijo de la fila "ACTIVIDAD" (Horizontal
con `section-label` + semáforo + contadores). `Footer` eliminado de `ArtistScreen`.
Sin `dock`, el widget es parte del flujo normal y siempre visible.

---

### Bug TUI — Semáforo siempre gris (especificidad CSS)

**Síntoma:** el semáforo mostraba el texto correcto al cambiar de estado pero el
color permanecía siempre gris.

**Causa:** `#semaphore { color: #8888aa }` (selector de ID, especificidad alta)
sobreescribía `.status-running { color: #00cc66 }` (selector de clase, especificidad
menor). El color del ID ganaba siempre, independientemente del estado.

**Fix:** eliminar `color` del rule `#semaphore`. La clase `.status-*` aplicada vía
`sem.set_classes(cls)` ya define el color correctamente.

---

### Bug — `database is locked` con workers concurrentes

**Síntoma:** durante descarga con 3+ workers, aparecía
`sqlite3.OperationalError: database is locked` en el log de actividad.

**Causa:** cada worker llamaba `next_counter`, `url_exists`, `hash_exists`, `add_file`
concurrentemente, cada uno abriendo una conexión SQLite nueva. Sin timeout, SQLite
falla inmediatamente (`SQLITE_BUSY`) si otro writer tiene el lock.

**Fix:** `catalog.py` — helper `_db(path)` que abre con `timeout=30`. Aplicado a
todas las conexiones del módulo. Adicionalmente, `init_catalog` activa
`PRAGMA journal_mode=WAL`: con WAL los lectores no bloquean escritores y viceversa,
reduciendo drásticamente la contención.

---

### Bug — Producer de paginación silencioso ante errores de red

**Síntoma:** la descarga terminaba con pocos archivos sin ningún mensaje de error.
Ejecuciones sucesivas descargaban más cada vez (el catálogo saltaba los ya vistos y
avanzaba en páginas nunca alcanzadas).

**Causa:** `asyncio.gather(*_all_tasks, return_exceptions=True)` convierte cualquier
excepción del producer en un valor de retorno. Si `iter_files` falla en página 6 por
un error de red, el producer entra al `finally`, manda `None` a todos los workers,
y el proceso termina limpiamente sin mostrar el error.

**Fix:** después del gather, se verifica `_results[0]` (resultado del producer). Si
es una excepción, se loguea el tipo y mensaje, se muestra aviso de descarga incompleta,
y se pone el semáforo en rojo (`_set_semaphore("error")`).

---

### Semáforo — estados y colores finales

| Estado | Color | Cuándo |
|---|---|---|
| ● Listo | gris `#8888aa` | inactivo |
| ● Corriendo… | verde `#00cc66` bold | descargando |
| ● Completado | azul `#44aaff` bold | todo descargado sin errores ni pendientes |
| ● Cancelado | amarillo `#ffaa00` bold | errores parciales o archivos diferidos |
| ● Error | rojo `#ff4444` bold | fallo de paginación/red en el producer |

---

### Bug TUI — SettingsScreen: `download_path` sin setter

**Síntoma:** al guardar en la pantalla de configuración global de la TUI, aparecía
`Error al guardar: property 'download_path' of 'UserConfig' object has no setter`.

**Causa:** `tui/app.py:action_save` asignaba `cfg.download_path = Path(...)`.
`download_path` es un `@property` de solo lectura calculado a partir de `download_dir`.

**Fix:** `cfg.download_dir = <valor_str>` — asigna directamente el campo real del
modelo Pydantic.

---

## 2026-03-21 — Fase 8: Compactación de numeración — COMPLETA

### Módulos implementados

**`catalog.py`** — tres funciones nuevas:
- `get_numbered_files(folder)` → extrae counter del **nombre físico** del archivo
  (no del campo `counter` en DB, que puede estar desactualizado tras renombres previos).
  Filtra solo archivos que existen en disco. Ordena por counter extraído con regex.
- `plan_compaction(files)` → retorna `(old_name, new_name, hash, new_counter)`.
  Compara nombres (no contadores DB) para detectar qué realmente cambia.
- `apply_compaction(folder, plan, new_total)` → rename en dos fases + UPDATE atómico.
  Usa hash como clave primaria para todos los UPDATEs (evita UPDATE encadenado).
  Aplica prefijo `_purged_` a registros fantasma antes de reasignar su filename.

**`cli.py`** — comando `cherry-dl compact <perfil> [--dry-run] [--yes/-y]`

**`tui/app.py`** — `CompactConfirmModal` + botón `⊟ Compactar` + `_start_compact`/`_do_compact`

### Bugs encontrados y corregidos en test

**Bug 1 — `apply_compaction` no actualizaba columna `counter`:**
Primera implementación hacía `UPDATE files SET filename=?` pero no tocaba `counter`.
El campo `counter` quedaba con el valor original, causando que la siguiente llamada
a `plan_compaction` (con comparación de contadores) volviera a generar el mismo plan.
**Fix:** actualizar también `counter` en cada UPDATE.

**Bug 2 — `plan_compaction` comparaba contadores en vez de nombres:**
La condición `if new_counter != old_counter` detectaba archivos "que deben moverse"
basándose en el contador DB (potencialmente stale). Tras el Bug 1, el archivo
`X_05354.jpg` tenía counter=5356 en DB pero su nombre ya era correcto.
La comparación de counters lo marcaba incorrectamente como "pendiente de mover".
**Fix:** condición `if new_name != filename` — compara el nombre resultante con el actual.

**Bug 3 — `get_numbered_files` ordenaba por counter DB (stale):**
Con counter DB desactualizado, los archivos aparecían en el orden original de descarga
en lugar del orden físico actual. `plan_compaction` recibía una lista mal ordenada
y generaba un plan incorrecto (asignaba posiciones erróneas).
**Fix:** extraer el counter del nombre del archivo con regex `_(\d{5})\.[^.]*$`
y ordenar por ese valor, ignorando el campo `counter` de la DB.

**Bug 4 — UPDATE encadenado por filename:**
`UPDATE files SET filename=B WHERE filename=A` seguido de
`UPDATE files SET filename=C WHERE filename=B` — el segundo UPDATE afectaba
al registro ya modificado por el primero, causando que un archivo terminara
con filename=C en lugar de B.
**Fix:** usar `WHERE hash=?` (clave primaria) en lugar de `WHERE filename=?`.

**Bug 5 — Reactivación de registros "purgados":**
Cuando la compactación rellena el hueco en la posición K, crea un archivo
`artista_0000K.jpg` en disco. Si existe un registro en DB con `filename=artista_0000K.jpg`
de un archivo previamente purgado por el usuario, ese registro "revive" y aparece
duplicado en la siguiente llamada a `get_numbered_files` (el archivo ahora existe en disco).
La siguiente pasada de `plan_compaction` ve duplicados y genera un plan incoherente.
**Fix:** antes de cada UPDATE por hash, `UPDATE files SET filename='_purged_'||hash
WHERE filename=new_name AND hash!=file_hash`. El prefijo `_purged_` no coincide con
el patrón `_\d{5}\.ext`, por lo que `get_numbered_files` lo ignora. El hash y
url_source se preservan para evitar re-descargas del archivo purgado.

### Test en producción (2026-03-21)

- **Hoovesart**: 2026 archivos, 1703 renombrados — verificación hash 100% OK
- **RuiDX**: 6331 archivos, primera pasada (código antiguo) dejó 976 pendientes;
  segunda pasada (código corregido) completó la compactación — verificación hash 100% OK
- Ambos perfiles idempotentes: segunda llamada a `compact` reporta "Numeración ya es continua"

---

## 2026-03-21 — Planificación Fase 8: Compactación de numeración

### Problema

Los artistas postean contenido mixto: arte original, memes, imágenes de texto, avisos, etc.
El usuario limpia su colección borrando archivos no deseados, dejando huecos en la numeración:
`artista_0001.jpg, artista_0002.jpg, artista_0005.jpg, artista_0009.jpg`

Un comando/botón de compactación rellena esos huecos renombrando los archivos existentes.

### Re-descarga de archivos purgados: ya está resuelto

Los archivos borrados del disco mantienen su registro en `catalog.db`. El engine usa
`url_exists()` y `hash_exists()` antes de descargar — si el registro está en la DB, se
salta el archivo aunque no exista en disco. No se requiere ningún cambio adicional.

### Algoritmo de compactación

```
1. Scan del folder → lista de archivos que coinciden con el patrón de nombre
2. Cruzar con catalog.db → obtener (counter, filename, hash) solo de existentes en disco
3. Ordenar por counter actual
4. Construir plan: asignar nuevos contadores secuenciales desde 1
   - Si counter ya es correcto (sin hueco previo) → no mover
5. Ejecutar en dos fases (anti-colisión):
   Fase 1: todos los archivos a renombrar → nombre.tmp
   Fase 2: nombre.tmp → nombre final
6. UPDATE catalog.db SET filename = nuevo_nombre WHERE filename = viejo_nombre
7. UPDATE meta SET value = nuevo_max WHERE key = 'counter'
```

**Por qué dos fases:** renombrar directo puede causar colisiones cuando el nuevo nombre
de un archivo coincide con el nombre actual de otro (ej. `0005→0003` si `0003` ya existe).
Con la fase intermedia `.tmp`, ningún nombre final puede colisionar.

### Superficie de usuario

**TUI (`ArtistScreen`):**
- Botón "⊘ Compactar" junto al botón dedup existente
- Al hacer clic: modal con doble advertencia:
  - "Se renombrarán N archivos en disco"
  - "Esta acción no se puede deshacer"
  - Botón "Cancelar" con focus por defecto + botón "Confirmar"
- Progress feedback durante rename
- Mensaje final en log: "Compactación completa — N archivos renombrados"

**CLI:**
- `cherry-dl compact <profile_name_or_id>`
- Muestra el plan (tabla: viejo → nuevo, N archivos)
- Confirmación interactiva: `¿Continuar? [s/N]`
- Flag `--yes` para omitir confirmación (uso en scripts)

---

## Fase 9 — Descarga incremental ("Actualizar") — 2026-03-24

### Problema
No existía forma de descargar solo el contenido nuevo desde la última sync. El botón
"Descargar" siempre iteraba todos los posts/obras del artista desde el inicio, dejando
que el sistema de dedup (url_source / hash) descartara lo ya descargado. Ineficiente
en artistas con miles de posts.

### Solución implementada

**`templates/base.py`**
- `iter_files()` acepta nuevo parámetro `since: datetime | None = None`
- Helper `parse_date_utc(s)` exportado desde `base.py`:
  - Normaliza cualquier formato ISO 8601 a `datetime` UTC naive
  - Acepta: naive, `+00:00`, `.000+00:00` (Patreon), `Z`, `YYYY-MM-DD HH:MM:SS` (SQLite)
  - Retorna `None` en cadena vacía o inparseable

**`templates/kemono.py`**
- `iter_files(since)` — dentro del loop de posts, compara `post["published"]` contra `since`
- Si `pub < since`: hace `return` → para toda la paginación
- Justificación: Kemono devuelve posts newest-first; todos los siguientes también serían más viejos

**`templates/patreon.py`**
- `iter_files(since)` pasa `since` a `_iter_posts(since)`
- `_iter_posts(since)` compara `post["attributes"]["published_at"]` contra `since`
- Si `pub < since`: hace `return` → para paginación cursor-based
- Justificación: Patreon devuelve posts newest-first (sort=-published_at)

**`templates/pixiv.py`**
- `iter_files(since)` → `_iter_all(since)` → `_process_batch(since)`
- En `_process_batch`: compara `work["createDate"]` contra `since`
- Si `pub < since`: `continue` (no `return` — IDs de Pixiv no tienen orden garantizado)
- Optimización: se saltea las llamadas extra a `/pages` y `/ugoira_meta` para obras antiguas,
  evitando peticiones HTTP innecesarias

**`tui/app.py`**
- Botón `↑ Actualizar` (id=`btn-update`) — entre "Verificar" y "Descargar"
- Se deshabilita durante operaciones activas (junto con los demás botones)
- `action_start_update()` → `_run_download(update_only=True)`
- `_do_download(update_only)` calcula `url_since` por URL individual:
  - Lee `pu["last_synced"]` de cada fuente
  - `parse_date_utc(pu["last_synced"])` → `since` para esa fuente
  - Si `last_synced` es `None` (nunca synced): hace descarga completa de esa fuente
  - Log: `↑ Actualizar desde YYYY-MM-DD HH:MM`

### Smoke test
- Imports de los 3 templates + TUI: OK
- `parse_date_utc`: 7 casos (naive, UTC+tz, ms, SQLite, Z, vacío, basura): OK
- Firmas de `iter_files`, `_process_batch`, `_iter_posts` verificadas via `inspect`: OK
- `_run_download(update_only)`, `_do_download(update_only)`, `action_start_update`: OK

### Commit
`a8837ee` — feat: botón Actualizar — descarga incremental desde última sync
- Flag `--dry-run` para ver el plan sin ejecutar
