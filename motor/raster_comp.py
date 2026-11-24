import io
import json
import lzma
import zstandard as zstd
import struct
from pathlib import Path
import numpy as np
import tifffile
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION

FLAG_RASTER = 16

PRED_RAW = 0
PRED_DELTA_H = 1
PRED_DELTA_V = 2

def comprimir_tif(entrada, salida):
    print(f"Archivo: {Path(entrada).name}")
    try:
        with tifffile.TiffFile(entrada) as tif:
            arr = tif.asarray()
            photometric = tif.pages[0].photometric.name if tif.pages else 'MINISBLACK'
            extratags = []
            for tag in tif.pages[0].tags.values():
                if tag.code in (33550, 33922, 34735, 34736, 34737, 42112, 42113):
                    extratags.append((tag.code, tag.dtype, tag.count, tag.value, False))
    except Exception as e:
        print(f"Error leyendo TIFF: {e}")
        return

    total_pixeles = arr.size
    bytes_por_pixel = arr.dtype.itemsize
    total_orig = total_pixeles * bytes_por_pixel
    print(f"Análisis: {arr.shape} - Tipo: {arr.dtype} - {total_orig/1024/1024:.2f} MB")

    print("\nProbando predictores (Prueba rápida con Zstd)...")
    opciones = []
    opciones.append((PRED_RAW, arr.tobytes()))

    if arr.ndim >= 2:
        dh = np.zeros_like(arr)
        dh[:, 0, ...] = arr[:, 0, ...] 
        dh[:, 1:, ...] = arr[:, 1:, ...] - arr[:, :-1, ...]
        opciones.append((PRED_DELTA_H, dh.tobytes()))

        dv = np.zeros_like(arr)
        dv[0, ...] = arr[0, ...]
        dv[1:, ...] = arr[1:, ...] - arr[:-1, ...]
        opciones.append((PRED_DELTA_V, dv.tobytes()))

    resultados_prueba = []
    cctx_fast = zstd.ZstdCompressor(level=1)
    for pred, data_bytes in opciones:
        c_fast = cctx_fast.compress(data_bytes)
        resultados_prueba.append((pred, data_bytes, len(c_fast)))
        
    resultados_prueba.sort(key=lambda x: x[2])
    mejor_pred, mejor_data_bytes, _ = resultados_prueba[0]

    print("\nComprimiendo el ganador con LZMA...")
    mejor_cuerpo = lzma.compress(mejor_data_bytes, preset=6)
    crc_pixeles = crc32(arr.tobytes())

    metadata = {
        'shape': list(arr.shape),
        'dtype': str(arr.dtype),
        'predictor': mejor_pred,
        'photometric': photometric,
        'extratags': extratags
    }
    meta_json = json.dumps(metadata).encode('utf-8')
    cuerpo_final = struct.pack('<I', len(meta_json)) + meta_json + mejor_cuerpo

    cabecera = Cabecera(VERSION, 0, total_orig, FLAG_RASTER, crc_pixeles)
    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, cuerpo_final))

    tamaño_final = salida_path.stat().st_size
    nombres_pred = {0: "RAW", 1: "Delta-H", 2: "Delta-V"}
    print(f"\nMejor: {nombres_pred.get(mejor_pred, '?')}")
    print(f"Comprimido: {tamaño_final:,} bytes")
    print(f"Ratio: {tamaño_final / total_orig * 100:.2f}%")

def descomprimir_tif(entrada, salida):
    raw = Path(entrada).read_bytes()
    cabecera, contenido = desempaquetar(raw)
    
    meta_len = struct.unpack('<I', contenido[:4])[0]
    meta_json = contenido[4:4+meta_len].decode('utf-8')
    metadata = json.loads(meta_json)
    cuerpo = contenido[4+meta_len:]
    
    print("Descomprimiendo...")
    datos_bytes = lzma.decompress(cuerpo)
    
    shape = tuple(metadata['shape'])
    dtype = np.dtype(metadata['dtype'])
    predictor = metadata['predictor']
    photometric = metadata.get('photometric', 'MINISBLACK')

    arr = np.frombuffer(datos_bytes, dtype=dtype).reshape(shape)

    if predictor == PRED_DELTA_H:
        arr = np.cumsum(arr, axis=1, dtype=arr.dtype)
    elif predictor == PRED_DELTA_V:
        arr = np.cumsum(arr, axis=0, dtype=arr.dtype)

    if crc32(arr.tobytes()) != cabecera.crc32:
        raise ValueError("CRC de píxeles no coincide. El archivo se corrompió.")

    buf = io.BytesIO()
    extratags = [tuple(tag) for tag in metadata['extratags']]
    
    # Escribimos banda por banda
    with tifffile.TiffWriter(buf) as tif:
        if arr.ndim == 3:
            for i in range(arr.shape[-1]):
                if i == 0:
                    tif.write(arr[:,:,i], photometric='minisblack', compression='deflate', extratags=extratags)
                else:
                    tif.write(arr[:,:,i], photometric='minisblack', compression='deflate')
        else:
            tif.write(arr, photometric=photometric, compression='deflate', extratags=extratags)
    
    Path(salida).write_bytes(buf.getvalue())
    print(f"Recuperado: {len(buf.getvalue()):,} bytes")
    print("Verificación de píxeles: OK")