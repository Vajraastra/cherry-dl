# Feature: Artist Discovery Engine ("What's New?")

## Prompt para Claude Code

Estoy implementando una nueva feature en mi scrapper de arte. El scrapper actualmente funciona con un sistema de templates para descargar contenido de Fanbox/Pixiv, Patreon, y tiene soporte parcial para kemono.cr (en deprecación). La UI está construida en **Python con Textual (TUI)**. Necesito que implementes un módulo de descubrimiento de artistas usando sitios booru como motor de búsqueda, NO como fuente de descarga.

---

## Contexto del problema

Los sitios booru (Danbooru, Gelbooru, etc.) tienen cantidades masivas de contenido pero están pobremente catalogados para uso como colección por artista. Los problemas incluyen tags inconsistentes, duplicados masivos entre boorus, imágenes re-subidas en distintas resoluciones y posts sin tag de artista. Intentar usarlos como fuente de descarga directa es inviable para un colector de arte organizado.

Sin embargo, los boorus (especialmente Danbooru) mantienen metadatos valiosos: **source URLs** que apuntan a las fuentes originales (Pixiv, Twitter/X, Fanbox, etc.) y **artist entries** que son esencialmente una base de datos de artistas con sus URLs conocidas en todas las plataformas.

La idea es usar los boorus exclusivamente como **motor de descubrimiento**, no de descarga. El booru identifica artistas y sus plataformas, y luego delega la descarga real a los templates existentes del scrapper (Fanbox, Pixiv, Patreon, Ko-fi, etc.).

---

## Arquitectura general

El sistema tiene dos capas separadas:

### Capa 1: Discovery Engine (backend)

Módulo que consulta APIs de boorus para extraer información de artistas y sus fuentes. No descarga imágenes de arte, solo metadata y thumbnails de preview.

#### API principal: Danbooru

Danbooru es el booru más riguroso en catalogación de artistas. Endpoints relevantes:

**Buscar artista por nombre:**
```
GET https://danbooru.donmai.us/artists.json?search[name]=nombre_artista
```

**Buscar artista por URL (búsqueda inversa):**
```
GET https://danbooru.donmai.us/artists.json?search[url_match]=pixiv.net/users/12345
```

**Respuesta tipo del artist entry:**
```json
{
  "id": 12345,
  "name": "artist_name",
  "other_names": ["alias1", "alias2", "旧名前"],
  "url_string": "https://www.pixiv.net/users/123456\nhttps://twitter.com/artist\nhttps://artistname.fanbox.cc\nhttps://ko-fi.com/artist",
  "is_banned": false,
  "is_deleted": false
}
```

El campo `url_string` es el más valioso: contiene todas las URLs conocidas del artista separadas por newlines. Estas URLs deben parsearse y clasificarse por plataforma.

**Buscar posts recientes de un artista (para thumbnails de preview):**
```
GET https://danbooru.donmai.us/posts.json?tags=artist_name&limit=4
```

Cada post incluye `preview_file_url` (thumbnail pequeño) y `source` (URL original).

**Buscar artistas actualizados recientemente (para el feed "What's New?"):**
```
GET https://danbooru.donmai.us/posts.json?tags=order:id_desc&limit=50
```

De estos posts, extraer los tags de tipo `artist` y agrupar para mostrar qué artistas tienen contenido nuevo.

#### Autenticación Danbooru

La API de Danbooru funciona sin auth pero con rate limiting estricto (sin auth). Con cuenta gratuita se obtiene mayor rate limit:
```
GET https://danbooru.donmai.us/endpoint.json?login=usuario&api_key=key
```

Implementar manejo de rate limiting con backoff exponencial.

#### APIs secundarias (opcionales, para mayor cobertura)

- **Gelbooru:** `https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags=artist_name`
  - Menos estructurado que Danbooru pero mayor volumen.
  - No tiene artist entries, solo tags en posts. Los source URLs están en cada post.

- **Safebooru/otros:** Misma estructura que Gelbooru generalmente.

#### Clasificador de URLs

Módulo que toma una URL y la clasifica por plataforma. Debe reconocer patrones como:

```python
PLATFORM_PATTERNS = {
    "pixiv": [
        r"pixiv\.net/users/(\d+)",
        r"pixiv\.net/member\.php\?id=(\d+)",
    ],
    "fanbox": [
        r"(\w+)\.fanbox\.cc",
        r"fanbox\.cc/@(\w+)",
    ],
    "patreon": [
        r"patreon\.com/(\w+)",
    ],
    "twitter": [
        r"twitter\.com/(\w+)",
        r"x\.com/(\w+)",
    ],
    "ko-fi": [
        r"ko-fi\.com/(\w+)",
    ],
    "subscribestar": [
        r"subscribestar\.adult/(\w+)",
        r"subscribestar\.com/(\w+)",
    ],
    "fantia": [
        r"fantia\.jp/fanclubs/(\d+)",
    ],
    "gumroad": [
        r"(\w+)\.gumroad\.com",
        r"gumroad\.com/(\w+)",
    ],
    "booth": [
        r"(\w+)\.booth\.pm",
        r"booth\.pm/(\w+)",
    ],
    "skeb": [
        r"skeb\.jp/@(\w+)",
    ],
    "website": [
        # Fallback para URLs que no matchean ningún patrón conocido
    ]
}
```

Este clasificador alimenta el sistema para saber qué templates de descarga del scrapper ya existentes puede invocar.

#### Modelo de datos del artista descubierto

```python
@dataclass
class DiscoveredArtist:
    """Artista descubierto via booru discovery engine."""
    danbooru_id: int | None
    name: str  # nombre canónico
    aliases: list[str]  # otros nombres conocidos
    platforms: dict[str, str]  # {"pixiv": "12345", "fanbox": "username", ...}
    platform_urls: dict[str, str]  # {"pixiv": "https://...", "fanbox": "https://...", ...}
    tags: list[str]  # tags del booru asociados (para categorización)
    thumbnail_urls: list[str]  # URLs de preview thumbnails (max 4)
    last_seen: datetime  # última vez que apareció contenido nuevo
    source_booru: str  # de qué booru se descubrió
```

---

### Capa 2: TUI - Pantalla de descubrimiento (frontend)

Interfaz en Textual que muestra el feed de artistas descubiertos.

#### Dependencias necesarias

```
textual>=0.80.0
textual-image>=0.8.5  # Para renderizar thumbnails en terminal
```

`textual-image` soporta Terminal Graphics Protocol (Kitty), Sixel, y fallback Unicode. Esto significa que en Kitty/WezTerm se verán thumbnails reales, y en terminales básicas se verá una aproximación en bloques — ambos funcionales para el propósito de descubrimiento.

#### Estructura de pantalla propuesta

```
┌─────────────────────────────────────────────────┐
│ [Header] Artist Discovery - What's New?         │
├──────────┬──────────────────────────────────────┤
│          │                                      │
│  Tags    │  ┌──────┐ ┌──────┐ ┌──────┐        │
│  Filter  │  │thumb │ │thumb │ │thumb │        │
│          │  │      │ │      │ │      │        │
│ ☐ anime  │  │ Name │ │ Name │ │ Name │        │
│ ☐ manga  │  │ Pixiv│ │ Fanb.│ │ Patr.│        │
│ ☐ illust │  ├──────┤ ├──────┤ ├──────┤        │
│ ☐ comic  │  │thumb │ │thumb │ │thumb │        │
│ ☐ game   │  │      │ │      │ │      │        │
│          │  │ Name │ │ Name │ │ Name │        │
│ [Search] │  │ Ko-fi│ │ Twit.│ │ Fant.│        │
│          │  └──────┘ └──────┘ └──────┘        │
│          │                                      │
│          │  [Load More...]                      │
├──────────┴──────────────────────────────────────┤
│ [Footer] ↑↓ Navigate  Enter: Details  A: Add   │
└─────────────────────────────────────────────────┘
```

#### Widget: ArtistCard

Widget custom que representa una card de artista en el grid.

```python
class ArtistCard(Static):
    """Card de artista para el grid de descubrimiento."""

    # Composición:
    # - Thumbnail (via textual-image o fallback placeholder)
    # - Nombre del artista (Static/Label)
    # - Badges de plataformas disponibles (ej: [Px] [Fb] [Pa] [Ko])
    # - Indicador de "nuevo" si fue descubierto recientemente

    # Eventos:
    # - on_click / Enter -> abre panel de detalle del artista
    # - tecla "a" -> agrega artista a la cola de descarga del scrapper
```

Consideraciones del thumbnail:
- Descargar thumbnails async usando workers de Textual para no bloquear la UI.
- Implementar lazy loading: solo cargar thumbnails de cards visibles en viewport.
- Cache de thumbnails en disco (directorio temporal) para evitar re-descargas al hacer scroll.
- Si textual-image no está disponible o la terminal no soporta imágenes, mostrar un placeholder con el nombre del artista en texto estilizado con colores.

#### Widget: ArtistDetailPanel

Panel que se abre al seleccionar un artista del grid.

```
┌─ Artist Detail ─────────────────────────┐
│                                          │
│  nombre_artista (alias1, alias2)         │
│                                          │
│  Plataformas encontradas:                │
│  ✓ Pixiv     pixiv.net/users/12345       │
│  ✓ Fanbox    artist.fanbox.cc            │
│  ✓ Patreon   patreon.com/artist          │
│  ○ Ko-fi     ko-fi.com/artist            │
│  ○ Twitter   x.com/artist                │
│                                          │
│  ✓ = template de descarga disponible     │
│  ○ = solo link, sin template             │
│                                          │
│  Tags: anime, illustration, manga        │
│                                          │
│  [Agregar a descargas] [Abrir URLs]      │
│  [Volver]                                │
└──────────────────────────────────────────┘
```

El panel debe distinguir visualmente entre plataformas que el scrapper ya sabe descargar (✓) y plataformas que solo son links informativos (○).

#### Pantalla: TagCatalog

Vista alternativa de navegación por categorías/tags.

```python
# Usa Tree widget de Textual para navegación jerárquica
# Estructura:
# ├── illustration
# │   ├── anime_style (245 artistas)
# │   ├── realistic (89 artistas)
# │   └── pixel_art (34 artistas)
# ├── manga
# │   ├── doujinshi (567 artistas)
# │   └── webcomic (123 artistas)
# └── game_assets
#     ├── character_design (78 artistas)
#     └── concept_art (45 artistas)
```

Al seleccionar una categoría, se filtra el grid principal para mostrar solo artistas con ese tag.

---

## Flujo de datos completo

```
1. Usuario abre pantalla "What's New?"
2. Discovery Engine consulta Danbooru API: posts recientes → extrae artist tags
3. Para cada artista nuevo/actualizado:
   a. Consulta artist entry → obtiene url_string
   b. Clasifica URLs por plataforma
   c. Descarga thumbnail de preview (async)
   d. Construye DiscoveredArtist
4. TUI renderiza grid de ArtistCards
5. Usuario navega, filtra por tags, selecciona artista
6. ArtistDetailPanel muestra plataformas disponibles
7. Usuario presiona "Agregar a descargas"
8. Sistema crea entradas en la cola de descarga usando los templates
   existentes del scrapper (Pixiv, Fanbox, Patreon, etc.)
```

---

## Consideraciones técnicas importantes

### Rate limiting
- Danbooru sin auth: ~1 req/segundo (ser conservador).
- Danbooru con auth: más permisivo pero respetar headers de rate limit.
- Implementar cache agresivo de artist entries (cambian infrecuentemente).
- Los thumbnails de preview se cachean en disco por URL hash.

### Base de datos local de artistas descubiertos
- Usar SQLite para persistir artistas descubiertos, evitar re-consultas.
- Tabla: `discovered_artists` con campos del dataclass + timestamps.
- Tabla: `artist_platforms` relación artista → plataforma → URL.
- Tabla: `discovery_cache` para cachear respuestas de API del booru.
- Índices por nombre, por plataforma, por tag, por fecha de descubrimiento.

### Separación de concerns
- El discovery engine NO debe saber nada sobre cómo descargar contenido.
- Solo produce `DiscoveredArtist` objects.
- La integración con el sistema de descargas existente ocurre en una capa superior que mapea `platform_urls` a templates del scrapper.
- El discovery engine es un "template de consulta", no un "template de descarga".

### Manejo de thumbnails para la TUI
```python
# Flujo de carga de thumbnail:
# 1. Card se monta en el DOM de Textual
# 2. Worker async descarga thumbnail a /tmp/scrapper_thumbs/{hash}.jpg
# 3. Si textual-image disponible: renderizar imagen real
# 4. Si no: generar Rich Text placeholder con colores del artista
# 5. Al salir del viewport: liberar recursos del widget de imagen
```

### Detección de capacidad de terminal
```python
# Antes de iniciar la pantalla de descubrimiento:
# 1. Verificar si textual-image está instalado
# 2. Detectar protocolo soportado (TGP > Sixel > Unicode fallback)
# 3. Ajustar tamaño del grid según capacidad:
#    - Con imágenes reales: cards más grandes, grid 3-4 columnas
#    - Con fallback Unicode: cards más compactas, grid 4-6 columnas
#    - Solo texto: usar DataTable en vez de grid de cards
```

---

## Estructura de archivos sugerida

```
scrapper/
├── discovery/
│   ├── __init__.py
│   ├── engine.py          # DiscoveryEngine - lógica principal
│   ├── booru/
│   │   ├── __init__.py
│   │   ├── base.py        # BooruProvider ABC
│   │   ├── danbooru.py    # DanbooruProvider
│   │   └── gelbooru.py    # GelbooruProvider (opcional)
│   ├── classifier.py      # URLClassifier - clasifica URLs por plataforma
│   ├── models.py          # DiscoveredArtist y otros dataclasses
│   ├── cache.py           # Cache SQLite para artistas y respuestas API
│   └── thumbnails.py      # ThumbnailManager - descarga/cache de thumbs
├── tui/
│   ├── screens/
│   │   ├── discovery.py   # DiscoveryScreen - pantalla principal
│   │   └── catalog.py     # TagCatalogScreen - navegación por tags
│   ├── widgets/
│   │   ├── artist_card.py # ArtistCard widget
│   │   ├── artist_detail.py  # ArtistDetailPanel widget
│   │   ├── tag_filter.py  # TagFilter sidebar widget
│   │   └── thumbnail.py   # ThumbnailWidget - wrapper de textual-image
│   └── css/
│       ├── discovery.tcss # Estilos de la pantalla de descubrimiento
│       └── catalog.tcss   # Estilos del catálogo
└── templates/
    └── ... (templates de descarga existentes)
```

---

## Prioridades de implementación

1. **Primero:** `discovery/models.py` + `discovery/classifier.py` — son independientes y testeables.
2. **Segundo:** `discovery/booru/danbooru.py` — solo Danbooru inicialmente, es el más completo.
3. **Tercero:** `discovery/cache.py` + `discovery/thumbnails.py` — necesarios antes de la TUI.
4. **Cuarto:** `tui/widgets/artist_card.py` + `tui/widgets/thumbnail.py` — el componente visual core.
5. **Quinto:** `tui/screens/discovery.py` — ensamblar todo en la pantalla.
6. **Sexto:** `tui/screens/catalog.py` + tag filtering — feature secundaria.
7. **Último:** Integración con el sistema de descargas existente (mapear plataformas a templates).

Cada paso debe ser funcional de forma aislada antes de pasar al siguiente. El discovery engine debe poder ejecutarse headless (sin TUI) para testing.
