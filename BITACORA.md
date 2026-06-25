# BITACORA — cherry-dl

> **Propósito de este archivo:** registro de producción y mapa técnico del proyecto.
> Una sesión nueva debe poder leer este archivo y entender completamente la arquitectura,
> el estado actual y los próximos pasos sin necesidad de contexto previo.

---

## 2026-06-24 (sesión 6) — Fase 3 Paso 8: deprecación formal de la TUI

**Fase 3 (migración a PySide6) COMPLETA.** La GUI PySide6 es la interfaz oficial; la TUI
Textual queda deprecada (se conserva como spec de features, no se borra).

### Alcance decidido con David
- **TUI → sólo marcar** (no borrar): se mantiene funcional como referencia de features.
- **GUI legada → retirar**: ya estaba hecho *de facto*. La migración a PySide6 fue **in-place**
  sobre `cherry_dl/gui/` (no había dos GUIs paralelas); `bridge.py` y `native_dialog.py` se
  borraron en el saneamiento (commit 12ba7c6) y `cherry-dl gui` ya lanza el `QStackedWidget`
  nuevo (`gui/app.py`, 6 vistas). Sólo quedaba un docstring obsoleto que mentía ("Dear PyGui").

### Cambios
- **`cli.py` `tui()`**: aviso de deprecación con `rich` + `time.sleep(3)` cancelable (Ctrl-C →
  `Exit(0)`) antes de arrancar. Docstring marcado `[LEGADO]`. **Texto ASCII-safe**: se evitan
  `⚠` (U+26A0) y `…` porque al escribir por una consola Windows en cp1252 lanzan
  `UnicodeEncodeError` y **crashearían el arranque** — riesgo real en el camino crítico.
- **`cli.py` `gui()`**: docstring → "interfaz gráfica oficial".
- **`gui/__init__.py`**: docstring "Dear PyGui" → "PySide6 + qasync".
- **`README.md`**: estaba invertido (TUI=default, GUI=legacy). Corregido a GUI=default/oficial,
  TUI=legacy/deprecated; CLI reference lista `gui` (oficial) y `tui` (legado).
- **`run.sh`**: comentario actualizado (deprecación completa, ya no "en curso").

### Verificación
- Aviso imprime las 4 líneas y luego arranca (probado con `time.sleep`/`tuiapp.run` parcheados).
- `cherry-dl --help` muestra los docstrings nuevos; `cherry_dl.gui.app:run_app` y `cherry_dl.cli`
  importan OK.

---

## 2026-06-24 (sesión 5) — Fase 3 Pasos 6-7 + bug de hash-join

### Cambios de UI (GUI PySide6)
- **Filtro por tipos** portado a `artist_detail_view` y al wizard (checkboxes `EXT_GROUPS` +
  custom). El wizard antes IGNORABA su `QLineEdit` de filtro; ahora persiste vía
  `update_profile_ext_filter`. (commit 875adc2)
- **Barras de worker → porcentaje numérico** (`QLabel` `%`). Las descargas son tan rápidas que
  las `QProgressBar` no daban señal útil. (commit 875adc2)
- **Paso 6 — `batch_view.py`**: descarga por lotes multi-perfil sobre `run_profile_download`;
  loop que reintenta un perfil sólo si su `pending_queue` baja, abandona si no progresa. (0f700dc)
- **Paso 7 — `duplicates_view.py`**: port de la `DuplicateScreen` (fase 1 URL/nombre, fase 2
  hash-join, fusión con gestión de huérfanos + compactación opcional). Helpers puros movidos a
  `cherry_dl/dedup.py` (la TUI los reimporta como alias).

### BUG raíz — `compare_by_hash_join` falso 100% (catalog.py)
**Síntoma:** un perfil creado pero **sin descargar** (su `catalog.db` sólo tiene `profile_meta`,
creado por `write_profile_meta`, sin la tabla `files`) daba **100% de coincidencia de hashes**
contra cualquier otro perfil al compararse.
**Causa raíz:** el query usaba `SELECT COUNT(*) FROM files` SIN cualificar. Con la otra base
ATTACHeada como `cat_b`, si `files` no existe en la base principal, SQLite resuelve el nombre
en la base adjunta → contaba los archivos del OTRO catálogo como propios → coverage 1.0.
**Fix:** cualificar `main.files` / `cat_b.files` en todos los `COUNT`/`JOIN` + guard que verifica
que la tabla `files` existe en AMBAS bases (si falta en alguna → coverage 0).
**Impacto:** afectaba también a la `DuplicateScreen` de la TUI (que nunca se probó e2e — por eso
figuraba como "PENDIENTE PRUEBA"). Detectado por el test e2e nuevo de `duplicates_view`.

### Pruebas
Tests headless reutilizables en scratchpad: `test_batch_view.py` (orquestación del batch con
servicio stubbeado) y `test_duplicates_view.py` (índice + catálogos reales en temp: fase1, fase2,
fusión + migración, y el caso del falso 100%). 18/18 tests; TUI+GUI importan.

---

## 2026-06-24 (sesión 4) — Fase 3 Pasos 2-3: GUI sobre el servicio + prueba e2e qasync

### Paso 2 — EXT_GROUPS compartido
`EXT_GROUPS` movido de `tui/app.py` a `cherry_dl/downloads.py` (módulo canónico compartido por
servicio y GUI). `tui/app.py` y `tests/test_ext_groups.py` reimportan. 18/18 tests. (commit 8ae7e80)

### Paso 3 — artist_detail_view delega en download_service
`ArtistDetailView._do_download` pasó de ~520 líneas (reimplementación inline del flujo) a 50:
ahora llama a `run_profile_download(...)` y traduce los eventos tipados a widgets en el nuevo
`_on_service_event`. Detalles:
- **Throttle 4 Hz** añadido en `_worker_progress`: el servicio emite `WorkerProgress` por cada
  chunk (~160/s/worker a 10 MB/s); sin throttle satura el event loop de qasync con ops Qt. (El
  throttle vivía antes dentro del callback inline; al delegar, debe vivir en el handler de UI.)
- `_parse_ext_filter` importado de `downloads`; borrados imports muertos `add_file`/`next_counter`.
- `exclude_mode=False` (la UI de filtro de la GUI es solo-incluir). `resolve_auth=None` por ahora
  → si falta sesión Patreon, el servicio loguea "auth cancelada" y salta la fuente (pendiente Paso 5).

### Feedback de escaneo en la GUI (post-prueba 2: login OK, scan invisible)
2ª prueba de David: el login guiado funcionó (Brave abrió, "Sesión capturada", guardada). Pero al
entrar en la Fase 1 ("Filtro cambiado — escaneo completo…") la GUI quedó aparentemente congelada: la
barra de estado seguía pegada en "Esperando login…" y no había señal de progreso del scan (RuiDX
escanea miles de posts antes de descargar). Causa: la Fase 1 casi no emitía eventos.
- Nuevo evento `ScanProgress(seen, queued)` en `download_service`, emitido cada 25 posts dentro del
  loop de `iter_files`. El dispatcher de `artist_detail_view` lo traduce a la barra de estado
  inferior ("Escaneando… N posts vistos · M nuevos en cola").
- El dispatcher ahora también actualiza el estado en `SourceStarted` ("Escaneando {artista}…") y
  `BatchInfo` ("Descargando N archivo(s)…"); y `_do_download` resetea el estado tras el gate de login
  ("Preparando descarga…") para no quedar pegado en "Esperando login…".
- Probado: el servicio emite ScanProgress en seen=25 y 50 (60 archivos), ScanComplete=60.

### Login Patreon en la GUI — chequeo proactivo + login/logout (post-prueba de David)
Probando la GUI en vivo, David vio que al descargar de Patreon NO saltaba el login y la descarga
bajaba (probablemente) solo contenido público en silencio. Diagnóstico: `get_artist_info` resuelve
la campaña con datos públicos sin lanzar `NeedsManualAuth`, así que la auth perezosa nunca forzaba
login. Además, en Windows las cookies del navegador no se auto-leen (App-Bound) y `run.bat` abría la
TUI por error (corregido: ahora `run.sh` lanza `gui` por defecto; TUI vía `run.bat tui`).

Decisión de diseño (David): el login NO se esconde en Ajustes (usuarios no se enterarían) — popup
**en contexto** solo al descargar de Patreon sin sesión; cerrar sesión sí en Ajustes.
- `artist_detail_view._ensure_patreon_login_if_needed()`: antes de `run_profile_download`, si hay
  fuente Patreon activa y `load_patreon_cookies()` es None → `QMessageBox` "Iniciar sesión / Cancelar".
  Cancelar aborta la descarga (no baja lo público en silencio).
- `_run_patreon_login()`: login guiado compartido (`guided_login_patreon` → abre el navegador) con
  status al log. Reusado por `_resolve_auth` (fallback si la sesión expira a mitad de descarga).
- `settings_view`: sección "Cuenta de Patreon" — estado (Conectado/No conectado), "Iniciar sesión"
  (login guiado) y "Cerrar sesión" (`clear_patreon_session`).
- Lógica del gate testeada headless (4 casos). E2e de descarga sigue verde (mock de sesión para no
  colgar el popup). ⚠ El login guiado real (nodriver abre Brave) requiere validación interactiva.

### Paso 5 — fix estructura de carpetas + resolve_auth en la GUI
**(a) Estructura de carpetas plana.** `cherry_dl.profiles.create_profile` (y el preview del wizard)
construían el `folder_path` como `{download_dir}/{site}/{nombre}` — estructura VIEJA. Pero
`organizer.organize` (L85), `index.reindex_from_folders` (itera `download_dir.iterdir()` directo)
y la biblioteca real (`G:\images\cherry-dl\BadSpider`, sin subcarpeta de sitio) son PLANAS
`{download_dir}/{nombre}`. Consecuencia del bug: un perfil creado por el wizard tendría
`folder_path=.../site/nombre`, pero el prescan organiza a `.../nombre` y reindex no lo casaría →
archivos dispersos y perfil divergente del resto. Corregido a plano en `profiles.py` (L107 +
docstring), `new_profile_wizard._update_folder_preview` y el comentario de schema en `index.py` L43.
Verificado: `create_profile` ahora devuelve `{base}/{nombre}`.
(`rename`→`replace` ya estaba resuelto: `grep .rename( cherry_dl/gui` → 0 — el download inline con
`old_path.rename` se borró al delegar en el servicio en Paso 3; el servicio usa `.replace`.)

**(b) resolve_auth cableado.** `ArtistDetailView._do_download` pasa `resolve_auth=self._resolve_auth`
a `run_profile_download`. El nuevo `_resolve_auth(site)`: para `patreon` muestra un `QMessageBox`
y, si el usuario acepta, hace `ensure_patreon_session(allow_guided=True, on_status=→log)` y retorna
True si obtiene `session_id`; otros sitios → False con aviso. Antes el servicio recibía
`resolve_auth=None` y saltaba la fuente logueando "auth cancelada".
⚠ No se pudo probar headless (el login guiado abre un navegador real por CDP/nodriver y requiere
acción del usuario). Validar interactivamente al abrir la GUI. La regresión del camino sin-auth sí
se cubrió: la prueba e2e (template stub) sigue verde con el nuevo parámetro `resolve_auth`.

### Paso 4 — profiles_view: columna Estado sobre la pending_queue
La columna Estado de la GUI sólo mostraba `✓ fecha` / "Sin verificar". Ahora replica la semántica
de la TUI, basada en `pending_count` (filtrado por el ext_filter del perfil vía `_decode_profile_filter`):
`⏳ N` (amarillo) si hay pendientes / `✓ fecha` (verde) si verificado y sin cola / `○ Sin sync` (gris)
si nunca verificado / `?` si la carpeta no existe. Color por `QTableWidgetItem.setForeground(QColor)`.
- **`_encode_profile_filter` / `_decode_profile_filter` movidos** de `tui/app.py` a `downloads.py`
  (mismo patrón que EXT_GROUPS en Paso 2): la GUI no debe depender de la TUI (Textual + a deprecar).
  La TUI los reimporta. Son funciones puras (JSON + backward-compat con formato coma legado).
- Prueba headless (offscreen + qasync, catálogos reales con pendientes + filas sintéticas): los 5
  estados renderizan bien, incluido el conteo filtrado (perfil con filtro `zip` → ⏳1 de 3 pendientes).

### Prueba e2e (headless, bajo qasync real) — TODO VERDE
Riesgo del Paso 3: que el servicio async corra bien bajo el loop fusionado Qt+asyncio de qasync y
que `emit` (callback síncrono que toca widgets) funcione. El scan real de Patreon es 2-fases
secuencial (escanea TODO antes de descargar → minutos/horas), así que para validar *la costura*
sin esperas usé un **template stub** sirviendo archivos pequeños desde un `http.server` local, con
índice y carpeta temporales (no se tocó la biblioteca real). Se ejercitó `ArtistDetailView._do_download`
sobre `QEventLoop` de qasync con plataforma Qt `offscreen`:
- **Completación**: 5/5 archivos en disco, 5/5 filas de catálogo, 5 callbacks de progreso, log poblado.
- **Cancelación**: cancelado tras 3 descargas (server con delay 0.4s/req para cancelar a mitad) →
  3 archivos en disco, `task.done()==True`, status "Cancelado por el usuario", sin deadlock ni
  excepciones huérfanas.
- Nota menor (no regresión): el status final muestra "Listo" en vez de "Completado" porque
  `_do_download` cierra con `_load_async()` que resetea el label; el resumen queda en el log.

---

## 2026-06-24 (sesión 2) — Migración a PySide6: auditoría + saneamiento

### Contexto
La GUI PySide6 en `cherry_dl/gui/` la hizo un agente anterior sin diseño competente → tratada
como referencia sospechosa, no base. La TUI (`tui/app.py`) es la spec funcional; el backend
(catalog/index/engine/templates/auth) es compartido y sólido.

### Fase 1 — Auditoría del legacy (veredicto por archivo)
- `theme.py` ✅ rescatar — QSS limpio, Fusion.
- `app.py` ✅ rescatar+ampliar — router QStackedWidget + qasync correctos; faltan vistas Batch/
  Duplicados y cleanup al cerrar.
- `profiles_view.py` ✅ rescatar — qasync bien usado; falta columna Estado y botones Batch/Comparar.
- `settings_view.py` ✅ rescatar — sólida (pydantic model_copy, validación).
- `new_profile_wizard.py` 🟡 rescatar+fix — BUG: preview de carpeta usa estructura vieja
  `download/{site}/{name}` (L279), debe ser artist-first; falta dedup-check Levenshtein.
- `artist_detail_view.py` 🟡 rescatar+modernizar — sorprendentemente bueno (productor/workers,
  dedup atómico in_progress_hashes, cola diferida, throttle UI 4Hz, cancelación correcta); NO usa
  pending_queue, ni incremental last_synced, ni EXT_GROUPS; `rename` no Windows-safe (L1053).
- `bridge.py` 🔴 reescribir — arquitectura Dear PyGui (AsyncBridge hilo+queue) mezclada con qasync;
  TODO el cuerpo pesado es código muerto (0 usos: AsyncBridge, download_for_gui, prescan_and_download,
  update_async, repair_async, load_collections_async, ProgressUpdate); funciones vivas con estructura
  de carpetas vieja (bug) y URL kemono hardcoded. Solo 4 helpers se rescatan.
- `native_dialog.py` 🔴 descartar — Linux-only (kdialog/zenity)+tkinter; en app Qt → QFileDialog.

Heredado gratis: el lazy auth de Patreon ya funciona en la GUI (get_artist_info →
ensure_patreon_session abre el navegador solo).

### Saneamiento ejecutado (paso 1, mecánico)
- Nuevo `cherry_dl/downloads.py`: helpers vivos extraídos de `gui/bridge.py` (build_filename,
  _safe_prefix, _parse_ext_filter, _passes_ext_filter, _build_local_hash_map, _MEDIA_EXTENSIONS).
  Rompe la dependencia invertida TUI→gui. De paso: `get_event_loop`→`get_running_loop`.
- Redirigidos 8 imports: cli.py, organizer.py, tui/app.py (×5), gui/views/artist_detail_view.py,
  tests/test_ext_groups.py → `cherry_dl.downloads`.
- BORRADOS `gui/bridge.py` (código muerto + helpers movidos) y `gui/native_dialog.py`.
- `QFileDialog.getExistingDirectory()` reemplaza `pick_directory_sync` en wizard y settings.
- Verificación: smoke import de cli/tui/gui/downloads OK; 18/18 tests de test_ext_groups.

### Próximo: Fase 2 (arquitectura)
Servicio de descarga compartido (productor/workers + pending_queue + incremental) reutilizable
por GUI y TUI — hoy duplicado entre tui/app.py y artist_detail_view.py. Luego port por vistas.

---

## 2026-06-24 (sesión 3) — Decisión: GUI oficial + servicio de descarga compartido

### Decisión de rumbo
La GUI PySide6 será la **UI oficial**; la TUI Textual se **deprecará** cuando la GUI esté lista.
La TUI pasa a ser solo fuente de lógica probada. El backend es compartido y sólido.

### Fase 3 paso 1 — `cherry_dl/download_service.py` (servicio compartido)
Servicio async **agnóstico de UI**: la UI pasa un callback `emit(evento)` y recibe eventos tipados
(Log, WorkersResolved, SourceStarted, Cooldown, BatchInfo, WorkerStart/Progress/Done/Idle, Counters,
SourceDone, ScanComplete). Todo corre en un solo event loop (qasync) → emit síncrono, sin AsyncBridge.

`run_profile_download(profile, *, workers, ext_filter, exclude_mode, force_full, update_only,
scan_only, emit, config, resolve_auth)` porta el flujo de 2 fases de la TUI:
scan API (iter_files since=last_synced) → pending_queue → cooldown → productor/workers (dedup
atómico in_progress_hashes, remove_pending por éxito) → cola diferida → update last_synced.
Cancelación: cancelar la task que await-ea la función (propaga CancelledError limpiamente).

### Validación headless con BadSpider (index.db redirigido a temporal — biblioteca real intacta)
- Fase 1 (scan_only): 1567 imágenes encoladas en pending_queue.
- Fase 2: retoma cola sin re-escanear, cap workers=2 (Patreon), descarga real + catalogar +
  remove_pending. 30 imgs en disco = 30 catalogadas (consistencia perfecta).
- Cancelación graceful: cierre limpio en 1.0s.

### BUG real encontrado y corregido — deadlock del producer al cancelar
Diagnóstico vía faulthandler + Task.print_stack: al cancelar, los workers mueren y dejan de
consumir la cola; el `producer` en su `finally` intentaba `await file_queue.put(None)` (centinelas)
en una **cola llena sin consumidores** → put colgado para siempre → el gather padre lo esperaba
eternamente. El proceso nunca cerraba tras cancel.
Fix: encolar los centinelas None **solo en terminación normal** (al final del try), no en el
`finally`; en cancelación el except deja propagar el cancel sin tocar la cola.
NOTA: el mismo patrón está latente en `tui/app.py:_do_download` (líneas ~2649) y en la GUI legada
`artist_detail_view._do_download`; no se corrigen porque ambas se reemplazan/deprecan.

---

## 2026-06-24 — Login de Patreon: cableado del CLI + auth perezosa

### Hecho
1. **`cli.py patreon-login` cableado al login guiado.** Por defecto abre el navegador real
   (`find_browser()` + `guided_login_patreon()`) y captura `session_id` por CDP; `--session-id`
   queda como fallback manual. Si no hay navegador instalado, mensaje claro hacia el modo manual.
   - Quitados los caracteres `→` del docstring: `typer`/`rich` los renderiza en `--help` y
     reventaban con `UnicodeEncodeError` en una consola Windows cp1252 (no-UTF8).
   - **Probado e2e**: abrió Brave, "Sesión capturada", guardó `session.json`, exit 0.

2. **Auth perezosa en `auth/patreon.py:ensure_patreon_session()`.** Petición del usuario: el login
   no debe requerir un comando/flag previo, sino dispararse solo al usar un perfil de Patreon.
   - Nueva firma: `ensure_patreon_session(*, allow_guided=True, on_status=None)`.
   - Paso 3 nuevo (antes de `raise NeedsManualAuth`): si `allow_guided` y `find_browser()` halla
     navegador → `await guided_login_patreon()` y retorna las cookies capturadas.
   - Como el template (`templates/patreon.py:120`) llama `ensure_patreon_session()` en cada
     descarga/sync, el navegador se abre automáticamente cuando falta sesión — en CLI **y** TUI.
   - **Backend puro, TUI intacta.** El modal `NeedsManualAuth` de la TUI queda como fallback para
     el caso sin-navegador o login cancelado.

3. **Eliminado el TTL de 30 días.** `load_patreon_cookies()` ya no caduca por reloj; la sesión
   vale hasta que Patreon devuelva 401/403 (manejado en `templates/patreon.py:215` y `289`, que
   ya llamaban `clear_patreon_session()`). Combinado con la auth perezosa, el re-login es
   totalmente automático: sesión limpia → próximo uso abre el navegador (perfil persistente, ya
   logueado) → captura en segundos.
   - Quitada la constante `_SESSION_TTL`. `_saved_at` se conserva solo como metadato informativo.
   - Actualizados docstrings/mensajes que mencionaban "30 días" (cli.py, auth/patreon.py,
     templates/patreon.py — este último además decía "Playwright", ya obsoleto).
   - **Persistencia en dos capas**: (1) `session.json` sin caducidad → el navegador no se reabre;
     (2) perfil `~/.cherry-dl/browser-profile` persistente → el flujo tedioso de Google (email+código)
     solo ocurre la primera vez. Re-login tras 401 reusa el perfil, sin repetir Google.

### Notas / pendientes
- nodriver deja `ResourceWarning: unclosed transport` (I/O on closed pipe) al cerrar en Windows.
  Es ruido del teardown del subprocess del navegador; no afecta el resultado (exit 0). Suprimir si molesta.
- Falta probar el lazy path real: borrar sesión + `cherry-dl download <url_patreon>` → debe auto-abrir login.
- Pixiv conserva su TTL de 30 días (no se tocó; mismo patrón si se quiere replicar).

---

## 2026-06-23 (sesión 2) — Recuperación de biblioteca + auth Patreon (login guiado)

### Estado: PAUSADO en punto seguro. ← LEER PARA RETOMAR

#### Commiteado + pusheado a origin/main (commits 6d1808e, 5380a6c, 7710a50)
- **Recuperación del índice** tras la migración: el `index.db` de Windows estaba vacío y
  el de Bazzite no es accesible (partición ext4). Colecciones en **`G:\images\cherry-dl\`**
  (55 carpetas). El `catalog.db` NO guarda la URL de scrape (solo archivos) → se reconstruyó
  cruzando el NOMBRE de carpeta con el directorio de creadores de kemono (`/api/v1/creators`).
  - **`cherry-dl recover`** (`cherry_dl/recovery.py`): 46/55 perfiles reconstruidos y
    **validados** contra la API de kemono. 9 sin match → `G:\images\cherry-dl\.recovery\recovery_review.txt`.
  - Snapshot offline del directorio kemono en `.recovery/` (cierre ~2026-07-04).
  - Fix Windows: `save_config` rompía con rutas `G:\…` (backslash en TOML) → string literal.
  - `patreon-login` (versión manual, fallback).

#### Hallazgos clave
- **kemono cierra ~2026-07-04** → Patreon = fuente primaria; kemono interino.
- **id de kemono ≠ campaign_id de Patreon** (BadSpider: 7998148 vs 1261877; kemono da el
  *user id*). Para pre-cargar URLs Patreon usar vanity = nombre kemono (`patreon.com/c/{name}`),
  que el template resuelve (verificado con BadSpider).
- **Auth Windows bloqueada**: App-Bound Encryption de Brave/Chrome (~2024) → `browser_cookie3`
  y `rookiepy` fallan (RequiresAdminError). Logins con Google bloquean navegadores embebidos.
  **Solución (esquiva ambos): login guiado en el navegador REAL + captura por CDP (nodriver).**
  PROBADO: capturó session_id, resolvió BadSpider, y test e2e bajó 15 imágenes de BadSpider
  desde Patreon con filtro solo-imágenes (proyecto de prueba ya borrado; `BadSpider` original intacto).

#### Integración del login guiado — EN CURSO (SIN commitear aún)
- HECHO: `pyproject.toml` (+`nodriver>=0.50.0`); `cherry_dl/auth/browser_login.py`
  (`find_browser()` cross-OS + `capture_cookies()` CDP); `cherry_dl/auth/patreon.py`
  (`guided_login_patreon()`). Imports OK; find_browser detecta Brave.
- Perfil de navegador persistente en `~/.cherry-dl/browser-profile`.

#### ← EMPEZAR AQUÍ al retomar
1. **Cablear `cli.py` `patreon-login`**: por defecto login guiado (llamar
   `guided_login_patreon` + `find_browser`); `--session-id` como fallback manual.
   `cli.py` está INTACTO en su versión manual (la edición no se aplicó).
2. **Probar** `cherry-dl patreon-login` guiado y luego sincronizar.
3. **NO tocar la TUI** (decisión del usuario): tras confirmar compat dual-OS → transición a **PySide6**.
4. Pendiente aparte: re-sync TOTAL del BadSpider real (mal organizado); 9 folders en review;
   prueba DuplicateScreen (04-09).

---

## 2026-06-23 — Compatibilidad Windows 11 + biblioteca portable entre OSes

Migración de Bazzite (Fedora Atomic) a Windows 11. Auditoría de compatibilidad y
capa de portabilidad para usar **una sola biblioteca compartida** (partición común
montada con prefijos distintos por OS: `E:\…` vs `/run/media/…`).

### Decisión de arquitectura
`index.db` (en `~/.cherry-dl/`, con rutas **absolutas**) pasa a ser un **caché
derivado y reconstruible**. La fuente de verdad es la biblioteca de carpetas:
cada `catalog.db` lleva una tabla `profile_meta` (JSON) que hace la carpeta
**auto-describible** (display_name, primary_site, urls con last_synced/file_count,
ext_filter). Así el índice se reconstruye en cualquier OS con rutas locales sin
perder las URLs de scrape ni el estado de sync. Se descartó reubicar `index.db` a
la partición compartida (≈102 referencias a `INDEX_DB` → refactor caro y frágil, y
escritura concurrente entre OSes).

### Fase 1 — Windows-now (mecánico)
- **`run.sh` portable**: detecta OS por `uname` (MINGW/MSYS/CYGWIN → `uv.exe`,
  `Scripts/`). Filosofía de **cero dependencia del sistema**: `UV_PYTHON_INSTALL_DIR=.tools/python`
  instala un Python standalone **dentro del proyecto**; uv + venv también locales.
  En Windows uv se baja como binario (`curl` + `unzip` del zip oficial), no con
  `install.ps1` (Windows PowerShell 5.1 falla al invocarse desde Git Bash: error
  de carga de módulos en `Get-ExecutionPolicy`).
- **`run.bat`**: wrapper que delega a `run.sh` vía el Git Bash de Git for Windows.
- **Higiene de repo**: `.tools/` y `.venv` fuera de git (`git rm --cached .tools`,
  ya estaban los ELF Linux trackeados); `.gitattributes` con `*.sh eol=lf` y
  `*.bat eol=crlf` (CRLF rompería los scripts bajo Git Bash).
- **`rename()` → `replace()`**: en Windows `Path.rename` lanza `FileExistsError`
  si el destino existe; `replace()` sobrescribe atómicamente en ambos OS. Aplicado
  en `engine.py` (tmp→dest), `catalog.py` (compactación `.tmp`→new) y los 5 sitios
  de descarga de `tui/app.py`. (La renombrada de carpeta huérfana `orphan_*` se
  deja como `rename` a propósito: no debe clobberar una existente.)
- **Portapapeles Windows**: `_read_clipboard()` usa `powershell Get-Clipboard -Raw`
  en `sys.platform == 'win32'`, antes de los backends Linux (wl-paste/xclip/xsel).

### Fase 2 — Capa de portabilidad
- **`catalog.py`**: tabla `profile_meta` (fila única id=1, blob JSON) idempotente
  en `init_catalog`; `write_profile_meta` / `read_profile_meta`.
- **`index.py`**: `sync_profile_meta(db_path, profile_id)` vuelca el perfil a su
  `catalog.db` (silencioso si la carpeta no existe). Se llama desde las mutaciones
  (`add_profile_url`, `update_profile_url_sync`, `update_profile_ext_filter`,
  `set_profile_url_enabled`) y desde el borrado de URL en la TUI → toda
  modificación mantiene la carpeta al día sin tocar los ~20 call-sites que leen
  `folder_path`.
- **`index.py`**: `export_all_meta` (volcar todo, correr en OS origen) y
  `reindex_from_folders` (reconstruir index.db desde carpetas, upsert por
  folder_path local, idempotente, **no destructivo** con carpetas no montadas).
- **`cli.py`**: comandos `export-meta` y `reindex [dir] [--dry-run]`.
- **`tui/app.py`**: botón "⟲ Reindexar" + **auto-reindex** al arrancar si el índice
  está vacío pero la carpeta tiene catálogos (escenario "recién cambié de OS").
- **Flujo de migración**: en Bazzite `cherry-dl export-meta` → bootear Windows →
  apuntar `download_dir` a la partición → `reindex` (o auto al abrir la TUI).

### Fase 3 — Endurecimiento de nombres
- `cherry_dl/util.py`: `safe_dirname()` único (caracteres ilegales, control,
  puntos/espacios finales, nombres reservados de Windows `CON/NUL/COM1…`). Los 4
  `_safe_dirname` duplicados delegan en él; `_auto_folder` de la TUI ahora sanitiza
  (antes no).

### Caveats inevitables de NTFS (no son bugs de código)
- **Case-insensitive**: ext4 (Linux) distingue mayúsculas/minúsculas, NTFS no. Dos
  archivos que solo difieren en caso colisionan al copiar a Windows. cherry-dl
  numera los archivos (`Artista_00001`) → riesgo bajo, pero existe en nombres de
  carpeta de artista.
- **Límite de ruta 260 chars**: recomendable habilitar *long paths* en Win11
  (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`).
- `index.db` se mantiene en el home (NO en la partición compartida) para evitar
  corrupción por permisos/montaje NTFS y escritura concurrente entre OSes.

### Verificación
- `run.sh --help` en Win11: bootstrap completo (uv vía curl, Python 3.12.13
  standalone en `.tools/python`, venv sync, CLI corre). ✓
- Test funcional roundtrip (`scratchpad`): write/read meta, reindex en índice vacío
  con rutas locales, `last_synced` preservado, idempotencia. ✓
- `safe_dirname` (8 casos) ✓ · suite `tests/` 18/18 ✓.
- **Pendiente**: prueba cross-OS real cuando la partición esté montada en ambos
  sistemas. PySide6 sigue fuera de alcance (fase futura; la compat vive en backend).

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

## Sistema de detección y fusión de duplicados — 2026-04-09

### Diseño acordado

**Pipeline de 3 niveles con costo ascendente:**

| Nivel | Criterio | Costo | Certeza | Threshold |
|-------|----------|-------|---------|-----------|
| 1 | URL match (mismo `site` + `artist_id`) | DB query O(n²) | Definitivo | cualquier coincidencia |
| 2 | Similitud de nombre (difflib) | CPU O(n²) | Probable/Posible | ≥0.80 / 0.60–0.79 |
| 3 | Hash join SQL (ATTACH + INNER JOIN) | I/O SQLite | Definitivo/Probable | ≥51% auto / 10–50% revisión |

**Reglas de fusión:**
- El perfil más antiguo (`created_at`, fallback a `id` menor) absorbe al más nuevo.
- Archivos únicos de B se mueven físicamente a la carpeta de A con nuevo contador.
- URLs de B se agregan a A solo si no son duplicadas (mismo `url` o mismo `site+artist_id`).
- Archivos ya existentes en A quedan como "huérfanos" en carpeta B: usuario elige borrar / renombrar carpeta como `orphan_*` / ignorar.
- Se ofrece compactar la numeración del perfil destino tras la migración.
- Pares marcados como "distintos" se guardan en `profile_exclusions` y se omiten en futuros scans.

**Comparación de hashes — implementación SQL:**
```sql
ATTACH DATABASE '/ruta/catalog_b.db' AS cat_b;
SELECT COUNT(*) FROM files a INNER JOIN cat_b.files b ON a.hash = b.hash;
```
No escanea archivos en disco — solo consulta hashes ya indexados en SQLite. Eficiente porque `hash` es `PRIMARY KEY` (B-tree index) en ambas tablas.

### Archivos modificados

**`cherry_dl/catalog.py`**
- `import asyncio, shutil, Callable` agregados
- `compare_by_hash_join(folder_a, folder_b) → dict` — ATTACH + INNER JOIN; retorna `{total_a, total_b, matches, coverage}`
- `migrate_unique_files(folder_src, folder_dst, on_progress) → dict` — mueve archivos únicos con nuevo counter, retorna `{moved, orphaned, purged, errors, orphaned_paths}`

**`cherry_dl/index.py`**
- `_CREATE_EXCLUSIONS` — nueva tabla `profile_exclusions(id_a, id_b PK, CHECK id_a < id_b)`
- Tabla creada en `init_index()` (idempotente)
- `add_exclusion(db_path, id_a, id_b)` — guarda par como "distintos"
- `get_exclusions(db_path) → set[tuple]` — retorna todos los pares excluidos
- `merge_profiles()` actualizado — deduplica URLs al fusionar, limpia exclusiones del perfil eliminado

**`cherry_dl/tui/app.py`**
- Imports actualizados: `compare_by_hash_join`, `migrate_unique_files`, `add_exclusion`, `get_exclusions`
- `_url_overlap(urls_a, urls_b) → str` — helper que detecta mismo `site+artist_id` en dos listas de URLs
- `action_compare_profiles()` simplificado — ahora solo hace `push_screen(DuplicateScreen())`
- `_do_compare`, `_do_merge`, `_execute_merge` eliminados (reemplazados por DuplicateScreen)
- `SelectProfileModal`, `CompareResultModal`, `MergeConfirmModal` eliminados
- **`DuplicateScreen`** — pantalla completa con:
  - `_scan_phase1()`: worker que compara todos los pares N×N por URL + nombre, llena tablas en tiempo real
  - `_scan_phase2()`: worker de hash join bajo demanda, actualiza tablas conforme llega cada resultado
  - Tabla "FUSIÓN AUTOMÁTICA" (URL match o ≥51% hashes)
  - Tabla "REVISIÓN MANUAL" (nombre similar / hash 10–50%) con checkboxes toggle
  - `_execute_merges()`: migra archivos, fusiona índice, gestiona huérfanos, ofrece compactar
- `HashScanWarningModal` — advertencia antes de fase 2
- `OrphanActionModal` — borrar / renombrar carpeta como orphan_ / ignorar
- `CompactAfterMergeModal` — ofrece compactar perfiles que recibieron archivos
- `_dup_keep_remove()` — determina cuál perfil es "más antiguo" (keep vs remove)
- `_handle_orphans()` — función sync ejecutada en thread (borra o renombra)
- `_compact_folders()` — compacta numeración tras fusión

**`cherry_dl/tui/theme.tcss`**
- Estilos para `DuplicateScreen`: `#dup-main`, `#dup-status`, `#dup-progress`, `#dup-auto-table`, `#dup-review-table`, `#dup-actions`

### Estado al cerrar la sesión

El código compila y los imports verifican correctamente:
```
catalog OK (compare_by_hash_join, migrate_unique_files)
index OK (add_exclusion, get_exclusions, merge_profiles)
tui OK (DuplicateScreen, todos los símbolos nuevos)
```
**No se ejecutó prueba end-to-end.** La sesión terminó antes de poder probar en producción.

### Pendiente para próxima sesión
1. **Prueba de DuplicateScreen** — abrir TUI, ir a Perfiles → ⊗ Comparar, verificar que fase 1 se ejecuta y puebla las tablas.
2. **Prueba de fusión** — crear dos perfiles de prueba, ejecutar fusión automática, verificar migración de archivos + catalog + index.
3. **Prueba de fase 2** — con dos perfiles con archivos en común, verificar que el hash join retorna coverage correcto.
4. **Auto-check en creación de perfil** — actualmente la detección de duplicados solo corre cuando el usuario abre DuplicateScreen manualmente. Falta añadir un check automático (fase 1 solamente) al finalizar `_create_profile` en `NewProfileModal`.

---

## Filtro por tipo + pending_queue — 2026-04-09

### Problema raíz

Al descargar imágenes y luego cambiar el filtro a ZIPs, el sistema ignoraba los ZIPs porque:

1. El fingerprint de filtro se guardaba **incluso cuando la API devolvía 0 archivos** (bloqueo transitorio de DDoS-Guard u otro error de red). En el siguiente intento, el sistema veía `changed=False` y saltaba el scan.
2. La condición de reanudación solo chequeaba `existing_pending > 0`, sin verificar que algún pendiente realmente coincidiera con el filtro actual. Un ítem `20.png` atascado en la cola hacía que nunca se escaneara para ZIPs.
3. Al cambiar el filtro, `_scan_since` seguía siendo `url_since` (fecha del último sync), bloqueando la visibilidad de posts anteriores donde estaban los ZIPs.

### Solución implementada

**`tui/app.py: ArtistScreen._do_download()`**

| Cambio | Descripción |
|--------|-------------|
| `_matching_pending` | Cuenta pendientes que coinciden con el filtro actual (usa `ext_filter` en SQL). |
| Condición de reanudación | `existing_pending > 0 and not _filter_changed and _matching_pending > 0` — si los pendientes no coinciden, se fuerza un nuevo scan. |
| `_scan_since = None` | Cuando el filtro cambia, se ignora `url_since` y se hace scan completo para que posts viejos sean visibles. |
| Fingerprint condicional | `set_meta_int(folder, _filter_key, _filter_sig)` solo se llama cuando `_scan_files_seen > 0`. Si la API devuelve 0 (error transitorio), el fingerprint no se guarda y el próximo intento repite el scan. |

**`catalog.py: pending_count(ext_filter=...)`**

Agregado parámetro opcional `ext_filter: set[str] | None`. Cuando se pasa, añade cláusulas `LOWER(filename_hint) LIKE '%{ext}'` al WHERE para contar solo ítems del tipo deseado.

**`catalog.py: clean_pending_catalog_overlap()`**

Nueva función: elimina entradas de `pending_queue` cuyo `url_source` ya aparece en la tabla `files` (archivos descargados que no se limpiaron de la cola en sesiones previas).

### Otros cambios de la sesión

- **ArtistScreen**: reemplazado `Input` de texto para filtro con checkboxes idénticos a `BatchScreen` (usa `EXT_GROUPS`). El filtro se persiste en JSON en la DB del perfil.
- **`_encode_profile_filter` / `_decode_profile_filter`**: serialización JSON de filtros con compatibilidad hacia atrás con el formato de texto legado.
- **`ProfileFilterModal`**: modal de configuración de filtro por perfil, invocado desde `BatchScreen` cuando se activa "usar configuración por perfil".
- **`BatchScreen`**: checkbox "usar config por perfil" — al activarlo, se configura el filtro de cada perfil antes de iniciar el batch.
- **Pending count en lista de perfiles**: la columna `⏳ N` ahora cuenta solo archivos que coinciden con el filtro activo del perfil.
- **Mensajes DBG eliminados**: limpieza de logs de depuración temporales.

---

## Auditoría y correcciones — 2026-04-07

### Bugs corregidos

| ID | Archivo | Descripción | Fix |
|----|---------|-------------|-----|
| BUG-1 | `tui/app.py:796` | `compare_catalogs` llamado sin `await` → retornaba coroutine, datos siempre 0 | `await compare_catalogs(...)` |
| BUG-2 | `tui/app.py:1211` | `unique_to_b` es `list[str]` pero se trataba como `int` → modal imprimía la lista cruda | `len(s.get("unique_to_b", []))` |
| BUG-3 | `tui/app.py` deferred loop + `BatchScreen._download_url` | `next_counter` se asignaba antes de intentar la descarga → fallo generaba hueco en numeración | Mover `next_counter` + rename al bloque de éxito; descargar a nombre temporal `_dl_{hash}{ext}` |
| BUG-4 | `tui/app.py:_do_verify` | `NeedsManualAuth`/`NeedsPixivAuth` no capturadas → error genérico sin ofrecer modal de auth | Agregar handlers con `PatreonAuthModal` / `PixivAuthModal` (mismo patrón que `_do_download`) |
| BUG-5 | `tui/app.py:ArtistScreen.action_go_back` | `pop_screen()` inmediato con workers activos → callbacks intentaban actualizar widgets desmontados | Flag `_pending_exit`; `_set_busy(False)` detecta el flag y hace el pop |
| BUG-6 | `tui/app.py:_resolve_url` | `_name_similarity(_normalize_name(a), _normalize_name(b))` — normalización doble (idempotente pero redundante) | `_name_similarity(a, b)` — la función ya normaliza internamente |
| DEUDA-2 | `engine.py:407,422` | `asyncio.get_event_loop()` deprecado en Python 3.10+ dentro de contexto async | `asyncio.get_running_loop()` |

Todos los fixes verificados con suite de regresión: 11/11 ✓

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

### 2026-03-27 — Throttling severo + archivos corruptos en descargas de Kemono
**Síntoma:** descargas a ~50 KB/s desde el inicio, y archivos descargados con contenido erróneo (572 bytes de HTML en lugar del archivo real).
**Causa raíz:** el header `Accept: text/css` (bypass de DDoS-Guard para el API) estaba configurado a nivel del cliente httpx global, y se enviaba también en todas las requests al CDN de archivos. El CDN de kemono.cr responde con **HTTP 500** a requests con `Accept: text/css`, devolviendo una página de error HTML que el engine guardaba como el archivo descargado.
**Diagnóstico activo:** se verificó con requests directas:
  - `Accept: text/css` → 500 en CDN de archivos
  - `Accept: */*` → 200 OK, velocidad real del CDN (~70-100 KB/s por conexión)
  - El API documenta explícitamente en su 403: *"use Accept: text/css for scraping"* — solo aplica al API, no al CDN.
**Solución (engine.py):**
  - Renombrar `_DDG_HEADERS` → `_CLIENT_HEADERS` (sin `Accept: text/css`)
  - Agregar `_DDG_ACCEPT = {"Accept": "text/css"}` como dict separado
  - Aplicar `_DDG_ACCEPT` solo en `_get_json_with_retry()` como header por-request
  - El cliente httpx ya no incluye `Accept: text/css` en descargas de archivos
**Fix secundario (templates/kemono.py):**
  - `_hash_from_path()` esperaba el prefijo `/data/` en las rutas del CDN
  - El API de kemono.cr eliminó ese prefijo: paths ahora son `/{2chars}/{2chars}/{sha256}/{filename}`
  - Se actualizó para detectar ambos formatos (legado con `/data/`, actual sin él)

### 2026-03-27 — Descargas truncadas + TUI congelado en cola diferida
**Síntoma:** archivos se descargan parcialmente y se guardan como completos; workers desaparecen del panel sin entrada en el log; TUI se congela sin retroalimentación durante cola diferida.
**Causa 1 (archivos truncados):** el CDN de Kemono cierra la conexión antes de enviar todos los bytes declarados en `Content-Length` como mecanismo de throttling. `httpx` interpreta el cierre temprano como fin de stream normal — el engine guardaba el archivo parcial y lo reportaba como descarga exitosa.
**Solución:** validación de `Content-Length` vs bytes recibidos en `_do_download` (engine.py). Si `total < content_length`, se descarta el buffer, se trata como `NETWORK` error y se reintenta con backoff.
**Causa 1b (confirmado por el usuario):** todos los archivos que fallaron superan los 4 MB. El CDN reporta `x-cache-range: bytes=0-4194303` — sirve en chunks de exactamente 4 MB y cierra la conexión al terminar cada chunk. Archivos < 4 MB completan sin problema.
**Solución definitiva (engine.py — Range requests):** se refactorizó `_do_download` para acumular chunks via `Range: bytes=N-` entre segmentos. Se separaron los contadores de error: los range resumes no cuentan como error real; solo timeouts/connection errors/5xx incrementan `error_count`. Se agregó el helper `_parse_content_range_total()` para leer el tamaño total de `Content-Range`. El servidor puede ignorar el Range header (responde 200) — se detecta y se resetea el estado.
**Causa 2 (TUI congelado):** el handler de cola diferida llamaba `engine.download()` sin timeout. Un archivo que volvía a fallar podía mantener el hilo async bloqueado ~1050s (7 reintentos × 150s) con el TUI completamente sin retroalimentación.
**Solución:** cola diferida envuelta en `asyncio.wait_for(timeout=600.0)`, con actualización del worker slot 0 como indicador visual y logging explícito del timeout.

### 2026-03-29 — Workers desaparecen sin log tras implementar Range requests
**Síntoma:** tras implementar Range requests, el usuario reporta que "seguimos como al inicio" — workers desaparecen del panel sin entradas de descarga exitosa en el log.
**Diagnóstico (diag2.py — descarga completa de 5 MB):** el archivo se descargó en 1 solo request en 2.8s a 1813 KB/s. El CDN no truncó. El diagnóstico no reproducía el fallo.
**Causa raíz:** bug crítico en el manejo de timeouts dentro de `_do_download`. Cuando el Range request para el segundo chunk (bytes=4MB-) sufría un stall:
  1. `httpx.TimeoutException` se disparaba (stall_timeout=120s sin datos)
  2. El handler hacía `chunks.clear(); resume_from = 0; full_size = 0` ← **se perdían los 4 MB del primer chunk**
  3. El siguiente intento volvía a descargar desde 0 → mismo stall → mismo reset
  4. Tras 5 ciclos (5×120s = 600s), el `asyncio.wait_for` del TUI agotaba el timeout
  5. El archivo iba a la cola diferida (log: "⏸ diferido") — visible pero no como "completado"
**Solución parte 1 (engine.py — preservar progreso en timeout):** se modificaron los handlers de `httpx.TimeoutException` y `httpx.RequestError`:
  - `resume_from += chunk_received` (contabilizar bytes parciales recibidos antes del timeout)
  - Si `resume_from > 0`: conservar `chunks` y `full_size`, reintentar el Range request desde la posición actual
  - Solo si `resume_from == 0` (sin ningún dato): `chunks.clear(); full_size = 0` (inicio frío)
  - Mismo tratamiento para el guard de 0 bytes en post-stream

### 2026-03-29 — Workers desaparecen sin log (causa raíz definitiva)
**Síntoma:** tras todos los fixes anteriores, workers siguen desapareciendo del panel sin entrada en el log.
**Causa raíz:** bug de Python 3.12 + httpx HTTP/2. Cuando `asyncio.wait_for(timeout=600s)` dispara en el TUI:
  1. `wait_for` cancela la tarea interna inyectando `CancelledError` con un mensaje específico
  2. `CancelledError` se propaga hasta httpx, que hace cleanup del stream HTTP/2
  3. Durante el cleanup, h2 (la librería HTTP/2 subyacente) puede re-crear la excepción como un nuevo `CancelledError` SIN el mensaje original
  4. `wait_for` recibe el `CancelledError` "nuevo" (mensaje no coincide), asume que es una cancelación externa y lo re-lanza como `CancelledError` en lugar de `TimeoutError`
  5. El worker captura `CancelledError` con `except asyncio.CancelledError: raise` — sale del bloque sin log ni `done()`
  6. El worker muere silenciosamente, su tarea en `asyncio.gather` puede propagar la cancelación
**Solución (engine.py + tui/app.py — timeout interno en el engine):**
  - `download()` acepta nuevo parámetro `total_timeout: float | None`
  - `_do_download` rastrea el tiempo con `asyncio.get_event_loop().time()` y chequea al inicio de cada iteración del loop
  - Si `total_timeout` se excede, retorna `DownloadResult(error_kind=TIMEOUT)` de forma limpia — sin excepciones, sin CancelledError
  - TUI pasa `total_timeout=570.0` (30s antes del límite de wait_for)
  - `asyncio.wait_for` sube de 600s a 660s — solo actúa como safety net de último recurso
  - El TUI recibe un `DownloadResult` normal → `error_kind in DEFERRABLE` → log "⏸ diferido" ✓
**Impacto:** los workers ya no pueden desaparecer silenciosamente por la interacción Python 3.12 / httpx / wait_for.


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

## 2026-04-07 — Sistema de filtro por tipo de archivo en BatchScreen

### Problema
El campo de texto libre para filtrar extensiones en el modo Batch no funcionaba: aunque el usuario escribiera `jpg,png,webp`, la descarga continuaba bajando gif, mp4 y otros tipos no solicitados.

### Implementación: `EXT_GROUPS` + Checkboxes

Se reemplazó el `Input` de texto por un panel de `Checkbox` con 7 grupos predefinidos:

| ID | Etiqueta | Extensiones |
|----|----------|-------------|
| `images` | Imágenes | jpg, jpeg, png, webp, bmp, tiff, tif, avif, jxl |
| `anim` | Animaciones | gif, apng |
| `video` | Video | mp4, webm, mkv, avi, mov, wmv, flv, m4v, mpg, mpeg |
| `audio` | Audio | mp3, flac, ogg, wav, aac, m4a, opus, wma |
| `zip` | Comprimidos | zip, rar, 7z, tar, gz, bz2, xz, cbz, cbr |
| `docs` | Documentos | pdf, doc, docx, txt, epub |
| `project` | Archivos proyecto | psd, clip, xcf, kra, procreate, sai, sai2, ai, ora, mdp |

Más campo `Input` para extensiones custom. Sin ningún grupo marcado → descarga todo (sin filtro).

### Bug 1 — Filtro no aplicado en tiempo de descarga
**Síntoma:** aunque los checkboxes filtraban correctamente durante el scan (`_scan_url`), los archivos ya presentes en `pending_queue` de sesiones anteriores (sin filtro) se descargaban sin validación.
**Causa:** `_download_url` no recibía ni aplicaba `ext_filter`. El filtro solo existía en `_scan_url`.
**Solución:** `_download_url` recibe `ext_filter` y `exclude_mode`. Al inicio de `_one()` (worker por archivo), si el `filename_hint` no pasa el filtro → `remove_pending` + skip. El log muestra `⊘ archivo [excluido por filtro]`.

### Bug 2 — Extensiones sin punto nunca coincidían
**Síntoma:** después del fix anterior, con "Imágenes" marcado se rechazaban también los `.jpg`.
**Causa:** `EXT_GROUPS` almacena extensiones sin punto (`"jpg"`). `_build_include` en `_start_batch` hacía `include_exts.update(exts)` — sin agregar el punto. `_passes_ext_filter` compara contra `Path(filename).suffix` que devuelve `".jpg"` (con punto). `".jpg" in {"jpg"}` → `False` → todo rechazado.
**Solución:** `include_exts.update("." + ext for ext in exts)` — una línea.
**Por qué los tests no lo detectaron inicialmente:** el helper `_build_include` del test agregaba el punto manualmente, desacoplándose de la implementación real. Se agregó `test_ext_groups_dot_normalization` para fijar este contrato explícitamente.

### Archivos modificados
- `cherry_dl/tui/app.py` — `Checkbox` en imports, `EXT_GROUPS`, `BatchScreen.compose()`, `_start_batch()`, `_download_url()`
- `cherry_dl/tui/theme.tcss` — estilos `#batch-cfg-top`, `#batch-filter-groups`, `#batch-filter-custom`
- `tests/test_ext_groups.py` — 18 tests (nuevo archivo)

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

---

## 2026-03-29 — Fase 10: Estabilización de descargas Kemono — COMPLETA

### Problema
Descargas de kemono.cr fallaban sistemáticamente para archivos > 4 MB:
throttling severo, progreso que se congela y desaparece del panel de workers sin dejar ninguna entrada en el log, archivos truncados.

### Root causes encontrados y corregidos (en orden cronológico)

**1. `Accept: text/css` en descarga de archivos (CDN 500)**
El header DDG-bypass, necesario para el API, causaba HTTP 500 en el CDN de archivos.
Se separaron `_CLIENT_HEADERS` (base) y `_DDG_ACCEPT` (solo para llamadas al API).

**2. `_hash_from_path()` roto**
Kemono cambió el formato de paths de `/data/ab/cd/hash/file` a `/ab/cd/hash/file`.
La función no detectaba el nuevo formato y no extraía los hashes → dedup pre-descarga inutilizado.

**3. Archivos truncados a 4 MB**
El CDN sirve en chunks de exactamente 4 MB y cierra la conexión al terminar cada chunk.
httpx interpreta el cierre como fin de stream — el engine guardaba el parcial como completo.
Se implementaron Range requests: `_do_download` acumula chunks y continúa con `Range: bytes=N-`.

**4. Timeout pierde progreso acumulado**
Cuando el Range request del segundo chunk sufría stall (ReadTimeout):
el handler hacía `chunks.clear(); resume_from = 0`, tirando los 4 MB ya descargados.
Se corrigió: `resume_from += chunk_received`, solo resetear si `resume_from == 0`.

**5. Workers desaparecen sin log (causa raíz definitiva)**
Bug de interacción Python 3.12 + httpx HTTP/2 + `asyncio.wait_for`:
- Python 3.12 usa un `cancel_message` interno para que `wait_for` identifique "su" `CancelledError`
- httpx re-crea la excepción durante cleanup del stream HTTP/2, perdiendo el mensaje
- `wait_for` ve un `CancelledError` "extraño", lo trata como cancelación externa y lo re-lanza
- El worker `except asyncio.CancelledError: raise` lo captura → muere en silencio

**Solución definitiva:** timeout interno en el engine.
`download()` acepta `total_timeout: float | None`. `_do_download` chequea el tiempo
al inicio de cada iteración del loop y retorna `DownloadResult(TIMEOUT)` de forma limpia,
antes de que el `asyncio.wait_for` del TUI dispare. El TUI siempre recibe un resultado
legítimo → log garantizado. `wait_for` subió a 660s como safety net.

### Archivos modificados
- `cherry_dl/engine.py` — separación DDG headers, Range requests, timeout interno, preservar chunks
- `cherry_dl/templates/kemono.py` — `_hash_from_path()` para formato nuevo, Referer header
- `cherry_dl/tui/app.py` — `total_timeout=570` en llamadas, `wait_for` a 660s

---

## Fase 10 — Mapa persistente de URLs + politeness layer (2026-04-04)

### Problema raíz
Al reanudar una descarga o hacer "Actualizar", el template paginaba la API del servidor
(cientos de GETs en ráfaga) para descubrir qué URLs bajar. Cuando las descargas reales
comenzaban, el servidor ya había activado sus protecciones por el burst de API → throttling
desde el primer archivo. En Kemono esto era reproducible casi siempre con 3 workers.

### Solución implementada

#### cola persistente `pending_queue` en `catalog.db`
Nueva tabla por artista. Una URL descubierta se escribe en pending_queue antes de
descargar. Al completarse (éxito o dedup), se elimina. Si el proceso se interrumpe,
la cola sobrevive en disco y la próxima sesión retoma sin re-escanear la API.

Columnas: `url_source (PK)`, `download_url`, `filename_hint`, `post_id`, `post_published`,
`remote_hash`, `extra_headers (JSON)`, `profile_url_id`, `discovered_at`.

`profile_url_id` separa las colas de fuentes diferentes que comparten carpeta
(kemono + patreon del mismo artista → mismo catalog.db, colas distintas).

#### Dos fases separadas en `_do_download`
**Fase 1 — Scan:** itera la API lentamente (con delays), escribe a pending_queue.
Si pending_count > 0 (sesión interrumpida), salta el scan directamente.

**Fase 2 — Download:** lee pending_queue, construye FileInfo, alimenta workers.
Workers llaman `remove_pending` en éxito/dedup para limpiar la cola.

**Enfriamiento post-scan:** si el scan descubrió ≥ cooldown_threshold URLs nuevas,
espera cooldown_seconds antes de iniciar descargas (configurable por template).

#### Class vars nuevas en `SiteTemplate` (base.py)
- `scan_page_delay: float = 0.0` — delay entre páginas de API durante scan
- `cooldown_threshold: int = 200` — umbral para activar enfriamiento
- `cooldown_seconds: float = 10.0` — duración del enfriamiento

#### Kemono específico (kemono.py)
- `workers = 2` (era 3 — con 3 se activa rate limiting agresivo)
- `scan_page_delay = 1.0` — 1 segundo entre cada página de posts API
- `cooldown_threshold = 100` — umbral más bajo (Kemono es más sensible)
- `cooldown_seconds = 12.0`

Patreon y Pixiv ya tenían jitter interno entre requests (`random.uniform`),
no necesitan scan_page_delay adicional.

### Flujo resultante
```
Primera vez:
  scan API (1 pág/s) → pending_queue → [cooldown si >100] → descargar

Retomar tras interrupción:
  pending_count > 0 → SKIP scan → descargar directamente (0 requests API)

Actualizar (↑):
  pending_count = 0 → scan desde last_synced (pocas páginas) → descargar
```

### Archivos modificados
- `cherry_dl/catalog.py` — tabla pending_queue + 5 funciones nuevas + migración en init_catalog
- `cherry_dl/templates/base.py` — scan_page_delay, cooldown_threshold, cooldown_seconds
- `cherry_dl/templates/kemono.py` — workers=2, scan_page_delay=1.0, cooldown vars, delay en iter_files
- `cherry_dl/tui/app.py` — _do_download refactorizado en dos fases, remove_pending en worker_task

### Verificado en producción: 2026-03-29 ✓

---

## Fase 11 — Detección de duplicados + comparación por hash (2026-04-04)

### Motivación
Con 27+ perfiles activos, es posible crear duplicados accidentalmente (mismo artista, distinta URL).
Se implementaron dos sistemas complementarios de detección:

### Sistema 1 — Levenshtein en creación (preventivo)
Al resolver la URL de un nuevo perfil en `NewProfileModal._resolve_url()`, se compara el nombre
del artista resuelto contra todos los perfiles existentes usando `difflib.SequenceMatcher`.
Si la similitud ≥ 0.80, se muestra una notificación de advertencia con el nombre del perfil similar.
No bloquea la creación — el usuario decide.

### Sistema 2 — Comparación por hash (post-hoc)
`compare_catalogs(folder_a, folder_b)` en `catalog.py` carga los SHA-256 de archivos de dos
catalog.db y retorna `{total_a, total_b, matches, coverage, unique_to_b}`.
Si coverage ≥ 0.80, se considera posible duplicado.

Flujo completo desde ProfilesScreen:
```
Seleccionar perfil A → "⊗ Comparar" → SelectProfileModal (elige B)
  → _do_compare() → compare_catalogs() → CompareResultModal (muestra stats)
    → si acepta fusionar → MergeConfirmModal (confirmación final)
      → merge_profiles() → URLs de B reasignadas a A, B eliminado
```

`merge_profiles()` en `index.py`:
- Reasigna todas las profile_urls de remove_id a keep_id
- Elimina la entrada en artists y en profiles para remove_id
- NO mueve archivos en disco — el usuario es responsable
- Usa `PRAGMA foreign_keys = OFF` para gestión manual de cascada

### Botón "⟳ Chequear Todo"
Recorre todos los perfiles y actualiza la columna "Estado" con `pending_count()` sin hacer
ninguna descarga. Útil para ver de un vistazo qué perfiles tienen trabajos pendientes.

### Archivos modificados
- `cherry_dl/catalog.py` — `compare_catalogs()`
- `cherry_dl/index.py` — `merge_profiles()`
- `cherry_dl/tui/app.py` — columna Estado, botones Comparar/Chequear Todo, 3 nuevos modales,
  chequeo Levenshtein en NewProfileModal

### Bugs corregidos post-implementación

**Bug 1 — `pending_count` sin await en `_do_check_all`**
- Síntoma: `'>' not supported between instances of 'coroutine' and 'int'`
- Causa: `pending_count` es `async` pero se llamaba sin `await`
- Fix: `cnt = await pending_count(folder)`

**Bug 2 — `update_cell_at` no actualizaba celdas visibles**
- Síntoma: "Estado actualizado" sin cambio visual en la tabla
- Causa: `get_row_at()` devuelve valores con markup Rich (ej. `[yellow]⏳ 1375[/]`).
  Comparar `int(row[0]) == p["id"]` fallaba silenciosamente porque el contenido de la
  primera celda ya no era un string limpio tras el primer render.
- Fix: `_do_check_all` ahora llama directamente `await self._load_profiles()` que
  reconstruye la tabla completa con estados correctos. Más simple y sin el problema de parsing.

---

## Batch Download (2026-04-06)

### Problema
Kemono.cr (y potencialmente otros sitios) cortan la conexión tras cierto tiempo, matando el
proceso de descarga. Perfiles con 3000+ imágenes pueden tardar días si el usuario tiene que
reiniciar manualmente cada vez.

### Solución: `BatchScreen`
Nueva pantalla (`tui/app.py:BatchScreen`) que descarga todos los perfiles en secuencia con
reintentos automáticos. El truco clave es que **`pending_queue` ya existe**: si la conexión
se corta a mitad de un perfil, los archivos no descargados siguen en la cola y se retoman
exactamente ahí en la siguiente iteración.

### Flujo del loop
1. `to_process = todos los perfiles con URLs habilitadas`
2. Por cada perfil:
   - `pending_count()` → si 0, escanear API → si sigue en 0, marcar completo
   - `_download_url()` — descarga secuencial archivo por archivo
   - Si `consecutive_errors >= MAX_CONSECUTIVE (5)`: abandonar perfil, agregar a `still_incomplete`
3. `to_process = still_incomplete` → nueva iteración
4. Para cuando `to_process` quede vacío o el usuario presione ⏹ Detener

### Diferencias con `ArtistScreen._do_download`
| | ArtistScreen | BatchScreen |
|---|---|---|
| Workers | N (paralelo) | 1 (secuencial) |
| UI | Filas por worker | Log + ProgressBar |
| Timeout error | Diferido (cola secundaria) | `consecutive_errors` → abandona perfil |
| Scope | Un perfil a la vez | Todos los perfiles en loop |

La descarga secuencial es intencional: batch prioriza resiliencia sobre velocidad.

### Archivos modificados
- `cherry_dl/tui/app.py` — `BatchScreen` (escaneo, descarga, loop principal), botón "⚡ Batch"
- `cherry_dl/tui/theme.tcss` — estilos para `BatchScreen`

---

## Bug: `list_profiles` sin URLs (2026-04-06)

**Síntoma:** `BatchScreen` y `_do_scan_all` mostraban "No hay perfiles con URLs habilitadas"
aunque los perfiles existían y tenían URLs configuradas.

**Causa:** `list_profiles()` en `index.py` solo devuelve metadatos básicos del perfil
(`id`, `display_name`, `folder_path`, `url_count`) — NO incluye el array `urls`.
Ambas funciones hacían `profile.get("urls", [])` que siempre devolvía `[]`.

**Fix:** reemplazar `list_profiles` por un loop que llama `get_profile(id)` para cada perfil.
`get_profile` sí devuelve el campo `urls` completo con todas las URLs y su estado.

```python
_slim = await list_profiles(INDEX_DB)
profiles = []
for _p in _slim:
    _full = await get_profile(INDEX_DB, _p["id"])
    if _full:
        profiles.append(_full)
```

Afectaba: `ProfilesScreen._do_scan_all` y `BatchScreen._do_batch`.
