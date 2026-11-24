from pathlib import Path

# Firmas de bytes (magic numbers) para formatos binarios comunes
FIRMAS_BINARIAS = {
    b'PK\x03\x04': 'ZIP (puede ser KMZ, XLSX, DOCX)',
    b'SQLite format 3\x00': 'SQLite (puede ser GeoPackage o SpatiaLite)',
    b'LASF': 'LAS / LAZ (LiDAR)',
    b'II*\x00': 'TIFF (puede ser GeoTIFF)',
    b'MM\x00*': 'TIFF (Big Endian, puede ser GeoTIFF)',
    b'\x89PNG': 'PNG',
    b'\xFF\xD8\xFF': 'JPEG',
    b'PAR1': 'Parquet',
    b'DWG': 'AutoCAD DWG',
    b'\x00\x00\x00\x0c': 'DGN (MicroStation)',
}

# Extensiones conocidas y su categoría
EXTENSIONES = {
    # Vectorial
    '.shp': 'Vectorial (Shapefile)',
    '.shx': 'Índice Shapefile',
    '.dbf': 'Tabla Shapefile',
    '.prj': 'Proyección Shapefile',
    '.cpg': 'Codepage Shapefile',
    '.sbn': 'Índice Espacial Shapefile',
    '.sbx': 'Índice Espacial Shapefile',
    '.gpkg': 'Vectorial + Raster + SQL (GeoPackage)',
    '.geojson': 'Vectorial (GeoJSON)',
    '.gml': 'Vectorial (GML / INSPIRE)',
    '.kml': 'Vectorial (KML)',
    '.kmz': 'Vectorial (KMZ comprimido)',

    # Raster
    '.tif': 'Raster (GeoTIFF)',
    '.tiff': 'Raster (GeoTIFF)',
    '.asc': 'Raster (ASCII Grid)',
    '.png': 'Raster (PNG)',
    '.jpg': 'Raster (JPEG)',
    '.jpeg': 'Raster (JPEG)',

    # LiDAR
    '.laz': 'LiDAR (LAZ)',
    '.las': 'LiDAR (LAS)',

    # Bases de datos
    '.sqlite': 'Base de datos SQLite',
    '.db': 'Base de datos SQLite',
    '.spatialite': 'Base de datos SpatiaLite',
    '.mdb': 'Base de datos Microsoft Access',

    # Tabular y texto
    '.csv': 'Tabular (Texto)',
    '.txt': 'Texto plano',
    '.json': 'Estructurado (JSON)',
    '.xml': 'Estructurado (XML)',
    '.rdf': 'Metadatos (RDF)',
    '.md': 'Documento (Markdown)',
    '.readme': 'Documento (Readme)',

    # Columnar
    '.parquet': 'Columnar (Parquet)',

    # CAD
    '.dgn': 'CAD (MicroStation)',
    '.dwg': 'CAD (AutoCAD)',

    # Documentos
    '.html': 'Documento (HTML)',
    '.htm': 'Documento (HTML)',
    '.pdf': 'Documento (PDF)',

    # Estilos y metadatos de SIG
    '.qml': 'Estilo QGIS',
    '.sld': 'Estilo OGC (SLD)',
    '.lyr': 'Estilo ArcGIS',
    '.lyrx': 'Estilo ArcGIS Pro',
    '.qmd': 'Metadatos QGIS',
}

def detectar_archivo(ruta):
    """Detecta el formato de un archivo usando extensión y magic numbers."""
    ruta = Path(ruta)
    if not ruta.is_file():
        return None

    nombre = ruta.name
    ext = ruta.suffix.lower()
    
    # 1. Detectar por extensión
    categoria = EXTENSIONES.get(ext, 'Desconocido')
    
    # 2. Detectar por magic number (solo para archivos binarios)
    try:
        with open(ruta, 'rb') as f:
            cabecera = f.read(16)  # Leemos los primeros 16 bytes
        
        for firma, descripcion in FIRMAS_BINARIAS.items():
            if cabecera.startswith(firma):
                # Si la extensión ya nos dio algo específico, lo mantenemos
                if categoria == 'Desconocido' or 'SQLite' in descripcion:
                    categoria = descripcion
                break
    except Exception:
        pass  # nosec B110 - Si no se puede leer, nos quedamos con lo que dijo la extensión

    return {
        'archivo': nombre,
        'extension': ext,
        'categoria': categoria,
        'tamaño': ruta.stat().st_size
    }

def detectar_carpeta(carpeta):
    """Recorre una carpeta y detecta todos los archivos."""
    resultados = []
    for f in Path(carpeta).rglob('*'):
        if f.is_file():
            info = detectar_archivo(f)
            if info:
                resultados.append(info)
    return resultados