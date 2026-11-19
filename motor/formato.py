import struct
import zlib
from dataclasses import dataclass

MAGIC = b'GIAZ'
VERSION = 2
HEADER_SIZE = 21
HEADER_FMT = '<4sBHQHI'

@dataclass
class Cabecera:
    version: int
    id_dict: int
    tamaño: int
    flags: int
    crc32: int

def empaquetar(cabecera, cuerpo):
    return struct.pack(
        HEADER_FMT,
        MAGIC, cabecera.version, cabecera.id_dict,
        cabecera.tamaño, cabecera.flags, cabecera.crc32
    ) + cuerpo

def desempaquetar(raw):
    if len(raw) < HEADER_SIZE:
        raise ValueError("Archivo demasiado pequeño")
    magic, version, id_dict, tamaño, flags, crc = struct.unpack(
        HEADER_FMT, raw[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise ValueError("No es un archivo GeoIAZ (.giz)")
    return Cabecera(version, id_dict, tamaño, flags, crc), raw[HEADER_SIZE:]

def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF