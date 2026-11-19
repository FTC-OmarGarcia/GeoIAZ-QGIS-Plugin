from pathlib import Path
import zstandard as zstd
import lzma
from .formato import Cabecera, empaquetar, crc32, VERSION
from .ia_clasificador import predecir_codec, _calcular_entropia
from .historial_ia import registrar_operacion
from .licencia import verificar_licencia  # <-- NUEVO: Importamos el validador

# Umbrales de tamaño (en bytes) para decidir el uso del diccionario
UMBRAL_PEQUEÑO = 512 * 1024        # 512 KB
UMBRAL_GRANDE = 2 * 1024 * 1024    # 2 MB


def _comprimir_zstd(datos, dict_data=None, nivel=19):
    kwargs = {'level': nivel}
    if dict_data is not None:
        kwargs['dict_data'] = dict_data
    cctx = zstd.ZstdCompressor(**kwargs)
    return cctx.compress(datos)

def _comprimir_lzma(datos):
    return lzma.compress(datos, preset=6)


def comprimir(entrada, salida, dict_path=None):
    datos = Path(entrada).read_bytes()
    tamaño = len(datos)

    # Cargar diccionario si existe
    dict_data = None
    if dict_path and Path(dict_path).exists():
        dict_data = zstd.ZstdCompressionDict(Path(dict_path).read_bytes())

    # ============================================================
    # VERIFICAR SI ES PRO (El candado)
    # ============================================================
    es_pro, _ = verificar_licencia()

    id_dict = 0
    cuerpo = None
    estrategia = ""

    # Si hay diccionario y el archivo es chico, priorizar Zstd + dict
    if dict_data is not None and tamaño < UMBRAL_PEQUEÑO:
        cuerpo = _comprimir_zstd(datos, dict_data)
        id_dict = 1
        estrategia = f"Zstd + diccionario (archivo chico)"
    else:
        if es_pro:
            # --- LÓGICA PRO: Usa la IA ---
            codec_elegido_ia = predecir_codec(entrada, datos[:100_000])
            if codec_elegido_ia == 'lzma':
                cuerpo = _comprimir_lzma(datos)
                estrategia = "🤖 LZMA (elegido por IA)"
            else:
                cuerpo = _comprimir_zstd(datos)
                estrategia = "🤖 Zstd (elegido por IA)"

            if UMBRAL_PEQUEÑO <= tamaño < UMBRAL_GRANDE:
                cuerpo_zstd = _comprimir_zstd(datos)
                cuerpo_lzma = _comprimir_lzma(datos)
                if len(cuerpo_lzma) < len(cuerpo_zstd):
                    cuerpo = cuerpo_lzma
                    estrategia = f"🤖 LZMA (IA sugirió {codec_elegido_ia}, pero LZMA ganó al probar)"
                else:
                    cuerpo = cuerpo_zstd
                    estrategia = f"🤖 Zstd (IA sugirió {codec_elegido_ia}, pero Zstd ganó al probar)"
            
            # Registrar en el historial de IA solo si es PRO
            ratio = (len(cuerpo) + 21) / tamaño
            extension = Path(entrada).suffix.lower()
            entropia = _calcular_entropia(datos[:100_000])
            registrar_operacion(extension, tamaño, entropia, codec_elegido_ia, ratio)
        else:
            # --- LÓGICA COMMUNITY: Sin IA ---
            if UMBRAL_PEQUEÑO <= tamaño < UMBRAL_GRANDE:
                cuerpo_zstd = _comprimir_zstd(datos)
                cuerpo_lzma = _comprimir_lzma(datos)
                if len(cuerpo_lzma) < len(cuerpo_zstd):
                    cuerpo = cuerpo_lzma
                    estrategia = "LZMA (prueba básica sin IA)"
                else:
                    cuerpo = cuerpo_zstd
                    estrategia = "Zstd (prueba básica sin IA)"
            else:
                # Para archivos muy grandes en Community, usamos Zstd por defecto (es más rápido)
                cuerpo = _comprimir_zstd(datos)
                estrategia = "Zstd (por defecto sin IA)"

    salida_path = Path(salida)
    salida_path.parent.mkdir(parents=True, exist_ok=True)

    cabecera = Cabecera(VERSION, id_dict, tamaño, 0, crc32(datos))
    salida_path.write_bytes(empaquetar(cabecera, cuerpo))

    print(f"Original:    {tamaño:,} bytes")
    print(f"Comprimido:  {len(cuerpo) + 21:,} bytes")
    print(f"Ratio:       {(len(cuerpo) + 21) / tamaño * 100:.2f}%")
    print(f"Estrategia:  {estrategia}")