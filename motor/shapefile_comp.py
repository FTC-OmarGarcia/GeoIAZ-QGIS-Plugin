from pathlib import Path
import lzma
import zstandard as zstd
import json
import struct
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION
from .ia_clasificador import predecir_codec

FLAG_SHAPEFILE = 8
EXTENSIONES_SHP = [
    '.shp', '.shx', '.dbf', '.prj', '.cpg',
    '.sbn', '.sbx', '.qix', '.fix',
]


# ----------------------------------------------------------------------
#  Utilidades de bit-pattern (para delta exacto sin perder precisión)
# ----------------------------------------------------------------------

def _bits(x):
    """Convierte un double al entero sin signo de 64 bits de su representación IEEE 754."""
    return struct.unpack('<Q', struct.pack('<d', x))[0]


def _debits(i):
    """Vuelve el entero sin signo a double."""
    return struct.unpack('<d', struct.pack('<Q', i & 0xFFFFFFFFFFFFFFFF))[0]


# ----------------------------------------------------------------------
#  Delta encoding: aplicación y reversión
# ----------------------------------------------------------------------

def _aplicar_delta_puntos(blob, offsets, num_points):
    """Aplica delta de bit patterns a los (X,Y) dados por los offsets."""
    prev_x = None
    prev_y = None
    for i in range(num_points):
        off = offsets[i]
        x = struct.unpack('<Q', blob[off:off + 8])[0]
        y = struct.unpack('<Q', blob[off + 8:off + 16])[0]
        if prev_x is None:
            nx, ny = x, y
        else:
            nx = (x - prev_x) & 0xFFFFFFFFFFFFFFFF
            ny = (y - prev_y) & 0xFFFFFFFFFFFFFFFF
        struct.pack_into('<QQ', blob, off, nx, ny)
        prev_x, prev_y = x, y


def _revertir_delta_puntos(blob, offsets, num_points):
    """Revierte el delta: reconstruye las coordenadas originales."""
    prev_x = None
    prev_y = None
    for i in range(num_points):
        off = offsets[i]
        dx = struct.unpack('<Q', blob[off:off + 8])[0]
        dy = struct.unpack('<Q', blob[off + 8:off + 16])[0]
        if prev_x is None:
            x, y = dx, dy
        else:
            x = (prev_x + dx) & 0xFFFFFFFFFFFFFFFF
            y = (prev_y + dy) & 0xFFFFFFFFFFFFFFFF
        struct.pack_into('<QQ', blob, off, x, y)
        prev_x, prev_y = x, y


def _recorrer_shp(datos, revertir):
    """Recorre un .shp aplicando (o revirtiendo) delta en las coordenadas."""
    blob = bytearray(datos)
    n = len(datos)

    # Comprobación de firma: los .shp empiezan con file code 9994 big endian
    if n < 100 or datos[0:4] != b'\x00\x00\x27\x0a':
        # No es un shapefile válido, devolvemos tal cual
        return bytes(blob)

    pos = 100  # saltar header

    while pos + 8 <= n:
        # Record header: [número:4 BE][longitud:4 BE en words de 16 bits]
        try:
            rec_len_words = struct.unpack('>I', datos[pos + 4:pos + 8])[0]
        except Exception:
            break  # nosec B112
        rec_len_bytes = rec_len_words * 2
        content_start = pos + 8

        if content_start + 4 > n:
            break

        try:
            shape_type = struct.unpack('<I', datos[content_start:content_start + 4])[0]

            # PolyLine (3), Polygon (5) + variantes Z/M
            if shape_type in (3, 5, 13, 15, 23, 25):
                num_parts, num_points = struct.unpack(
                    '<II', datos[content_start + 36:content_start + 44]
                )
                points_start = content_start + 44 + num_parts * 4
                offsets = [points_start + i * 16 for i in range(num_points)]
                if revertir:
                    _revertir_delta_puntos(blob, offsets, num_points)
                else:
                    _aplicar_delta_puntos(blob, offsets, num_points)

            # MultiPoint (8) + variantes Z/M
            elif shape_type in (8, 18, 28):
                num_points = struct.unpack(
                    '<I', datos[content_start + 36:content_start + 40]
                )[0]
                points_start = content_start + 40
                offsets = [points_start + i * 16 for i in range(num_points)]
                if revertir:
                    _revertir_delta_puntos(blob, offsets, num_points)
                else:
                    _aplicar_delta_puntos(blob, offsets, num_points)
        except Exception:
            # Si un registro falla, seguimos con el siguiente
            pass  # nosec B110

        pos += 8 + rec_len_bytes

    return bytes(blob)


# ----------------------------------------------------------------------
#  Compresión / descompresión de bytes
# ----------------------------------------------------------------------

def _comprimir_bytes(datos):
    """Prueba Zstd nivel 22 y LZMA extreme. Devuelve (cuerpo, codec)."""
    cctx = zstd.ZstdCompressor(level=22, threads=-1, write_content_size=True)
    cuerpo_zstd = cctx.compress(datos)
    cuerpo_lzma = lzma.compress(datos, preset=9 | lzma.PRESET_EXTREME)
    if len(cuerpo_lzma) < len(cuerpo_zstd):
        return cuerpo_lzma, 1
    return cuerpo_zstd, 0


def _descomprimir_bytes(cuerpo, codec):
    if codec == 1:
        return lzma.decompress(cuerpo)
    dctx = zstd.ZstdDecompressor()
    return dctx.decompress(cuerpo, max_output_size=2 * 1024 * 1024 * 1024)


def _comprimir_archivo_con_mejor_estrategia(nombre, datos, ruta_completa):
    """Usa la IA para decidir el codec en lugar de probar ambos."""
    es_shp = nombre.lower().endswith('.shp')

    if not es_shp:
        # Para archivos que no son .shp (como .dbf), es tan rápido probar que mantenemos la prueba
        cuerpo, codec = _comprimir_bytes(datos)
        return cuerpo, codec, 0

    # Para el .shp, usamos la IA
    codec_ia = predecir_codec(ruta_completa, datos[:100_000])

    if codec_ia == 'lzma':
        cuerpo = lzma.compress(datos, preset=9 | lzma.PRESET_EXTREME)
        return cuerpo, 1, 0  # codec 1 = LZMA
    else:
        cctx = zstd.ZstdCompressor(level=22, threads=-1, write_content_size=True)
        cuerpo = cctx.compress(datos)
        return cuerpo, 0, 0  # codec 0 = Zstd


def _descomprimir_archivo(cuerpo, codec, delta):
    datos = _descomprimir_bytes(cuerpo, codec)
    if delta:
        datos = _recorrer_shp(datos, revertir=True)
    return datos


# ----------------------------------------------------------------------
#  Comandos principales
# ----------------------------------------------------------------------

def comprimir_shapefile(entrada_shp, salida):
    entrada = Path(entrada_shp)
    if not entrada.exists():
        raise ValueError(f"No existe: {entrada}")
    if entrada.suffix.lower() != '.shp':
        raise ValueError("El archivo de entrada debe ser .shp")

    base = entrada.with_suffix('')
    archivos = []
    for ext in EXTENSIONES_SHP:
        p = base.with_suffix(ext)
        if p.exists():
            archivos.append((p.name, p.read_bytes(), str(p)))

    if not archivos:
        raise ValueError("No se encontraron archivos del Shapefile")

    print(f"Shapefile: {entrada.name}")
    print("Archivos del conjunto:")
    for nombre, datos, _ in archivos:
        print(f"  {nombre:40s} {len(datos):>14,} bytes")

    print()
    print("Comprimiendo cada archivo (usando IA para .shp)...")
    indice = []
    cuerpos = []
    total_orig = 0

    for nombre, datos, ruta_completa in archivos:
        cuerpo, codec, delta = _comprimir_archivo_con_mejor_estrategia(nombre, datos, ruta_completa)
        nombre_codec = "LZMA" if codec == 1 else "Zstd"
        extra = " + delta" if delta else ""
        print(f"  {nombre:40s} {len(datos):>14,} -> {len(cuerpo):>14,} ({nombre_codec}{extra})")
        indice.append({
            'nombre': nombre,
            'tamaño_orig': len(datos),
            'tamaño_comp': len(cuerpo),
            'codec': codec,
            'delta': delta,
            'crc32': crc32(datos),
        })
        cuerpos.append(cuerpo)
        total_orig += len(datos)

    indice_json = json.dumps({'archivos': indice}, ensure_ascii=False).encode('utf-8')
    contenido = struct.pack('<I', len(indice_json)) + indice_json + b''.join(cuerpos)

    blob_orig = b''.join(d for _, d, _ in archivos)
    cabecera = Cabecera(VERSION, 0, len(blob_orig), FLAG_SHAPEFILE, crc32(blob_orig))

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, contenido))

    tamaño_final = salida_path.stat().st_size
    print()
    print(f"Original:    {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print(f"Comprimido:  {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio:       {tamaño_final / total_orig * 100:.2f}%")


def descomprimir_shapefile(entrada, salida):
    raw = Path(entrada).read_bytes()
    cabecera, contenido = desempaquetar(raw)

    if not (cabecera.flags & FLAG_SHAPEFILE):
        raise ValueError("Este archivo no es un Shapefile empaquetado")

    tamaño_indice = struct.unpack('<I', contenido[:4])[0]
    indice_json = contenido[4:4 + tamaño_indice]
    resto = contenido[4 + tamaño_indice:]
    indice = json.loads(indice_json.decode('utf-8'))['archivos']

    salida_path = Path(salida)
    if salida_path.suffix.lower() == '.shp':
        carpeta_destino = salida_path.parent
    else:
        carpeta_destino = salida_path
    carpeta_destino.mkdir(parents=True, exist_ok=True)

    offset = 0
    errores = 0
    for info in indice:
        nombre = info['nombre']
        tamaño = info['tamaño_comp']
        codec = info['codec']
        delta = info.get('delta', 0)
        cuerpo = resto[offset:offset + tamaño]
        offset += tamaño
        try:
            datos = _descomprimir_archivo(cuerpo, codec, delta)
        except Exception as e:
            print(f"  ERROR descomprimiendo {nombre}: {e}")
            errores += 1
            continue
        if crc32(datos) != info['crc32']:
            print(f"  ERROR CRC en {nombre}")
            errores += 1
            continue
        destino = carpeta_destino / nombre
        destino.write_bytes(datos)
        extra = " (delta revertido)" if delta else ""
        print(f"  {nombre:40s} {len(datos):>14,} bytes  OK{extra}")

    print()
    print(f"Recuperados: {len(indice) - errores} archivos")
    print(f"Destino:     {carpeta_destino}")
    if errores:
        print(f"ADVERTENCIA: {errores} archivos con error")
    else:
        print("Verificación: TODOS los CRC OK")