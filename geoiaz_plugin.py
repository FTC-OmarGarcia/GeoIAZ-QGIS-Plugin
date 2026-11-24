import os
import sys
import json
import struct
import tempfile
import shutil
import lzma
import subprocess
import zipfile
import zstandard as zstd

from qgis.core import (QgsApplication, QgsTask, QgsMessageLog, Qgis, QgsProject, 
                       QgsVectorLayer, QgsRasterLayer, QgsMapLayerType)
try:
    from qgis.core import QgsPointCloudLayer
    TIENE_NUBES_PUNTOS = True
except ImportError:
    TIENE_NUBES_PUNTOS = False

from qgis.PyQt.QtWidgets import (QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, 
                                 QTableWidgetItem, QCheckBox, QPushButton, QHeaderView, QMessageBox, QWidget, QFileDialog)
from qgis.PyQt.QtCore import Qt, QEvent, QObject
from qgis.PyQt.QtGui import QIcon

EXTENSIONES_IGNORADAS = {
    '.freelist', '.gdbtablx', '.gdbindexes', '.spx', '.atx', '.shx', '.dbf', '.prj', 
    '.cpg', '.sbn', '.sbx', '.xml', '.lock', '.db', '.sqlite-wal', '.sqlite-shm'
}

class GeoIAZFiltroGlobal(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.DragEnter, QEvent.DragMove):
            if event.mimeData().hasUrls():
                for url in event.mimeData().urls():
                    if url.toLocalFile().lower().endswith('.giz'):
                        event.acceptProposedAction()
                        return True 
        elif event.type() == QEvent.Drop:
            if event.mimeData().hasUrls():
                for url in event.mimeData().urls():
                    ruta = url.toLocalFile()
                    if ruta.lower().endswith('.giz'):
                        event.acceptProposedAction()
                        self.plugin.procesar_giz(ruta)
                        return True 
        return False

class ExtraerGizTask(QgsTask):
    def __init__(self, ruta_giz, ruta_temp):
        super().__init__("Extrayendo archivos de GeoIAZ...", QgsTask.CanCancel)
        self.ruta_giz = ruta_giz
        self.ruta_temp = ruta_temp
        self.archivos_extraidos = []
        self.error_msg = ""
        self.gdb_encontradas = set()

    def run(self):
        try:
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            if plugin_dir not in sys.path:
                sys.path.insert(0, plugin_dir)
                
            try:
                from motor.shapefile_comp import _recorrer_shp
            except Exception as e:
                self.error_msg = f"Error importando el motor: {str(e)}"
                return False

            with open(self.ruta_giz, 'rb') as f:
                raw = f.read()

            MAGIC = b'GIAZ'
            HEADER_FMT = '<4sBHQHI'
            HEADER_SIZE = 21
            if len(raw) < HEADER_SIZE:
                self.error_msg = "Archivo demasiado pequeño"
                return False
                
            magic, version, id_dict, tamaño, flags, crc = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
            if magic != MAGIC:
                self.error_msg = "No es un archivo GeoIAZ (.giz) válido"
                return False

            resto = raw[HEADER_SIZE:]
            
            if len(resto) < 4:
                self.error_msg = "Archivo corrupto: falta el índice"
                return False
                
            tamaño_indice = struct.unpack('<I', resto[:4])[0]
            indice_json = resto[4:4 + tamaño_indice]
            cuerpo = resto[4 + tamaño_indice:]
            indice = json.loads(indice_json.decode('utf-8'))

            FLAG_LZMA_PACK = 32
            if flags & FLAG_LZMA_PACK:
                blob = lzma.decompress(cuerpo)
            else:
                dctx = zstd.ZstdDecompressor()
                blob = dctx.decompress(cuerpo, max_output_size=tamaño)

            archivos_info = sorted(indice['archivos'], key=lambda x: x['tamaño'])
            total = len(archivos_info)

            for i, info in enumerate(archivos_info):
                if self.isCanceled():
                    return False
                
                offset = info['offset']
                tama = info['tamaño']
                datos = blob[offset:offset + tama]

                if info.get('delta', 0) == 1:
                    try:
                        datos = _recorrer_shp(datos, revertir=True)
                    except Exception as e:
                        self.error_msg = f"Error revirtiendo delta en {info['ruta']}: {str(e)}"
                        return False

                ruta_destino = os.path.join(self.ruta_temp, info['ruta'])
                os.makedirs(os.path.dirname(ruta_destino), exist_ok=True)
                
                with open(ruta_destino, 'wb') as f_out:
                    f_out.write(datos)

                ext = os.path.splitext(ruta_destino)[1].lower()
                nombre_base = os.path.basename(ruta_destino)
                
                if '.gdb' in ruta_destino.lower():
                    idx_gdb = ruta_destino.lower().find('.gdb')
                    ruta_gdb = ruta_destino[:idx_gdb + 4]
                    self.gdb_encontradas.add(ruta_gdb)
                elif ext not in EXTENSIONES_IGNORADAS:
                    self.archivos_extraidos.append({
                        'ruta': ruta_destino,
                        'nombre': nombre_base,
                        'tamaño': tama,
                        'es_gdb': False
                    })
                
                self.setProgress((i + 1) / total * 100)

            for ruta_gdb in self.gdb_encontradas:
                self.archivos_extraidos.append({
                    'ruta': ruta_gdb,
                    'nombre': os.path.basename(ruta_gdb),
                    'tamaño': 0,
                    'es_gdb': True
                })

            return True
        except Exception as e:
            self.error_msg = str(e)
            QgsMessageLog.logMessage(f"Error extrayendo GIZ: {e}", "GeoIAZ", Qgis.Critical)
            return False

# --- TAREA DE COMPRESIÓN MULTIPLATAFORMA ---
class ComprimirGizTask(QgsTask):
    def __init__(self, ruta_salida, archivos_a_comprimir):
        super().__init__("Comprimiendo con GeoIAZ (Motor Portable)...", QgsTask.CanCancel)
        self.ruta_salida = ruta_salida
        self.archivos_a_comprimir = archivos_a_comprimir
        self.error_msg = ""

    def run(self):
        try:
            # DETECCIÓN AUTOMÁTICA DE SISTEMA OPERATIVO
            if os.name == 'nt': # Windows
                ruta_cli = r"C:\GeoIAZ\GeoIAZ_CLI.exe"
                if not os.path.exists(ruta_cli):
                    ruta_cli = r"C:\Program Files\GeoIAZ\GeoIAZ_CLI.exe"
                if not os.path.exists(ruta_cli):
                    self.error_msg = "No se encontró GeoIAZ_CLI.exe. Asegurate de tenerlo instalado."
                    return False
                comando = [ruta_cli, "pack"]
            else: # Linux / macOS
                ruta_main = "/opt/geoiaz/main.py"
                if not os.path.exists(ruta_main):
                    self.error_msg = "No se encontró main.py en /opt/geoiaz."
                    return False
                comando = ["python3", ruta_main, "pack"]

            temp_dir = tempfile.mkdtemp(prefix="geoiaz_comp_")
            for archivo in self.archivos_a_comprimir:
                if os.path.exists(archivo):
                    if os.path.isfile(archivo):
                        shutil.copy2(archivo, temp_dir)
                    elif os.path.isdir(archivo):
                        shutil.copytree(archivo, os.path.join(temp_dir, os.path.basename(archivo)))

            comando.extend([temp_dir, self.ruta_salida])

            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            env = os.environ.copy()
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            env.pop("QGIS_PREFIX_PATH", None)

            resultado = subprocess.run(
                comando, 
                capture_output=True,
                text=True,
                env=env,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            ) # nosec B603
            
            shutil.rmtree(temp_dir, ignore_errors=True)

            if resultado.returncode != 0:
                self.error_msg = f"Error en motor: {resultado.stderr}"
                return False
            
            return True
            
        except Exception as e:
            self.error_msg = str(e)
            return False

class DialogoSeleccion(QDialog):
    def __init__(self, archivos):
        super().__init__()
        self.setWindowTitle("Seleccionar archivos a cargar")
        self.resize(600, 400)
        self.archivos = archivos
        self.seleccionados = []
        
        layout = QVBoxLayout()
        
        self.tabla = QTableWidget(len(archivos), 3)
        self.tabla.setHorizontalHeaderLabels(["", "Archivo", "Tamaño"])
        self.tabla.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabla.setColumnWidth(0, 40)
        self.tabla.setEditTriggers(QTableWidget.NoEditTriggers)
        
        for i, arch in enumerate(archivos):
            chk = QCheckBox()
            chk.setChecked(True)
            
            container = QWidget()
            layout_chk = QHBoxLayout(container)
            layout_chk.addWidget(chk)
            layout_chk.setAlignment(Qt.AlignCenter)
            layout_chk.setContentsMargins(0,0,0,0)
            self.tabla.setCellWidget(i, 0, container)
            
            nombre_mostrar = arch['nombre']
            if arch.get('es_gdb'):
                nombre_mostrar += " (Geodatabase)"
            
            item_nombre = QTableWidgetItem(nombre_mostrar)
            tamaño_str = f"{arch['tamaño']/1024/1024:.2f} MB" if arch['tamaño'] > 0 else "GDB"
            item_tamaño = QTableWidgetItem(tamaño_str)
            item_tamaño.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            
            self.tabla.setItem(i, 1, item_nombre)
            self.tabla.setItem(i, 2, item_tamaño)
            
            item_nombre.setData(Qt.UserRole, chk)
            
        layout.addWidget(QLabel("Seleccione las capas que desea cargar en QGIS:"))
        layout.addWidget(self.tabla)
        
        btn_layout = QHBoxLayout()
        btn_cargar = QPushButton("Cargar seleccionados")
        btn_cancelar = QPushButton("Cancelar")
        
        btn_cargar.clicked.connect(self.aceptar)
        btn_cancelar.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancelar)
        btn_layout.addWidget(btn_cargar)
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)

    def aceptar(self):
        for i in range(self.tabla.rowCount()):
            item = self.tabla.item(i, 1)
            chk = item.data(Qt.UserRole)
            if chk and chk.isChecked():
                self.seleccionados.append(self.archivos[i])
        self.accept()

class GeoIAZPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = '&GeoIAZ'
        self.carpetas_temporales = []
        self.task_actual = None
        self.filtro_global = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        
        action = QAction(QIcon(icon_path), "Abrir archivo .giz", self.iface.mainWindow())
        action.triggered.connect(self.abrir_giz)
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)

        self.action_compress_file = QAction(QIcon(icon_path), "Comprimir archivo(s)...", self.iface.mainWindow())
        self.action_compress_file.triggered.connect(self.comprimir_archivo_directo)
        self.iface.addToolBarIcon(self.action_compress_file)
        self.iface.addPluginToMenu(self.menu, self.action_compress_file)
        self.actions.append(self.action_compress_file)
        
        self.action_compress = QAction(QIcon(icon_path), "Comprimir con GeoIAZ", self.iface.mainWindow())
        self.action_compress.triggered.connect(self.comprimir_capa)
        self.iface.layerTreeView().contextMenuAboutToShow.connect(self.menuContextoCapas)
        
        try:
            self.filtro_global = GeoIAZFiltroGlobal(self)
            QgsApplication.instance().installEventFilter(self.filtro_global)
            self.iface.mainWindow().installEventFilter(self.filtro_global)
        except Exception as e:
            QgsMessageLog.logMessage(f"Error crítico al activar Drag & Drop: {e}", "GeoIAZ", Qgis.Critical)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
            
        try:
            self.iface.layerTreeView().contextMenuAboutToShow.disconnect(self.menuContextoCapas)
        except Exception:
            pass # nosec B110
            
        try:
            if self.filtro_global:
                QgsApplication.instance().removeEventFilter(self.filtro_global)
                self.iface.mainWindow().removeEventFilter(self.filtro_global)
        except Exception:
            pass # nosec B110
        self.limpiar_temporales()

    def menuContextoCapas(self, menu):
        capa = self.iface.layerTreeView().currentLayer()
        if capa:
            menu.addSeparator()
            menu.addAction(self.action_compress)

    def limpiar_temporales(self):
        for carpeta in self.carpetas_temporales:
            try:
                if os.path.exists(carpeta):
                    shutil.rmtree(carpeta, ignore_errors=True)
            except Exception:
                pass # nosec B110
        self.carpetas_temporales.clear()

    def abrir_giz(self):
        ruta_giz, _ = QFileDialog.getOpenFileName(self.iface.mainWindow(), "Abrir archivo GeoIAZ", "", "Archivos GeoIAZ (*.giz)")
        if not ruta_giz: return
        self.procesar_giz(ruta_giz)

    def comprimir_archivo_directo(self):
        archivos, _ = QFileDialog.getOpenFileNames(self.iface.mainWindow(), "Seleccionar archivos para comprimir", "", "Todos los archivos (*.*)")
        if not archivos: return

        nombre_sugerido = f"{os.path.splitext(os.path.basename(archivos[0]))[0]}.giz"
        ruta_salida, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), "Guardar como GeoIAZ", nombre_sugerido, "Archivos GeoIAZ (*.giz)")
        
        if not ruta_salida: return

        self.iface.messageBar().pushMessage("GeoIAZ", f"Comprimiendo {len(archivos)} archivo(s)...", level=Qgis.Info, duration=5)
        
        self.task_actual = ComprimirGizTask(ruta_salida, archivos)
        self.task_actual.taskCompleted.connect(lambda: self.on_compresion_terminada(self.task_actual.error_msg, ruta_salida))
        self.task_actual.taskCompleted.connect(lambda: self.iface.messageBar().popWidget())
        self.task_actual.taskTerminated.connect(lambda: self.on_task_terminated(self.task_actual.error_msg))
        
        QgsApplication.taskManager().addTask(self.task_actual)

    def procesar_giz(self, ruta_giz):
        ruta_temp = tempfile.mkdtemp(prefix="geoiaz_")
        self.carpetas_temporales.append(ruta_temp)

        self.iface.messageBar().pushMessage("GeoIAZ", f"Extrayendo {os.path.basename(ruta_giz)}...", level=Qgis.Info, duration=5)
        self.task_actual = ExtraerGizTask(ruta_giz, ruta_temp)
        self.task_actual.taskCompleted.connect(lambda: self.on_extraccion_terminada(self.task_actual.archivos_extraidos, self.task_actual.error_msg))
        self.task_actual.taskCompleted.connect(lambda: self.iface.messageBar().popWidget())
        self.task_actual.taskTerminated.connect(lambda: self.on_task_terminated(self.task_actual.error_msg))
        QgsApplication.taskManager().addTask(self.task_actual)

    def on_task_terminated(self, error_msg):
        self.iface.messageBar().popWidget()
        if error_msg:
            self.iface.messageBar().pushMessage("GeoIAZ ERROR", error_msg, level=Qgis.Critical, duration=15)
        else:
            self.iface.messageBar().pushMessage("GeoIAZ", "La tarea fue cancelada.", level=Qgis.Warning, duration=5)

    def on_extraccion_terminada(self, archivos, error_msg):
        if error_msg:
            self.iface.messageBar().pushMessage("GeoIAZ ERROR", error_msg, level=Qgis.Critical, duration=15)
            return
        if not archivos:
            self.iface.messageBar().pushMessage("GeoIAZ", "No se encontraron capas válidas para cargar.", level=Qgis.Warning, duration=5)
            return

        dialogo = DialogoSeleccion(archivos)
        if dialogo.exec_() == QDialog.Accepted:
            self.cargar_capas(dialogo.seleccionados)

    def cargar_capas(self, seleccionados):
        capas_cargadas = 0
        ignoradas = 0
        
        for arch in seleccionados:
            ruta = arch['ruta']
            es_gdb = arch.get('es_gdb', False)
            capa = None
            ext = os.path.splitext(ruta)[1].lower()

            if es_gdb:
                capa = QgsVectorLayer(ruta, os.path.basename(ruta), "ogr")
            elif ext == '.shp':
                capa = QgsVectorLayer(ruta, os.path.basename(ruta), "ogr")
            elif ext in ('.tif', '.tiff'):
                try:
                    capa = QgsRasterLayer(ruta, os.path.basename(ruta), "gdal")
                    if arch['tamaño'] > 50 * 1024 * 1024:
                        QgsMessageLog.logMessage(f"Creando pirámides para TIF grande: {ruta}", "GeoIAZ", Qgis.Info)
                        capa.buildOverviews("AVERAGE", [2, 4, 8, 16])
                except Exception as e:
                    QgsMessageLog.logMessage(f"Error cargando TIF {ruta}: {e}", "GeoIAZ", Qgis.Warning)
            elif ext in ('.gpkg', '.sqlite'):
                capa = QgsVectorLayer(ruta, os.path.basename(ruta), "ogr")
            elif ext == '.csv':
                uri = f"file:///{ruta.replace(os.sep, '/')}?delimiter=,"
                capa = QgsVectorLayer(uri, os.path.basename(ruta), "delimitedtext")
            elif ext in ('.gml', '.kml', '.json', '.geojson'):
                capa = QgsVectorLayer(ruta, os.path.basename(ruta), "ogr")
            elif ext == '.kmz':
                uri = f"/vsizip/{ruta}"
                capa = QgsVectorLayer(uri, os.path.basename(ruta), "ogr")
            elif ext in ('.las', '.laz'):
                if TIENE_NUBES_PUNTOS:
                    capa = QgsPointCloudLayer(ruta, os.path.basename(ruta), "pdal")
                else:
                    QgsMessageLog.logMessage(f"Tu versión de QGIS no soporta archivos {ext}.", "GeoIAZ", Qgis.Warning)
            elif ext == '.zip':
                try:
                    with zipfile.ZipFile(ruta, 'r') as z:
                        for nombre_interno in z.namelist():
                            ext_interna = os.path.splitext(nombre_interno)[1].lower()
                            if ext_interna in ('.shp', '.tif', '.tiff', '.gpkg', '.kml', '.gml'):
                                ruta_virtual = f"/vsizip/{ruta}/{nombre_interno}"
                                if ext_interna == '.shp':
                                    capa = QgsVectorLayer(ruta_virtual, os.path.basename(nombre_interno), "ogr")
                                elif ext_interna in ('.tif', '.tiff'):
                                    capa = QgsRasterLayer(ruta_virtual, os.path.basename(nombre_interno), "gdal")
                                else:
                                    capa = QgsVectorLayer(ruta_virtual, os.path.basename(nombre_interno), "ogr")
                                
                                if capa and capa.isValid():
                                    QgsProject.instance().addMapLayer(capa)
                                    capas_cargadas += 1
                                    break
                except Exception as e:
                    QgsMessageLog.logMessage(f"Error abriendo ZIP {ruta}: {e}", "GeoIAZ", Qgis.Warning)

            if capa and capa.isValid():
                QgsProject.instance().addMapLayer(capa)
                capas_cargadas += 1
            elif ext != '.zip':
                ignoradas += 1
                QgsMessageLog.logMessage(f"No se pudo cargar la capa: {ruta}", "GeoIAZ", Qgis.Warning)

        mensaje = f"Se cargaron {capas_cargadas} capas en QGIS."
        if ignoradas > 0:
            mensaje += f" ({ignoradas} archivos ignorados por QGIS)."
        
        self.iface.messageBar().pushMessage("GeoIAZ", mensaje, level=Qgis.Success, duration=10)

    def comprimir_capa(self):
        capa = self.iface.activeLayer()
        if not capa:
            self.iface.messageBar().pushMessage("GeoIAZ", "No hay ninguna capa seleccionada.", level=Qgis.Warning, duration=5)
            return

        ruta_fuente = capa.source()
        if not ruta_fuente:
            self.iface.messageBar().pushMessage("GeoIAZ", "La capa seleccionada no tiene un archivo fuente válido.", level=Qgis.Warning, duration=5)
            return

        archivos_a_comprimir = []
        ext = os.path.splitext(ruta_fuente)[1].lower()

        if ext == '.shp':
            base = ruta_fuente[:-4]
            for extension in ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.sbn', '.sbx', '.qix']:
                archivo_asociado = base + extension
                if os.path.exists(archivo_asociado):
                    archivos_a_comprimir.append(archivo_asociado)
        elif '.gdb' in ruta_fuente.lower():
            idx_gdb = ruta_fuente.lower().find('.gdb')
            ruta_gdb = ruta_fuente[:idx_gdb + 4]
            archivos_a_comprimir.append(ruta_gdb)
        else:
            if os.path.exists(ruta_fuente):
                archivos_a_comprimir.append(ruta_fuente)

        if not archivos_a_comprimir:
            self.iface.messageBar().pushMessage("GeoIAZ", "No se encontraron archivos para comprimir.", level=Qgis.Warning, duration=5)
            return

        nombre_sugerido = f"{capa.name()}.giz"
        ruta_salida, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), "Guardar como GeoIAZ", nombre_sugerido, "Archivos GeoIAZ (*.giz)")
        
        if not ruta_salida: return

        self.iface.messageBar().pushMessage("GeoIAZ", f"Comprimiendo {capa.name()}...", level=Qgis.Info, duration=5)
        
        self.task_actual = ComprimirGizTask(ruta_salida, archivos_a_comprimir)
        self.task_actual.taskCompleted.connect(lambda: self.on_compresion_terminada(self.task_actual.error_msg, ruta_salida))
        self.task_actual.taskCompleted.connect(lambda: self.iface.messageBar().popWidget())
        self.task_actual.taskTerminated.connect(lambda: self.on_task_terminated(self.task_actual.error_msg))
        
        QgsApplication.taskManager().addTask(self.task_actual)

    def on_compresion_terminada(self, error_msg, ruta_salida):
        self.iface.messageBar().popWidget()
        if error_msg:
            self.iface.messageBar().pushMessage("GeoIAZ ERROR", error_msg, level=Qgis.Critical, duration=15)
        else:
            self.iface.messageBar().pushMessage("GeoIAZ", f"¡Éxito! Archivo generado: {os.path.basename(ruta_salida)}", level=Qgis.Success, duration=10)