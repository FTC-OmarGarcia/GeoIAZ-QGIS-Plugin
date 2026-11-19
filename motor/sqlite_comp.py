from pathlib import Path
import struct
import lzma
import zstandard as zstd
from collections import Counter
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION


TIPOS_PAGINA = {
    0x02: 'btree_interior_index',
    0x05: 'btree_interior_table',
    0x0A: 'btree_leaf_index',
    0x0D: 'btree_leaf_table',
}

FLAG_SQLITE = 2
FLAG_LZMA = 4


def _leer_page_size(datos):
    """Lee el tamaño de página del encabezado SQLite (bytes 16-17, big-endian)."""
    if len(datos) < 100 or datos[:16] != b'SQLite format 3\x00':
        raise ValueError("No es un archivo SQLite/GeoPackage válido")
    ps_raw = struct.unpack('>H', datos[16:18])[0]
    return 65536 if ps_raw == 1 else ps_raw


def _clasificar(datos, offset):
    """Devuelve el nombre del tipo de página según el primer byte."""
    tipo_byte = datos[offset]
    return TIPOS_PAGINA.get(tipo_byte, f'otro_0x{tipo_byte:02x}')


def _orden_preferido(tipo):
    """Número menor = se coloca primero al reordenar."""
    orden = {
        'btree_leaf_table': 0,
        'btree_interior_table': 1,
        'btree_leaf_index': 2,
        'btree_interior_index': 3,
    }
    return orden.get(tipo, 99)


def comprimir_sqlite(entrada, salida):
    """Comprime un GeoPackage/SQLite. Prueba Zstd y LZMA, elige el más chico."""
    datos = Path(entrada).read_bytes()
    page_size = _leer_page_size(datos)

    n_total = len(datos) // page_size
    tail = bytes(datos[n_total * page_size:])

    if n_total == 0:
        raise ValueError("El archivo es más chico que una página")

    # Recolectar y clasificar páginas
    paginas = []
    for i in range(n_total):
        offset = i * page_size
        pag = bytes(datos[offset:offset + page_size])
        tipo = _clasificar(datos, offset)
        paginas.append((i, tipo, pag))

    stats = Counter(t for _, t, _ in paginas)
    print("Tipos de página encontrados:")
    for t, n in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {t:28s}: {n:7d} páginas")

    # Reordenar y deduplicar
    paginas.sort(key=lambda x: (_orden_preferido(x[1]), x[0]))

    unicas = []
    hash_a_idx = {}
    indices = []
    orden_original = []

    for idx_orig, tipo, pag in paginas:
        h = hash(pag)
        j = hash_a_idx.get(h)
        if j is not None and unicas[j] == pag:
            indices.append(j)
        else:
            j = len(unicas)
            unicas.append(pag)
            hash_a_idx[h] = j
            indices.append(j)
        orden_original.append(idx_orig)

    n_unicas = len(unicas)
    dedup = n_total - n_unicas
    print(f"Páginas totales:  {n_total}")
    print(f"Páginas únicas:   {n_unicas}  (deduplicadas: {dedup})")

    # Serializar estructura + blob de páginas únicas
    cab = struct.pack('<III', n_total, page_size, n_unicas)
    idxs = struct.pack(f'<{n_total}I', *indices)
    orden = struct.pack(f'<{n_total}I', *orden_original)
    cola = struct.pack('<I', len(tail)) + tail
    blob_unicas = b''.join(unicas)
    contenido = cab + idxs + orden + cola + blob_unicas

    # Probar Zstd y LZMA
    print("Comprimiendo con Zstd nivel 22...")
    cctx = zstd.ZstdCompressor(level=22, threads=-1, write_content_size=True)
    cuerpo_zstd = cctx.compress(contenido)
    print(f"  Zstd: {len(cuerpo_zstd):,} bytes")

    print("Comprimiendo con LZMA nivel 9 extreme (puede tardar)...")
    cuerpo_lzma = lzma.compress(contenido, preset=9 | lzma.PRESET_EXTREME)
    print(f"  LZMA: {len(cuerpo_lzma):,} bytes")

    if len(cuerpo_lzma) < len(cuerpo_zstd):
        cuerpo = cuerpo_lzma
        flags = FLAG_SQLITE | FLAG_LZMA
        print("  -> Gana LZMA")
    else:
        cuerpo = cuerpo_zstd
        flags = FLAG_SQLITE
        print("  -> Gana Zstd")

    cabecera = Cabecera(VERSION, 0, len(datos), flags, crc32(datos))
    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, cuerpo))

    print()
    print(f"Original:    {len(datos):,} bytes ({len(datos)/1024/1024:.2f} MB)")
    print(f"Comprimido:  {len(cuerpo) + 20:,} bytes ({(len(cuerpo)+20)/1024/1024:.2f} MB)")
    print(f"Ratio:       {(len(cuerpo) + 20) / len(datos) * 100:.2f}%")


def descomprimir_sqlite(entrada, salida):
    """Recupera un GeoPackage/SQLite desde un .mxcp creado por comprimir_sqlite."""
    raw = Path(entrada).read_bytes()
    cabecera, cuerpo = desempaquetar(raw)

    if cabecera.flags & FLAG_LZMA:
        print("Descomprimiendo con LZMA...")
        contenido = lzma.decompress(cuerpo)
    else:
        print("Descomprimiendo con Zstd...")
        dctx = zstd.ZstdDecompressor()
        max_out = cabecera.tamaño * 2 + 200_000_000
        contenido = dctx.decompress(cuerpo, max_output_size=max_out)

    # Parsear
    n_total, page_size, n_unicas = struct.unpack('<III', contenido[:12])
    off = 12
    indices = struct.unpack(f'<{n_total}I', contenido[off:off + n_total * 4])
    off += n_total * 4
    orden_original = struct.unpack(f'<{n_total}I', contenido[off:off + n_total * 4])
    off += n_total * 4
    tail_len = struct.unpack('<I', contenido[off:off + 4])[0]
    off += 4
    tail = contenido[off:off + tail_len]
    off += tail_len
    blob_unicas = contenido[off:]

    # Reconstruir
    n_bytes = n_total * page_size
    out = bytearray(n_bytes)
    for slot in range(n_total):
        idx_pag_orig = orden_original[slot]
        idx_unica = indices[slot]
        pag = blob_unicas[idx_unica * page_size:(idx_unica + 1) * page_size]
        inicio = idx_pag_orig * page_size
        out[inicio:inicio + page_size] = pag

    datos = bytes(out) + tail

    if crc32(datos) != cabecera.crc32:
        raise ValueError("CRC no coincide. Archivo corrupto o incompleto.")

    Path(salida).write_bytes(datos)
    print(f"Recuperado:  {len(datos):,} bytes")
    print("Verificación CRC32: OK")