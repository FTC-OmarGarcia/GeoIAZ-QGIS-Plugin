from pathlib import Path
import lzma
import zstandard as zstd
import io
from .formato import Cabecera, empaquetar, desempaquetar, crc32, VERSION
from .ia_clasificador import predecir_codec

try:
    import pymupdf
    PYMUPDF_OK = True
except ImportError:
    PYMUPDF_OK = False

FLAG_PDF = 4096

def comprimir_pdf(entrada, salida):
    """Comprime un PDF descomprimiendo su estructura interna y aplicando LZMA."""
    entrada_path = Path(entrada)
    datos_orig = entrada_path.read_bytes()
    tamaño_orig = len(datos_orig)

    print(f"Archivo: {entrada_path.name}")
    print(f"Tamaño original: {tamaño_orig:,} bytes ({tamaño_orig/1024/1024:.2f} MB)")

    if not PYMUPDF_OK:
        print("PyMuPDF no está instalado. Comprimiendo PDF original.")
        datos_pdf = datos_orig
    else:
        print("Abriendo PDF y descomprimiendo estructura interna...")
        try:
            doc = pymupdf.open(entrada_path)
            buffer = io.BytesIO()
            
            # LA CLAVE: deflate=False guarda el PDF sin compresión interna
            # Esto expande el archivo, pero permite que LZMA lo aplaste después
            doc.save(buffer, garbage=4, deflate=False, clean=True)
            doc.close()
            
            datos_pdf = buffer.getvalue()
            print(f"PDF aplanado (tamaño temporal): {len(datos_pdf):,} bytes ({len(datos_pdf)/1024/1024:.2f} MB)")
        except Exception as e:
            print(f"Aviso: No se pudo aplanar internamente ({e}). Usando PDF original.")
            datos_pdf = datos_orig

    print("Comprimiendo con LZMA extreme...")
    cuerpo = lzma.compress(datos_pdf, preset=9 | lzma.PRESET_EXTREME)

    # Empaquetar en el contenedor .giz
    cabecera = Cabecera(VERSION, 0, tamaño_orig, FLAG_PDF, crc32(datos_orig))
    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_bytes(empaquetar(cabecera, cuerpo))

    tamaño_final = salida_path.stat().st_size
    print()
    print(f"Original:    {tamaño_orig:,} bytes")
    print(f"Comprimido:  {tamaño_final:,} bytes")
    print(f"Ratio:       {tamaño_final / tamaño_orig * 100:.2f}%")


def descomprimir_pdf(entrada, salida):
    """Recupera un PDF desde un archivo .giz."""
    raw = Path(entrada).read_bytes()
    cabecera, cuerpo = desempaquetar(raw)

    if not (cabecera.flags & FLAG_PDF):
        raise ValueError("Este archivo no es un PDF empaquetado con GeoIAZ")

    print("Descomprimiendo PDF...")
    
    # Descomprimir LZMA
    datos = lzma.decompress(cuerpo)

    if crc32(datos) != cabecera.crc32:
        raise ValueError("CRC no coincide. Archivo corrupto.")

    Path(salida).write_bytes(datos)
    print(f"Recuperado:  {len(datos):,} bytes")
    print("Verificación CRC32: OK")