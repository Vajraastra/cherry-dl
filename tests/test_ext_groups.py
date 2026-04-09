"""
Tests para el sistema de filtro de extensiones por grupo (BatchScreen).

Cubre:
  - Integridad de EXT_GROUPS (sin duplicados, formato correcto)
  - _parse_ext_filter + _passes_ext_filter en modo include
  - Casos edge: filtro vacío, grupos combinados, extensiones custom
"""
import sys
from pathlib import Path

# Asegurar que el paquete sea importable desde la raíz del proyecto
sys.path.insert(0, str(Path(__file__).parent.parent))

from cherry_dl.tui.app import EXT_GROUPS
from cherry_dl.gui.bridge import _parse_ext_filter, _passes_ext_filter


# ── 1. Integridad de EXT_GROUPS ─────────────────────────────────────────────

def test_ext_groups_no_duplicates():
    """Ninguna extensión aparece en más de un grupo."""
    seen: dict[str, str] = {}
    for group_id, (label, exts) in EXT_GROUPS.items():
        for ext in exts:
            assert ext not in seen, (
                f"Extensión '{ext}' duplicada en grupos '{seen[ext]}' y '{group_id}'"
            )
            seen[ext] = group_id
    print(f"  ✓ {len(seen)} extensiones únicas en {len(EXT_GROUPS)} grupos")


def test_ext_groups_no_dots():
    """Las extensiones en EXT_GROUPS no deben tener punto (el punto lo agrega _parse_ext_filter)."""
    for group_id, (label, exts) in EXT_GROUPS.items():
        for ext in exts:
            assert not ext.startswith("."), (
                f"Extensión '{ext}' en grupo '{group_id}' tiene punto — debe ser sin punto"
            )
    print(f"  ✓ Ninguna extensión tiene punto prefijado")


def test_ext_groups_lowercase():
    """Todas las extensiones deben estar en minúsculas."""
    for group_id, (label, exts) in EXT_GROUPS.items():
        for ext in exts:
            assert ext == ext.lower(), (
                f"Extensión '{ext}' en grupo '{group_id}' no está en minúsculas"
            )
    print(f"  ✓ Todas las extensiones en minúsculas")


def test_ext_groups_required_groups():
    """Los grupos esperados deben existir."""
    required = {"images", "anim", "video", "audio", "zip", "docs", "project"}
    missing = required - set(EXT_GROUPS.keys())
    assert not missing, f"Grupos faltantes: {missing}"
    print(f"  ✓ Todos los grupos requeridos presentes: {sorted(EXT_GROUPS.keys())}")


def test_ext_groups_project_contents():
    """El grupo 'project' debe incluir los formatos gráficos acordados."""
    expected = {"psd", "clip", "xcf", "kra", "procreate", "sai", "sai2", "ai", "ora", "mdp"}
    actual = EXT_GROUPS["project"][1]
    missing = expected - actual
    assert not missing, f"Extensiones faltantes en grupo 'project': {missing}"
    print(f"  ✓ Grupo 'project' contiene: {sorted(actual)}")


def test_ext_groups_anim_separate_from_images():
    """gif y apng deben estar en 'anim', NO en 'images' — este era el bug original."""
    assert "gif"  not in EXT_GROUPS["images"][1], "gif no debe estar en 'images'"
    assert "apng" not in EXT_GROUPS["images"][1], "apng no debe estar en 'images'"
    assert "gif"  in EXT_GROUPS["anim"][1],   "gif debe estar en 'anim'"
    assert "apng" in EXT_GROUPS["anim"][1],   "apng debe estar en 'anim'"
    print("  ✓ gif y apng están en 'anim', separados de 'images'")


def test_ext_groups_webm_in_video_not_images():
    """webm debe estar en 'video', NO en 'images' — este era el otro bug original."""
    assert "webm" not in EXT_GROUPS["images"][1], "webm no debe estar en 'images'"
    assert "webm" in EXT_GROUPS["video"][1],   "webm debe estar en 'video'"
    print("  ✓ webm está en 'video', separado de 'images'")


# ── 2. _passes_ext_filter en modo include ───────────────────────────────────

def _build_include(group_ids: list[str], custom: str = "") -> set[str]:
    """
    Helper: replica exactamente la lógica de _start_batch().
    EXT_GROUPS almacena extensiones SIN punto; _passes_ext_filter espera CON punto.
    El punto se agrega aquí, igual que en la implementación real.
    """
    exts: set[str] = set()
    for gid in group_ids:
        exts.update("." + ext for ext in EXT_GROUPS[gid][1])  # punto obligatorio
    exts.update(_parse_ext_filter(custom))  # _parse_ext_filter ya agrega el punto
    return exts


def test_ext_groups_dot_normalization():
    """
    Verifica que _build_include agrega puntos correctamente.
    Este es el bug que hizo que jpg fuera rechazado: EXT_GROUPS tiene 'jpg'
    pero _passes_ext_filter compara contra '.jpg' (Path.suffix incluye el punto).
    """
    ext_filter = _build_include(["images"])
    assert ".jpg" in ext_filter,  "'.jpg' debe estar en el set (con punto)"
    assert ".png" in ext_filter,  "'.png' debe estar en el set"
    assert "jpg"  not in ext_filter, "'jpg' sin punto NO debe estar en el set"
    print("  ✓ Normalización de puntos correcta: '.jpg' en set, 'jpg' sin punto NO")


def test_empty_filter_downloads_all():
    """Sin grupos y sin custom → ext_filter vacío → se descarga todo."""
    ext_filter = _build_include([])
    assert ext_filter == set()
    for fname in ["image.jpg", "animation.gif", "video.mp4", "archive.zip", "project.psd"]:
        assert _passes_ext_filter(fname, ext_filter, exclude_mode=False), (
            f"Con filtro vacío, '{fname}' debería pasar"
        )
    print("  ✓ Filtro vacío → descarga todo")


def test_images_only():
    """Solo grupo 'images' → pasa jpg/png/webp, rechaza gif/mp4/zip."""
    ext_filter = _build_include(["images"])
    assert _passes_ext_filter("photo.jpg",      ext_filter, False)
    assert _passes_ext_filter("artwork.png",    ext_filter, False)
    assert _passes_ext_filter("image.webp",     ext_filter, False)
    assert not _passes_ext_filter("anim.gif",   ext_filter, False)
    assert not _passes_ext_filter("clip.mp4",   ext_filter, False)
    assert not _passes_ext_filter("pack.zip",   ext_filter, False)
    assert not _passes_ext_filter("file.psd",   ext_filter, False)
    print("  ✓ Solo 'images' → acepta jpg/png/webp, rechaza gif/mp4/zip/psd")


def test_images_plus_anim():
    """Imágenes + Animaciones → pasa jpg/gif/apng, rechaza mp4/zip."""
    ext_filter = _build_include(["images", "anim"])
    assert _passes_ext_filter("photo.jpg",      ext_filter, False)
    assert _passes_ext_filter("anim.gif",       ext_filter, False)
    assert _passes_ext_filter("sticker.apng",   ext_filter, False)
    assert not _passes_ext_filter("video.mp4",  ext_filter, False)
    assert not _passes_ext_filter("pack.zip",   ext_filter, False)
    print("  ✓ 'images' + 'anim' → acepta jpg/gif/apng, rechaza mp4/zip")


def test_project_files():
    """Solo grupo 'project' → pasa psd/clip/xcf/kra/sai/ai/etc., rechaza jpg/gif/mp4."""
    ext_filter = _build_include(["project"])
    for fname in ["work.psd", "draw.clip", "image.xcf", "art.kra",
                  "illustration.procreate", "sketch.sai", "sketch.sai2",
                  "vector.ai", "raster.ora", "page.mdp"]:
        assert _passes_ext_filter(fname, ext_filter, False), f"'{fname}' debería pasar en grupo 'project'"
    assert not _passes_ext_filter("photo.jpg", ext_filter, False)
    assert not _passes_ext_filter("anim.gif",  ext_filter, False)
    assert not _passes_ext_filter("video.mp4", ext_filter, False)
    print("  ✓ Grupo 'project' acepta todos los formatos gráficos, rechaza jpg/gif/mp4")


def test_custom_extensions():
    """Extensiones custom se agregan al include (sin grupos)."""
    ext_filter = _build_include([], custom="flp,reason")
    assert _passes_ext_filter("song.flp",     ext_filter, False)
    assert _passes_ext_filter("track.reason", ext_filter, False)
    assert not _passes_ext_filter("photo.jpg", ext_filter, False)
    print("  ✓ Custom 'flp,reason' → acepta .flp/.reason, rechaza .jpg")


def test_custom_plus_group():
    """Custom + grupo combinados funcionan juntos."""
    ext_filter = _build_include(["images"], custom="flp")
    assert _passes_ext_filter("photo.jpg", ext_filter, False)
    assert _passes_ext_filter("song.flp",  ext_filter, False)
    assert not _passes_ext_filter("video.mp4", ext_filter, False)
    print("  ✓ 'images' + custom 'flp' → acepta jpg/flp, rechaza mp4")


def test_case_insensitive():
    """El filtro es case-insensitive para extensiones en mayúsculas."""
    ext_filter = _build_include(["images"])
    assert _passes_ext_filter("PHOTO.JPG",  ext_filter, False)
    assert _passes_ext_filter("Image.PNG",  ext_filter, False)
    assert _passes_ext_filter("art.WebP",   ext_filter, False)
    print("  ✓ Filtro case-insensitive (JPG/PNG/WebP reconocidos)")


def test_no_extension_file():
    """Archivo sin extensión no pasa ningún filtro con grupos activos."""
    ext_filter = _build_include(["images", "video"])
    assert not _passes_ext_filter("Makefile", ext_filter, False)
    print("  ✓ Archivos sin extensión rechazados cuando hay filtro activo")


def test_gif_blocked_when_only_images_selected():
    """
    Reproduce el bug original: con solo 'Imágenes' seleccionado,
    gif y mp4 deben ser rechazados incluso si estaban en pending_queue
    sin filtro de una sesión anterior.
    """
    ext_filter = _build_include(["images"])
    # Estos son los que fallaban antes del fix
    assert not _passes_ext_filter("anim.gif",    ext_filter, False), "gif debe ser rechazado"
    assert not _passes_ext_filter("video.mp4",   ext_filter, False), "mp4 debe ser rechazado"
    assert not _passes_ext_filter("clip.webm",   ext_filter, False), "webm debe ser rechazado"
    # Estos deben pasar
    assert _passes_ext_filter("photo.jpg",       ext_filter, False), "jpg debe pasar"
    assert _passes_ext_filter("artwork.png",     ext_filter, False), "png debe pasar"
    assert _passes_ext_filter("image.webp",      ext_filter, False), "webp debe pasar"
    print("  ✓ Bug original: gif/mp4/webm rechazados cuando solo 'Imágenes' está marcado")


def test_filter_applied_at_download_time():
    """
    Verifica que el filtro actúa en modo include (exclude_mode=False),
    que es el modo que usa _download_url tras el fix.
    """
    ext_filter = _build_include(["images", "anim"])
    # Simula pending_queue con archivos mixtos de sesión anterior sin filtro
    cola_pendiente = [
        "artwork.jpg", "sketch.png", "anim.gif", "sticker.apng",
        "video.mp4", "clip.webm", "pack.zip", "source.psd",
    ]
    deben_pasar   = {"artwork.jpg", "sketch.png", "anim.gif", "sticker.apng"}
    deben_bloquearse = {"video.mp4", "clip.webm", "pack.zip", "source.psd"}

    for fname in cola_pendiente:
        resultado = _passes_ext_filter(fname, ext_filter, False)
        if fname in deben_pasar:
            assert resultado, f"'{fname}' debería pasar con filtro images+anim"
        else:
            assert not resultado, f"'{fname}' debería bloquearse con filtro images+anim"
    print("  ✓ Filtro aplicado en download_time: bloquea mp4/webm/zip/psd de pending_queue")


# ── Runner ───────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        # Integridad
        test_ext_groups_no_duplicates,
        test_ext_groups_no_dots,
        test_ext_groups_lowercase,
        test_ext_groups_required_groups,
        test_ext_groups_project_contents,
        test_ext_groups_anim_separate_from_images,
        test_ext_groups_webm_in_video_not_images,
        # Normalización de puntos (bug crítico)
        test_ext_groups_dot_normalization,
        # Lógica de filtro
        test_empty_filter_downloads_all,
        test_images_only,
        test_images_plus_anim,
        test_project_files,
        test_custom_extensions,
        test_custom_plus_group,
        test_case_insensitive,
        test_no_extension_file,
        test_gif_blocked_when_only_images_selected,
        test_filter_applied_at_download_time,
    ]

    passed = 0
    failed = 0
    for test in tests:
        print(f"\n[TEST] {test.__name__}")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ FALLO: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR INESPERADO: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Resultado: {passed}/{passed+failed} tests pasados")
    if failed:
        print(f"  ✗ {failed} fallaron")
        sys.exit(1)
    else:
        print("  ✓ Todos los tests pasaron")


if __name__ == "__main__":
    run_all()
