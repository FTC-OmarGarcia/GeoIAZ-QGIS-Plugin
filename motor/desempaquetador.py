from pathlib import Path
import zstandard as zstd
import lzma
import json
import struct
import numpy as np
import tifffile
import io
from .formato import desempaquetar, crc32
from .empaquetador import FLAG_MULTIARCHIVO, FLAG_LZMA_PACK

def desempaquetar_carpeta(entrada, salida, dict_path=None):
    raw = Path(entrada).read_bytes()
    cabecera, resto = desempaquetar(raw)

    if not (cabecera.flags & FLAG_MULTIARCHIVO):
        raise ValueError(
            "Este archivo no es un paquete multi-archivo. "
            "Usá 'decomp' para archivos individuales."
        )

    if len(resto) < 4:
        raise ValueError("Archivo corrupto: falta el índice")

    tamaño_indice = struct.unpack('<I', resto[:4])[0]
    indice_json = resto[4:4 + tamaño_indice]
    cuerpo = resto[4 + tamaño_indice:]
    indice = json.loads(indice_json.decode('utf-8'))

    if cabecera.flags & FLAG_LZMA_PACK:
        print("Descomprimiendo con LZMA...")
        blob = lzma.decompress(cuerpo)
    else:
        kwargs = {}
        if cabecera.id_dict != 0:
            if not dict_path:
                raise ValueError(
                    "El paquete fue comprimido con diccionario. "
                    "Pasá --dict con el mismo diccionario usado al empaquetar."
                )
            dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())
            kwargs['dict_data'] = dict_data
        print("Descomprimiendo con Zstd...")
        dctx = zstd.ZstdDecompressor(**kwargs)
        blob = dctx.decompress(cuerpo, max_output_size=cabecera.tamaño)

    if crc32(blob) != cabecera.crc32:
        raise ValueError("CRC global no coincide. Archivo corrupto.")
    print("CRC global: OK")

    salida_path = Path(salida)
    salida_path.mkdir(parents=True, exist_ok=True)

    from .shapefile_comp import _recorrer_shp

    errores = 0
    revertidos_shp = 0
    reconstruidos_tif = 0

    for info in indice['archivos']:
        offset = info['offset']
        tamaño = info['tamaño']
        delta = info.get('delta', 0)
        tif_meta = info.get('tif_meta')
        datos = blob[offset:offset + tamaño]

        if crc32(datos) != info['crc32']:
            print(f"ERROR CRC en {info['ruta']}")
            errores += 1
            continue

        if delta:
            try:
                datos = _recorrer_shp(datos, revertir=True)
                revertidos_shp += 1
            except Exception as e:
                print(f"ERROR revirtiendo delta en {info['ruta']}: {e}")
                errores += 1
                continue

        if tif_meta:
            try:
                arr = np.frombuffer(datos, dtype=tif_meta['dtype']).reshape(tif_meta['shape'])
                pred = tif_meta['predictor']
                
                if pred == 1:
                    arr = np.cumsum(arr, axis=1, dtype=arr.dtype)
                elif pred == 2:
                    arr = np.cumsum(arr, axis=0, dtype=arr.dtype)
                    
                buf = io.BytesIO()
                extratags = [tuple(tag) for tag in tif_meta['extratags']]
                
                # Escribimos el TIFF banda por banda (página por página)
                with tifffile.TiffWriter(buf) as tif:
                    if arr.ndim == 3:
                        for i in range(arr.shape[-1]):
                            if i == 0:
                                tif.write(arr[:,:,i], photometric='minisblack', compression='deflate', extratags=extratags)
                            else:
                                tif.write(arr[:,:,i], photometric='minisblack', compression='deflate')
                    else:
                        tif.write(arr, photometric=tif_meta['photometric'], compression='deflate', extratags=extratags)

                datos = buf.getvalue()
                reconstruidos_tif += 1
            except Exception as e:
                print(f"ERROR reconstruyendo TIFF en {info['ruta']}: {e}")
                errores += 1
                continue

        ruta_destino = salida_path / info['ruta']
        ruta_destino.parent.mkdir(parents=True, exist_ok=True)
        ruta_destino.write_bytes(datos)

    print()
    print(f"Recuperados: {len(indice['archivos']) - errores} archivos")
    print(f"Delta revertido: {revertidos_shp} archivos .shp")
    print(f"TIFFs reconstruidos: {reconstruidos_tif}")
    print(f"Total: {len(blob):,} bytes ({len(blob)/1024/1024:.2f} MB)")
    print(f"Destino: {salida_path}")
    if errores:
        print(f"ADVERTENCIA: {errores} archivos con error")
    else:
        print("Verificación: TODOS los CRC OK")