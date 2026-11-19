from pathlib import Path
import lzma
import zstandard as zstd
import struct
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION

FLAG_GML = 64

CODEC_ZSTD = 0
CODEC_LZMA = 1
CODEC_ZSTD_DICT = 2


def _nombre_codec(c):
    return {CODEC_ZSTD: "Zstd", CODEC_LZMA: "LZMA extreme",
            CODEC_ZSTD_DICT: "Zstd + diccionario"}.get(c, "?")


def comprimir_gml(entrada, salida, dict_path=None):
    datos = Path(entrada).read_bytes()
    total_orig = len(datos)

    print(f"Archivo: {Path(entrada).name}")
    print(f"Tamaño:  {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print()
    print("Probando estrategias...")

    opciones = []

    # 1. LZMA extreme
    print("  LZMA extreme...", end=" ", flush=True)
    c_lzma = lzma.compress(datos, preset=9 | lzma.PRESET_EXTREME)
    print(f"{len(c_lzma):,} bytes")
    opciones.append((CODEC_LZMA, c_lzma))

    # 2. Zstd con diccionario (si existe)
    if dict_path and Path(dict_path).exists():
        print("  Zstd + diccionario GML...", end=" ", flush=True)
        dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())
        cctx = zstd.ZstdCompressor(level=22, dict_data=dict_data, threads=-1)
        c_dict = cctx.compress(datos)
        print(f"{len(c_dict):,} bytes")
        opciones.append((CODEC_ZSTD_DICT, c_dict))

    # 3. Zstd sin diccionario
    print("  Zstd sin diccionario...", end=" ", flush=True)
    cctx = zstd.ZstdCompressor(level=22, threads=-1, write_content_size=True)
    c_zstd = cctx.compress(datos)
    print(f"{len(c_zstd):,} bytes")
    opciones.append((CODEC_ZSTD, c_zstd))

    # Elegir el mejor
    opciones.sort(key=lambda x: len(x[1]))
    codec, cuerpo = opciones[0]

    id_dict = 1 if codec == CODEC_ZSTD_DICT else 0
    flags = FLAG_GML

    metadata = struct.pack('<B', codec)
    cabecera = Cabecera(VERSION, id_dict, total_orig, flags, crc32(datos))

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, metadata + cuerpo))

    tamaño_final = salida_path.stat().st_size

    print()
    print(f"Mejor:       {_nombre_codec(codec)}")
    print(f"Original:    {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print(f"Comprimido:  {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio:       {tamaño_final / total_orig * 100:.2f}%")


def descomprimir_gml(entrada, salida, dict_path=None):
    raw = Path(entrada).read_bytes()
    cabecera, contenido = desempaquetar(raw)

    if not (cabecera.flags & FLAG_GML):
        raise ValueError("Este archivo no es un GML GeoIAZ")

    codec = struct.unpack('<B', contenido[:1])[0]
    cuerpo = contenido[1:]

    if codec == CODEC_LZMA:
        print("Descomprimiendo con LZMA...")
        datos = lzma.decompress(cuerpo)
    elif codec == CODEC_ZSTD_DICT:
        print("Descomprimiendo con Zstd + diccionario...")
        if not dict_path:
            raise ValueError("Se necesita --dict con el diccionario GML original")
        dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())
        dctx = zstd.ZstdDecompressor(dict_data=dict_data)
        datos = dctx.decompress(cuerpo, max_output_size=cabecera.tamaño)
    else:
        print("Descomprimiendo con Zstd...")
        dctx = zstd.ZstdDecompressor()
        datos = dctx.decompress(cuerpo, max_output_size=cabecera.tamaño)

    if crc32(datos) != cabecera.crc32:
        raise ValueError("CRC no coincide. Archivo corrupto.")

    Path(salida).write_bytes(datos)
    print(f"Recuperado:  {len(datos):,} bytes")
    print("Verificación CRC32: OK")