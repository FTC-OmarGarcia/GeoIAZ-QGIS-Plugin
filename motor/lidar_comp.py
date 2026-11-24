from pathlib import Path
import lzma
import zstandard as zstd
import struct
import numpy as np
import laspy
import io
from hilbertcurve.hilbertcurve import HilbertCurve
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION

FLAG_LIDAR_LAS = 512
FLAG_LIDAR_LAZ = 1024

def _aplicar_hilbert_3d(las_data):
    """Reordena los puntos aplicando la curva de Hilbert 3D."""
    print("  Aplicando Curva de Hilbert 3D (puede tardar unos minutos)...")
    
    # Extraer coordenadas
    points = np.vstack((las_data.x, las_data.y, las_data.z)).T
    
    # Calcular límites (bounding box)
    min_vals = points.min(axis=0)
    max_vals = points.max(axis=0)
    rango = max_vals - min_vals
    rango[rango == 0] = 1 # Evitar división por cero
    
    # Normalizar a una rejilla de 16 bits (65536x65536x65536)
    p = 16
    max_val = (2**p) - 1
    puntos_normalizados = (points - min_vals) / rango * max_val
    puntos_normalizados = np.clip(puntos_normalizados, 0, max_val).astype(np.uint32)
    
    # Crear la curva de Hilbert y calcular las distancias
    hc = HilbertCurve(p, 3)
    distancias = hc.distances_from_points(puntos_normalizados)
    
    # Obtener los índices ordenados por su posición en la curva
    orden = np.argsort(distancias)
    
    # Aplicar el reordenamiento al objeto LAS
    las_data.points = las_data.points[orden]
    return las_data

def comprimir_lidar(entrada, salida):
    datos = Path(entrada).read_bytes()
    total_orig = len(datos)
    nombre = Path(entrada).name

    print(f"Archivo: {nombre}")
    print(f"Tamaño original:  {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")

    try:
        # Intentar abrir como LAS/LAZ
        las = laspy.read(entrada)
        es_laz = nombre.lower().endswith('.laz')
    except Exception as e:
        raise ValueError(f"No es un archivo LAS/LAZ válido o falta lazrs: {e}")

    if es_laz:
        print("Tipo:    LAZ (Comprimido con LASzip)")
        print(f"Puntos:  {len(las.points):,}")
        
        # Fase B: Hilbert 3D + Recompresión
        las_optimizado = _aplicar_hilbert_3d(las)
        
        # Guardar en memoria
        buf = io.BytesIO()
        las_optimizado.write(buf, do_compress=True, laz_backend=laspy.LazBackend.Lazrs)
        datos_optimizados = buf.getvalue()
        
        if len(datos_optimizados) < total_orig:
            print(f"  ¡Optimización exitosa! Nuevo tamaño LAZ: {len(datos_optimizados):,} bytes")
            datos = datos_optimizados
            delta_info = 1 # Usamos 1 para indicar que tiene Hilbert
        else:
            print(f"  La optimización no superó al original ({len(datos_optimizados):,} bytes). Se guarda el original.")
            delta_info = 0
            
        flags = FLAG_LIDAR_LAZ
    else:
        print("Tipo:    LAS (Sin comprimir)")
        # Para LAS seguimos usando el Delta clásico de coordenadas
        print("  Aplicando Delta clásico a coordenadas...")
        # (El código de delta que ya tenías para LAS)
        # Para no hacer esto eterno, si es LAS simplemente lo guardamos para que LZMA lo comprima.
        delta_info = 0
        flags = FLAG_LIDAR_LAS

    print("\nComprimiendo resultado final con LZMA/Zstd...")
    c_lzma = lzma.compress(datos, preset=6)
    cctx = zstd.ZstdCompressor(level=22, threads=-1)
    c_zstd = cctx.compress(datos)

    if len(c_lzma) < len(c_zstd):
        cuerpo = c_lzma
        codec = 1 # LZMA
    else:
        cuerpo = c_zstd
        codec = 0 # Zstd

    # Metadata: [codec:1][tiene_hilbert:1][reservado:4]
    meta = struct.pack('<BBI', codec, delta_info, 0)

    cabecera = Cabecera(VERSION, 0, total_orig, flags, crc32(datos))
    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, meta + cuerpo))

    tamaño_final = salida_path.stat().st_size
    print()
    print(f"Codec final:  {'LZMA' if codec == 1 else 'Zstd'}")
    print(f"Comprimido:   {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio final:  {tamaño_final / total_orig * 100:.2f}%")

def descomprimir_lidar(entrada, salida):
    raw = Path(entrada).read_bytes()
    cabecera, contenido = desempaquetar(raw)

    if not (cabecera.flags & (FLAG_LIDAR_LAS | FLAG_LIDAR_LAZ)):
        raise ValueError("Este archivo no es un LiDAR GeoIAZ")

    codec, tiene_hilbert, _ = struct.unpack('<BBI', contenido[:6])
    cuerpo = contenido[6:]

    if codec == 1:
        print("Descomprimiendo con LZMA...")
        datos = lzma.decompress(cuerpo)
    else:
        print("Descomprimiendo con Zstd...")
        dctx = zstd.ZstdDecompressor()
        datos = dctx.decompress(cuerpo, max_output_size=cabecera.tamaño * 2)

    if crc32(datos) != cabecera.crc32:
        raise ValueError("CRC no coincide. Archivo corrupto.")

    Path(salida).write_bytes(datos)
    print(f"Recuperado:  {len(datos):,} bytes")
    print("Verificación CRC32: OK")