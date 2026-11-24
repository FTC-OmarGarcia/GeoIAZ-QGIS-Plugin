from pathlib import Path
import zstandard as zstd
from .formato import desempaquetar, crc32

def descomprimir(entrada, salida, dict_path=None):
    raw = Path(entrada).read_bytes()
    cabecera, cuerpo = desempaquetar(raw)
    
    kwargs = {}
    if cabecera.id_dict != 0:
        dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())
        kwargs['dict_data'] = dict_data
    
    dctx = zstd.ZstdDecompressor(**kwargs)
    datos = dctx.decompress(cuerpo, max_output_size=cabecera.tamaño)
    
    if crc32(datos) != cabecera.crc32:
        raise ValueError("Archivo corrupto: el CRC no coincide")
    
    Path(salida).write_bytes(datos)
    print(f"Recuperado:  {len(datos):,} bytes")
    print("Verificación CRC32: OK")