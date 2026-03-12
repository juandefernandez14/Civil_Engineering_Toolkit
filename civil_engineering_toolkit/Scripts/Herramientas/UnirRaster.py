
from qgis.PyQt.QtWidgets import QMessageBox, QInputDialog
from qgis.core import QgsProject, QgsRasterLayer, QgsProcessingFeedback
import processing

def pixel_size(layer: QgsRasterLayer):
    ext = layer.extent()
    xres = abs(ext.width() / layer.width())
    yres = abs(ext.height() / layer.height())
    return xres, yres

# --- 1) Listar ráster cargados ---
rasters = [lyr for lyr in QgsProject.instance().mapLayers().values() if isinstance(lyr, QgsRasterLayer)]
if len(rasters) < 2:
    QMessageBox.critical(None, "Error", "Necesitas al menos 2 ráster (MDT) cargados en el proyecto.")
    raise Exception("No hay suficientes ráster.")

names = [r.name() for r in rasters]

# --- 2) Elegir MDT1 (referencia) y MDT2 ---
r1_name, ok1 = QInputDialog.getItem(None, "Seleccionar MDT 1 (referencia)", "MDT 1:", names, 0, False)
if not ok1 or not r1_name:
    raise Exception("Cancelado.")

r2_name, ok2 = QInputDialog.getItem(None, "Seleccionar MDT 2", "MDT 2:", names, 1, False)
if not ok2 or not r2_name:
    raise Exception("Cancelado.")

r1 = next(r for r in rasters if r.name() == r1_name)
r2 = next(r for r in rasters if r.name() == r2_name)

# --- 3) Resolución objetivo = la del MDT1 ---
xres, yres = pixel_size(r1)
target_crs = r1.crs()

feedback = QgsProcessingFeedback()

# --- 4) Construir VRT con ambos (mosaico virtual) ---
vrt_path = processing.run("gdal:buildvirtualraster", {
    "INPUT": [r1, r2],
    "SEPARATE": False,
    "PROJ_DIFFERENCE": True,   # permite CRS distintos (luego warp al CRS objetivo)
    "ADD_ALPHA": False,
    "ASSIGN_CRS": None,
    "SRC_NODATA": None,
    "RESOLUTION": 0,           # valor seguro en QGIS antiguos (no dependemos de esto)
    "OUTPUT": "TEMPORARY_OUTPUT"
}, feedback=feedback)["OUTPUT"]

# --- 5) “Fijar” resolución + alineación + CRS con Warp (salida temporal) ---
# Nota: TARGET_RESOLUTION en QGIS 3.4 suele ser un único valor (pixel cuadrado).
# Usamos xres del MDT1 como referencia.
out_path = processing.run("gdal:warpreproject", {
    "INPUT": vrt_path,
    "SOURCE_CRS": None,
    "TARGET_CRS": target_crs,
    "RESAMPLING": 1,              # 1=Bilinear (recomendable para DEM)
    "NODATA": None,
    "TARGET_RESOLUTION": xres,    # mantiene tamaño píxel del MDT1
    "OPTIONS": "",
    "DATA_TYPE": 0,
    "TARGET_EXTENT": None,
    "TARGET_EXTENT_CRS": None,
    "MULTITHREADING": True,
    "EXTRA": "-tap",              # alinear a la rejilla (clave para DEM)
    "OUTPUT": "TEMPORARY_OUTPUT"
}, feedback=feedback)["OUTPUT"]

# --- 6) Cargar salida ---
out_layer = QgsRasterLayer(out_path, f"MDT_Mosaico_temp_{r1.name()}_{r2.name()}")
if not out_layer.isValid():
    QMessageBox.critical(None, "Error", "No se pudo cargar el MDT mosaico temporal.")
    raise Exception("Salida inválida.")

QgsProject.instance().addMapLayer(out_layer)
QMessageBox.information(None, "OK", "MDT mosaico temporal creado manteniendo resolución del MDT 1.")
