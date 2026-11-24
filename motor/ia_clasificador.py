# Archivo falso para el plugin de QGIS. 
# Evita que QGIS intente cargar joblib o pandas.

def predecir_codec(ruta_archivo, datos=None):
    return 'zstd'

def _calcular_entropia(datos):
    return 0.0