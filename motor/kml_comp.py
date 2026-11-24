from pathlib import Path
import lzma
import zstandard as zstd
import struct
import json
import io
import zipfile
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION

FLAG_KML = 128
FLAG_KMZ = 256

CODEC_ZSTD = 0
CODEC_LZMA = 1
CODEC_ZSTD_DICT = 2


def _nombre_codec(c):
    return {
        CODEC_ZSTD: "Zstd",
        CODEC_LZMA: "LZMA extreme",
        CODEC_ZSTD_DICT: "Zstd + diccionario",
    }.get(c, "?")


# ======================================================================
#  KML — archivo de texto (XML)
# ======================================================================

def comprimir_kml(entrada, salida, dict_path=None):
    datos = Path(entrada).read_bytes()
    total_orig = len(datos)

    print(f"Archivo: {Path(entrada).name}")
    print(f"Tamaño:  {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print()
    print("Probando estrategias...")

    opciones = []

    print("  LZMA extreme...", end=" ", flush=True)
    c_lzma = lzma.compress(datos, preset=9 | lzma.PRESET_EXTREME)
    print(f"{len(c_lzma):,} bytes")
    opciones.append((CODEC_LZMA, c_lzma))

    if dict_path and Path(dict_path).exists():
        print("  Zstd + diccionario...", end=" ", flush=True)
        dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())
        cctx = zstd.ZstdCompressor(level=22, dict_data=dict_data, threads=-1)
        c_dict = cctx.compress(datos)
        print(f"{len(c_dict):,} bytes")
        opciones.append((CODEC_ZSTD_DICT, c_dict))

    print("  Zstd sin diccionario...", end=" ", flush=True)
    cctx = zstd.ZstdCompressor(level=22, threads=-1, write_content_size=True)
    c_zstd = cctx.compress(datos)
    print(f"{len(c_zstd):,} bytes")
    opciones.append((CODEC_ZSTD, c_zstd))

    opciones.sort(key=lambda x: len(x[1]))
    codec, cuerpo = opciones[0]

    id_dict = 1 if codec == CODEC_ZSTD_DICT else 0
    metadata = struct.pack('<B', codec)
    cabecera = Cabecera(VERSION, id_dict, total_orig, FLAG_KML, crc32(datos))

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, metadata + cuerpo))

    tamaño_final = salida_path.stat().st_size
    print()
    print(f"Mejor:       {_nombre_codec(codec)}")
    print(f"Original:    {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print(f"Comprimido:  {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio:       {tamaño_final / total_orig * 100:.2f}%")


def descomprimir_kml(entrada, salida, dict_path=None):
    raw = Path(entrada).read_bytes()
    cabecera, contenido = desempaquetar(raw)

    if not (cabecera.flags & FLAG_KML):
        raise ValueError("Este archivo no es un KML GeoIAZ")

    codec = struct.unpack('<B', contenido[:1])[0]
    cuerpo = contenido[1:]

    if codec == CODEC_LZMA:
        print("Descomprimiendo con LZMA...")
        datos = lzma.decompress(cuerpo)
    elif codec == CODEC_ZSTD_DICT:
        print("Descomprimiendo con Zstd + diccionario...")
        if not dict_path:
            raise ValueError("Se necesita --dict con el diccionario original")
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


# ======================================================================
#  KMZ — ZIP con KML + assets (imágenes, etc.)
# ======================================================================

def comprimir_kmz(entrada, salida):
    """KMZ = archivo ZIP. Extrae entradas, las recompone con LZMA extreme."""
    entrada_p = Path(entrada)
    total_orig = entrada_p.stat().st_size

    print(f"Archivo: {entrada_p.name}")
    print(f"Tamaño:  {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print()

    try:
        with zipfile.ZipFile(entrada, 'r') as z:
            entries = []
            for info in z.infolist():
                data = z.read(info.filename)
                entries.append({
                    'filename': info.filename,
                    'date_time': info.date_time,
                    'external_attr': info.external_attr,
                    'create_system': info.create_system,
                    'data': data,
                })
    except Exception as e:
        raise ValueError(f"No se pudo leer el KMZ como ZIP: {e}")

    print(f"Entradas dentro del KMZ: {len(entries)}")
    for e in entries:
        print(f"  {e['filename']:40s} {len(e['data']):>12,} bytes")

    # Serializar: metadata JSON + blob concatenado
    meta = []
    blob = bytearray()
    offset = 0
    for e in entries:
        meta.append({
            'filename': e['filename'],
            'date_time': list(e['date_time']),
            'external_attr': e['external_attr'],
            'create_system': e['create_system'],
            'size': len(e['data']),
            'offset': offset,
        })
        blob.extend(e['data'])
        offset += len(e['data'])

    meta_json = json.dumps(meta, ensure_ascii=False).encode('utf-8')
    contenido_plano = struct.pack('<I', len(meta_json)) + meta_json + bytes(blob)

    print()
    print("Comprimiendo contenido extraído con LZMA extreme...")
    cuerpo = lzma.compress(contenido_plano, preset=9 | lzma.PRESET_EXTREME)

    cabecera = Cabecera(VERSION, 0, len(contenido_plano),
                        FLAG_KMZ, crc32(contenido_plano))

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, cuerpo))

    tamaño_final = salida_path.stat().st_size
    print()
    print(f"Original:    {total_orig:,} bytes ({total_orig/1024/1024:.2f} MB)")
    print(f"Comprimido:  {tamaño_final:,} bytes ({tamaño_final/1024/1024:.2f} MB)")
    print(f"Ratio:       {tamaño_final / total_orig * 100:.2f}%")


def descomprimir_kmz(entrada, salida):
    raw = Path(entrada).read_bytes()
    cabecera, cuerpo = desempaquetar(raw)

    if not (cabecera.flags & FLAG_KMZ):
        raise ValueError("Este archivo no es un KMZ GeoIAZ")

    print("Descomprimiendo contenido con LZMA...")
    contenido_plano = lzma.decompress(cuerpo)

    if crc32(contenido_plano) != cabecera.crc32:
        raise ValueError("CRC no coincide. Archivo corrupto.")

    # Parsear
    tamaño_meta = struct.unpack('<I', contenido_plano[:4])[0]
    meta_json = contenido_plano[4:4 + tamaño_meta]
    blob = contenido_plano[4 + tamaño_meta:]
    meta = json.loads(meta_json.decode('utf-8'))

    print(f"Reconstruyendo KMZ con {len(meta)} entradas...")

    # Reconstruir ZIP byte a byte
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for m in meta:
            data = blob[m['offset']:m['offset'] + m['size']]
            info = zipfile.ZipInfo(
                filename=m['filename'],
                date_time=tuple(m['date_time'])
            )
            info.external_attr = m['external_attr']
            info.create_system = m['create_system']
            z.writestr(info, data)

    Path(salida).write_bytes(buf.getvalue())
    print(f"Recuperado:  {len(buf.getvalue()):,} bytes")
    print("Verificación: KMZ reconstruido")


# ======================================================================
#  Router automático
# ======================================================================

def comprimir_kml_auto(entrada, salida, dict_path=None):
    ext = Path(entrada).suffix.lower()
    if ext == '.kmz':
        comprimir_kmz(entrada, salida)
    else:
        comprimir_kml(entrada, salida, dict_path)


def descomprimir_kml_auto(entrada, salida, dict_path=None):
    raw = Path(entrada).read_bytes()
    cabecera, _ = desempaquetar(raw)
    if cabecera.flags & FLAG_KMZ:
        descomprimir_kmz(entrada, salida)
    else:
        descomprimir_kml(entrada, salida, dict_path)