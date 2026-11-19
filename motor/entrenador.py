from pathlib import Path
import zstandard as zstd

TAMAÑO_MAX_DICT = 112 * 1024          # 112 KB
MAX_MUESTRAS = 2000                    # máximo de archivos a usar
TAMAÑO_MAX_MUESTRA = 1 * 1024 * 1024   # 1 MB por muestra
TAMAÑO_MIN_MUESTRA = 8                 # bytes mínimos


def _recolectar_muestras(carpeta, extensiones=None):
    """Recorre una carpeta y devuelve una lista de bytes para entrenar.

    Si se pasan extensiones, solo toma archivos con esas extensiones.
    Si un archivo es muy grande, solo lee el primer MB.
    """
    muestras = []
    for f in Path(carpeta).rglob('*'):
        if not f.is_file():
            continue
        if extensiones is not None:
            if f.suffix.lower() not in extensiones:
                continue
        tamaño = f.stat().st_size
        if tamaño < TAMAÑO_MIN_MUESTRA:
            continue
        try:
            with open(f, 'rb') as fh:
                datos = fh.read(TAMAÑO_MAX_MUESTRA)
        except Exception:
            continue
        muestras.append(datos)
        if len(muestras) >= MAX_MUESTRAS:
            break
    return muestras


def entrenar(carpeta, salida):
    """Entrena un diccionario Zstd desde una carpeta (sin filtrar por tipo)."""
    muestras = _recolectar_muestras(carpeta, extensiones=None)

    if len(muestras) < 10:
        raise ValueError(
            f"Muy pocas muestras, necesito al menos 10 archivos. "
            f"Encontré {len(muestras)} en {carpeta}"
        )

    print(f"Entrenando con {len(muestras)} archivos (sin filtro)...")
    dict_data = zstd.train_dictionary(TAMAÑO_MAX_DICT, muestras)
    Path(salida).parent.mkdir(parents=True, exist_ok=True)
    Path(salida).write_bytes(dict_data.as_bytes())
    print(f"Diccionario guardado en {salida}")


def entrenar_por_tipo(carpeta, extensiones, salida):
    """Entrena un diccionario Zstd solo con archivos de ciertas extensiones.

    'extensiones' es una lista, por ejemplo ['.csv'] o ['.xml', '.gml'].
    """
    extensiones = [e.lower() for e in extensiones]
    muestras = _recolectar_muestras(carpeta, extensiones=set(extensiones))

    if len(muestras) < 10:
        raise ValueError(
            f"Muy pocas muestras para {extensiones}: encontré {len(muestras)}, "
            f"necesito al menos 10."
        )

    print(f"Entrenando con {len(muestras)} archivos de tipo {extensiones}...")
    dict_data = zstd.train_dictionary(TAMAÑO_MAX_DICT, muestras)
    Path(salida).parent.mkdir(parents=True, exist_ok=True)
    Path(salida).write_bytes(dict_data.as_bytes())
    print(f"Diccionario guardado en {salida}")