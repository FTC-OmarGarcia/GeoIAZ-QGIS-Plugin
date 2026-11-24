from pathlib import Path
import zstandard as zstd
import lzma
import json
import struct
import time
import numpy as np
import tifffile
from .formato import Cabecera, empaquetar, crc32, VERSION
from .ia_clasificador import predecir_codec
from .licencia import verificar_licencia  # <-- NUEVO: Importamos el validador

FLAG_MULTIARCHIVO = 1
FLAG_LZMA_PACK = 32
FLAG_COMMUNITY = 2048

UMBRAL_PROBAR_DICT = 10 * 1024 * 1024

EXTENSIONES_YA_COMPRIMIDAS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.laz', '.zip', '.7z', '.rar', '.gz', '.bz2', '.xz', '.lz', '.lzma',
    '.pdf', '.mp3', '.mp4', '.m4a', '.ogg', '.webm', '.mkv',
}

UMBRAL_PROPORCION_COMPRIMIDOS = 0.5

def _cargar_dict(ruta):
    if not ruta:
        return None
    p = Path(ruta)
    if not p.exists():
        return None
    return zstd.ZstdCompressionDict(p.read_bytes())

def _comprimir_zstd(blob, dict_data=None):
    kwargs = {
        'level': 22,
        'write_content_size': True,
        'threads': -1,
    }
    if dict_data is not None:
        kwargs['dict_data'] = dict_data
    cctx = zstd.ZstdCompressor(**kwargs)
    t0 = time.perf_counter()
    cuerpo = cctx.compress(blob)
    t1 = time.perf_counter()
    return cuerpo, (t1 - t0)

def _comprimir_lzma(blob):
    t0 = time.perf_counter()
    cuerpo = lzma.compress(blob, preset=6)
    t1 = time.perf_counter()
    return cuerpo, (t1 - t0)

def empaquetar_carpeta(carpeta, salida, dict_path=None, edition='pro'):
    carpeta = Path(carpeta)
    if not carpeta.is_dir():
        raise ValueError(f"{carpeta} no es una carpeta")

    archivos = sorted([f for f in carpeta.rglob('*') if f.is_file()])
    if not archivos:
        raise ValueError(f"No hay archivos en {carpeta}")

    print(f"Recolectando {len(archivos)} archivos...")

    indice = []
    partes = []
    offset = 0
    total = 0
    bytes_ya_comprimidos = 0
    aplicados_delta_shp = 0
    aplicados_delta_tif = 0

    for f in archivos:
        ruta_rel = str(f.relative_to(carpeta)).replace('\\', '/')
        try:
            datos = f.read_bytes()
        except Exception as e:
            print(f"  Saltando {ruta_rel}: {e}")
            continue

        if f.suffix.lower() in EXTENSIONES_YA_COMPRIMIDAS:
            bytes_ya_comprimidos += len(datos)

        delta = 0
        tif_meta = None

        if f.suffix.lower() == '.shp':
            from .shapefile_comp import _recorrer_shp
            cctx_rapido = zstd.ZstdCompressor(level=3)
            try:
                sin_delta = cctx_rapido.compress(datos)
                datos_delta = _recorrer_shp(datos, revertir=False)
                con_delta = cctx_rapido.compress(datos_delta)
                if len(con_delta) < len(sin_delta):
                    datos = datos_delta
                    delta = 1
                    aplicados_delta_shp += 1
            except Exception:
                pass  # nosec B110

        elif f.suffix.lower() in ('.tif', '.tiff'):
            try:
                with tifffile.TiffFile(f) as tif:
                    arr = tif.asarray()
                    photometric = tif.pages[0].photometric.name if tif.pages else 'MINISBLACK'
                    extratags = []
                    for tag in tif.pages[0].tags.values():
                        if tag.code in (33550, 33922, 34735, 34736, 34737, 42112, 42113):
                            extratags.append((tag.code, tag.dtype, tag.count, tag.value, False))
                
                opciones_tif = []
                opciones_tif.append((0, arr.tobytes())) # RAW
                
                if arr.ndim >= 2:
                    dh = np.zeros_like(arr)
                    dh[:, 0, ...] = arr[:, 0, ...]
                    dh[:, 1:, ...] = arr[:, 1:, ...] - arr[:, :-1, ...]
                    opciones_tif.append((1, dh.tobytes())) # Delta-H

                    dv = np.zeros_like(arr)
                    dv[0, ...] = arr[0, ...]
                    dv[1:, ...] = arr[1:, ...] - arr[:-1, ...]
                    opciones_tif.append((2, dv.tobytes())) # Delta-V

                cctx_fast = zstd.ZstdCompressor(level=1)
                resultados_tif = [(pred, data, len(cctx_fast.compress(data))) for pred, data in opciones_tif]
                resultados_tif.sort(key=lambda x: x[2])
                mejor_pred, mejor_data, _ = resultados_tif[0]
                
                datos = mejor_data
                tif_meta = {
                    'shape': list(arr.shape),
                    'dtype': str(arr.dtype),
                    'photometric': photometric,
                    'predictor': mejor_pred,
                    'extratags': extratags
                }
                aplicados_delta_tif += 1
                print(f"  {ruta_rel}: TIFF optimizado (Predictor {mejor_pred})")
            except Exception as e:
                print(f"  {ruta_rel}: no se pudo optimizar TIFF ({e}), se guarda crudo.")

        item_idx = {
            'ruta': ruta_rel,
            'tamaño': len(datos),
            'crc32': crc32(datos),
            'offset': offset,
            'delta': delta,
        }
        if tif_meta:
            item_idx['tif_meta'] = tif_meta

        indice.append(item_idx)
        partes.append(datos)
        offset += len(datos)
        total += len(datos)

    if not partes:
        raise ValueError("No se pudo leer ningún archivo")

    print(f"Delta aplicado a {aplicados_delta_shp} archivos .shp")
    print(f"TIFFs optimizados: {aplicados_delta_tif}")

    blob = b''.join(partes)
    proporcion_comprimida = bytes_ya_comprimidos / total if total > 0 else 0
    print(f"Total a comprimir: {total:,} bytes ({total/1024/1024:.2f} MB)")
    print(f"Proporción ya comprimida: {proporcion_comprimida * 100:.1f}%")

    dict_data = _cargar_dict(dict_path)
    id_dict = 0
    flags = FLAG_MULTIARCHIVO

    if edition == 'community':
        flags |= FLAG_COMMUNITY

    # ============================================================
    # VERIFICAR SI ES PRO (El candado)
    # ============================================================
    es_pro, _ = verificar_licencia()

    estrategia_dict = ""

    if proporcion_comprimida >= UMBRAL_PROPORCION_COMPRIMIDOS:
        print("Paquete con muchos archivos comprimidos: usando LZMA...")
        cuerpo, t = _comprimir_lzma(blob)
        flags |= FLAG_LZMA_PACK
        estrategia_dict = f"LZMA preset 6 [{t:.1f}s]"
    else:
        if es_pro and edition != 'community':
            # --- LÓGICA PRO: Usa la IA ---
            primer_archivo = archivos[0]
            prediccion_ia = predecir_codec(str(primer_archivo), blob[:100_000])
            
            if prediccion_ia == 'lzma':
                print("IA eligió LZMA para el paquete...")
                cuerpo, t = _comprimir_lzma(blob)
                flags |= FLAG_LZMA_PACK
                estrategia_dict = f"🤖 LZMA preset 6 [{t:.1f}s] (Elegido por IA)"
            else:
                print("IA eligió Zstd para el paquete...")
                cuerpo, t = _comprimir_zstd(blob, dict_data)
                id_dict = 1 if dict_data else 0
                estrategia_dict = f"🤖 Zstd [{t:.1f}s] (Elegido por IA)"
        else:
            # --- LÓGICA COMMUNITY: Sin IA ---
            # Para packs grandes, LZMA suele dar mejor ratio. Lo usamos por defecto.
            print("Modo Community: usando LZMA para el paquete...")
            cuerpo, t = _comprimir_lzma(blob)
            flags |= FLAG_LZMA_PACK
            estrategia_dict = f"LZMA preset 6 [{t:.1f}s] (Community)"

    indice_json = json.dumps(
        {'archivos': indice, 'total': total},
        ensure_ascii=False
    ).encode('utf-8')

    cabecera = Cabecera(VERSION, id_dict, total, flags, crc32(blob))
    cabecera_bytes = empaquetar(cabecera, b'')

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)

    with open(salida_path, 'wb') as out:
        out.write(cabecera_bytes)
        out.write(struct.pack('<I', len(indice_json)))
        out.write(indice_json)
        out.write(cuerpo)

    tamaño_final = salida_path.stat().st_size

    print()
    print(f"Carpeta:     {carpeta}")
    print(f"Archivos:    {len(indice)}")
    print(f"Original:    {total:,} bytes ({total/1024/1024:.2f} MB)")
    print(f"Comprimido:  {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio:       {tamaño_final / total * 100:.2f}%")
    print(f"Codec:       {estrategia_dict}")
    if edition == 'community':
        print(f"Edición:     Community (uso no comercial)")