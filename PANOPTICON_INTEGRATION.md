# Integración Panopticon × cherry-dl

## Resumen

**Panopticon** (herramienta de curación de datasets de IA) lee el `catalog.db`
de cherry-dl en modo **estrictamente read-only** para conocer qué archivos
descargó cherry-dl y cuáles existen realmente en disco.

No hay escritura, no hay modificación de schema, no hay dependencia de versión.

---

## Qué lee Panopticon de catalog.db

Solo la tabla `files`, solo estas columnas:

| Columna | Uso en Panopticon |
|---|---|
| `hash` | Identificador (guardado internamente, no modificado) |
| `filename` | Construye el path absoluto junto al folder del artista |
| `url_source` | Metadata informativa (origen de la imagen) |
| `date_added` | Usado como `created_date` en el índice de Panopticon |
| `file_size` | Tamaño inicial (Panopticon puede actualizarlo en su propia DB) |

Las tablas `meta` y `pending_queue` **no se leen**.

---

## Qué lee Panopticon de index.db

Archivo: `~/.cherry-dl/index.db`  
Tabla: `profiles`  
Columnas: `display_name`, `primary_site`, `last_checked`  
Filtro: `WHERE folder_path = <carpeta del artista>`

Solo se usa para mostrar el nombre del artista en la interfaz de Panopticon.
Es completamente opcional: si index.db no existe, Panopticon sigue funcionando.

---

## Qué NO hace Panopticon

- No escribe en `catalog.db`
- No escribe en `index.db`
- No modifica el schema de ninguna tabla de cherry-dl
- No elimina ni mueve archivos descargados por cherry-dl
- No interfiere con descargas en curso (abre catalog.db con URI `?mode=ro`)

---

## Cómo filtra Panopticon los archivos

cherry-dl descarga archivos de todo tipo (`.psd`, `.zip`, `.mp4`, customs, etc.)
y mantiene entradas en catalog.db de archivos borrados intencionalmente por el
usuario (mecanismo de deduplicación de cherry-dl).

Panopticon aplica dos filtros sobre las filas de `files`:

1. **Extensión de imagen**: solo procesa `.png`, `.jpg`, `.jpeg`, `.webp`, `.avif`
2. **Existencia en disco**: `Path(folder / filename).is_file()` debe ser `True`

Resultado: Panopticon indexa únicamente las imágenes que el usuario decidió
conservar, ignorando el resto del catálogo de cherry-dl.

---

## Datos de Panopticon: dónde viven

Todos los datos que Panopticon agrega (tags, ratings, prompts editados) se
guardan en **dos lugares propios de Panopticon**, nunca en cherry-dl:

1. **Metadata embebida en la imagen** (`panopticon_data` en PNG tEXt / XMP)
   — viaja con el archivo si se mueve o renombra.
2. **`panopticon.db`** — índice central de Panopticon (path, rating, tags).

---

## Consideraciones para cherry-dl

- El modo `?mode=ro` garantiza que Panopticon nunca abre catalog.db en modo
  escritura, incluso si el proceso tiene permisos de escritura en el archivo.
- Si cherry-dl está escribiendo en catalog.db al mismo tiempo que Panopticon
  lo lee, SQLite maneja el lock sin conflictos (WAL mode de cherry-dl + timeout
  de 5 segundos en el lado de Panopticon).
- Si en el futuro cherry-dl agrega columnas o tablas nuevas, Panopticon las
  ignora automáticamente (solo hace SELECT de las columnas listadas arriba).

---

## Archivo clave en Panopticon

```
core/catalog_reader.py   — lector read-only (is_cherry_catalog, get_image_files, get_artist_info)
modules/librarian/logic/indexer.py — detecta catalog.db y delega a CatalogReader
```
