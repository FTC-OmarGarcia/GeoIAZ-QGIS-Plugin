from pathlib import Path
import zstandard as zstd
import subprocess


# Extensiones que consideramos "texto"
EXTENSIONES_TEXTO = {
    '.csv', '.txt', '.json', '.geojson',
    '.xml', '.gml', '.kml', '.html', '.htm',
    '.md', '.rdf',
}


def entrenar_dict_texto(carpeta, tamaño_dict=112 * 1024):
    """Entrena un diccionario Zstd sobre todos los archivos de texto de una carpeta."""
    muestras = []
    for f in Path(carpeta).rglob('*'):
        if not f.is_file():
            continue
        if f.suffix.lower() not in EXTENSIONES_TEXTO:
            continue
        tamaño = f.stat().st_size
        # Solo archivos entre 100 bytes y 5 MB
        if 100 < tamaño < 5 * 1024 * 1024:
            muestras.append(f.read_bytes())
            if len(muestras) >= 500:
                break

    if len(muestras) < 10:
        raise ValueError(
            f"Muy pocas muestras para entrenar: {len(muestras)}. "
            f"Necesito al menos 10 archivos de texto."
        )

    print(f"Entrenando diccionario con {len(muestras)} archivos de texto...")
    dict_data = zstd.train_dictionary(tamaño_dict, muestras)
    print(f"Diccionario entrenado: {len(dict_data.as_bytes()):,} bytes")
    return dict_data


def comprimir_con_dict(datos, dict_data=None, nivel=19):
    """Comprime datos con o sin diccionario."""
    kwargs = {'level': nivel}
    if dict_data is not None:
        kwargs['dict_data'] = dict_data
    cctx = zstd.ZstdCompressor(**kwargs)
    return cctx.compress(datos)


def bench_texto(carpeta):
    """Compara compresión de texto: Zstd sin dict vs Zstd con dict vs 7z."""
    carpeta = Path(carpeta)
    if not carpeta.exists():
        print(f"La carpeta {carpeta} no existe.")
        return

    # 1. Entrenar diccionario con la propia carpeta
    try:
        dict_data = entrenar_dict_texto(carpeta)
    except ValueError as e:
        print(f"Error: {e}")
        return

    # 2. Listar archivos de texto
    archivos = [
        f for f in carpeta.rglob('*')
        if f.is_file() and f.suffix.lower() in EXTENSIONES_TEXTO
    ]

    if not archivos:
        print("No hay archivos de texto para comparar.")
        return

    print()
    print(f"{'Archivo':<32} {'Original':>11} {'Zstd':>11} {'Zstd+Dict':>11} {'7z':>11} {'Mejora':>8}")
    print("-" * 92)

    total_orig = 0
    total_zstd = 0
    total_dict = 0
    total_7z = 0

    for f in archivos[:30]:  # Solo los primeros 30 para no tardar horas
        try:
            datos = f.read_bytes()
        except Exception:
            continue
        orig = len(datos)
        if orig < 100:
            continue

        # Zstd sin diccionario
        z_plain = len(comprimir_con_dict(datos))

        # Zstd con diccionario
        z_dict = len(comprimir_con_dict(datos, dict_data))

        # 7z (si está instalado)
        s_7z = 0
        try:
            out_7z = Path('salida_bench') / (f.name + '.7z')
            out_7z.parent.mkdir(exist_ok=True)
            subprocess.run(
                ['7z', 'a', '-mx=9', '-bso0', '-bsp0', str(out_7z), str(f)],
                capture_output=True, timeout=180
            )
            if out_7z.exists():
                s_7z = out_7z.stat().st_size
        except Exception:
            s_7z = 0

        mejora = (1 - z_dict / z_plain) * 100 if z_plain > 0 else 0

        print(
            f"{f.name[:32]:<32} {orig:>11,} {z_plain:>11,} "
            f"{z_dict:>11,} {s_7z:>11,} {mejora:>7.1f}%"
        )

        total_orig += orig
        total_zstd += z_plain
        total_dict += z_dict
        total_7z += s_7z

    print("-" * 92)
    print(f"{'TOTAL':<32} {total_orig:>11,} {total_zstd:>11,} {total_dict:>11,} {total_7z:>11,}")
    print()
    if total_orig > 0:
        print(f"Ratio Zstd sin dict:   {total_zstd / total_orig * 100:>6.2f}%")
        print(f"Ratio Zstd + dict:     {total_dict / total_orig * 100:>6.2f}%")
        if total_7z > 0:
            print(f"Ratio 7z:              {total_7z / total_orig * 100:>6.2f}%")
        if total_zstd > 0:
            print(f"Mejora del diccionario sobre Zstd: {(1 - total_dict / total_zstd) * 100:>6.2f}%")