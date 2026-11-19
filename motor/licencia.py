import json
import hmac
import hashlib
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Usamos exactamente la misma clave que tu generar_licencia.py
CLAVE_SECRETA = b"FTC_GeoIAZ_MasterKey_2026_Secret" 

def obtener_ruta_licencia():
    # 1. Intentar en AppData (Estándar profesional, siempre tiene permisos de escritura)
    appdata = os.getenv('APPDATA')
    if appdata:
        ruta_appdata = Path(appdata) / "GeoIAZ" / "licencia.json"
        if ruta_appdata.exists():
            return ruta_appdata

    # 2. Fallback: buscar al lado del ejecutable (para instalaciones viejas o portables)
    ruta_base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent.parent
    return ruta_base / "licencia.json"

def verificar_licencia():
    archivo_lic = obtener_ruta_licencia()

    if not archivo_lic.exists():
        return False, "No hay licencia. Modo Community activo."

    try:
        datos = json.loads(archivo_lic.read_text(encoding='utf-8'))

        empresa = datos.get('empresa', 'Freelance')
        id_factura = datos.get('id_factura', 'N/A')
        
        # --- ACÁ ESTÁ EL ARREGLO: Agregamos las barras verticales | ---
        mensaje = f"{datos['email']}|{empresa}|{datos['tipo']}|{id_factura}|{datos['vencimiento']}".encode('utf-8')
        
        firma_calculada = hmac.new(CLAVE_SECRETA, mensaje, hashlib.sha256).hexdigest()

        if firma_calculada != datos['firma']:
            return False, "Licencia inválida (firma alterada). Modo Community activo."

        fecha_venc = datetime.strptime(datos['vencimiento'], "%Y-%m-%d")
        hoy = datetime.now()

        if hoy <= fecha_venc:
            dias_restantes = (fecha_venc - hoy).days
            return True, f"Licencia {datos['tipo']} válida para {empresa}. Vence en {dias_restantes} días."

        fecha_limite_gracia = fecha_venc + timedelta(days=30)
        if hoy <= fecha_limite_gracia:
            dias_gracia = (fecha_limite_gracia - hoy).days
            return True, f"Licencia VENCIDA. Período de gracia activo. Le quedan {dias_gracia} días."

        return False, "Licencia vencida hace más de 30 días. Modo Community activo."

    except Exception as e:
        return False, f"Error leyendo licencia: {e}"