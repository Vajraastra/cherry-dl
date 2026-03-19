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

## Errores encontrados
<!-- Formato: fecha | error | causa | solución -->

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
