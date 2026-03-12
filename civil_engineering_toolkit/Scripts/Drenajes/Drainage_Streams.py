from qgis.PyQt.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QLabel, QComboBox, QLineEdit, QDialogButtonBox
)
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsProcessingFeedback
import processing

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def pixel_size(layer: QgsRasterLayer):
    ext = layer.extent()
    xres = abs(ext.width() / layer.width())
    yres = abs(ext.height() / layer.height())
    return xres, yres

def add_layer(output, name):
    if isinstance(output, (QgsRasterLayer, QgsVectorLayer)):
        output.setName(name)
        QgsProject.instance().addMapLayer(output)
        return output

    r = QgsRasterLayer(output, name)
    if r.isValid():
        QgsProject.instance().addMapLayer(r)
        return r

    v = QgsVectorLayer(output, name, "ogr")
    if v.isValid():
        QgsProject.instance().addMapLayer(v)
        return v

    QMessageBox.warning(None, "Aviso", f"No se pudo cargar la salida '{name}'.")
    return None

def problem_area_percent(problem_raster_path, reference_raster_layer):
    """
    % = (celdas no-nodata en problem areas / celdas totales de referencia) * 100
    Asume que el raster 'areas' de r.fill.dir deja NODATA fuera de las zonas problemáticas.
    """
    total = reference_raster_layer.width() * reference_raster_layer.height()
    if total <= 0:
        return 100.0

    stats = processing.run("qgis:rasterlayerstatistics", {
        "INPUT": problem_raster_path,
        "BAND": 1
    })
    count = None
    for k in ("COUNT", "count", "N", "n"):
        if k in stats:
            count = stats[k]
            break
    if count is None:
        raise Exception("No se pudo obtener COUNT de 'qgis:rasterlayerstatistics' para Problem Areas.")

    return (float(count) / float(total)) * 100.0

# -------------------------------------------------
# UI Dialog (DEM + Threshold)
# -------------------------------------------------
class DemThresholdDialog(QDialog):
    def __init__(self, raster_layers):
        super().__init__(None)
        self.setWindowTitle("Hidrología DEM: MDT + threshold")

        self.raster_layers = raster_layers
        self.name_to_layer = {lyr.name(): lyr for lyr in raster_layers}

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Selecciona el MDT (DEM):"))
        self.combo = QComboBox()
        self.combo.addItems([lyr.name() for lyr in raster_layers])
        layout.addWidget(self.combo)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        layout.addWidget(QLabel("Threshold (nº de celdas para iniciar cauces):"))
        self.thr_edit = QLineEdit()
        self.thr_edit.setPlaceholderText("Ej: 1000")
        layout.addWidget(self.thr_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.combo.currentTextChanged.connect(self._update_info)
        self._update_info(self.combo.currentText())

        self._dem = None
        self._threshold = None
        self._cellsize_square = None

    def _update_info(self, dem_name):
        dem = self.name_to_layer[dem_name]
        xres, yres = pixel_size(dem)

        # Celda cuadrada = min(xres, yres)
        cell = min(xres, yres)
        self._cellsize_square = cell

        txt = (
            f"Resolución detectada del MDT: {xres:.3f} x {yres:.3f} (unidades del CRS)\n"
            f"Tamaño de celda usado (cuadrada): {cell:.3f} x {cell:.3f}\n\n"
            "Iteración:\n"
            "• r.fill.dir se ejecuta en bucle usando el DEM rellenado del ciclo anterior\n"
            "  hasta que Problem Areas < 5% del área total (conteo de celdas).\n"
        )
        self.info.setText(txt)

        if not self.thr_edit.text().strip():
            if cell <= 1.5:
                self.thr_edit.setText("100000")
            elif cell <= 7.5:
                self.thr_edit.setText("10000")
            else:
                self.thr_edit.setText("1000")

    def _validate_and_accept(self):
        self._dem = self.name_to_layer[self.combo.currentText()]

        raw_thr = self.thr_edit.text().strip()
        if raw_thr == "":
            QMessageBox.critical(None, "Error", "Debes introducir un threshold (entero > 0).")
            return
        try:
            thr = int(raw_thr)
            if thr <= 0:
                raise ValueError
            self._threshold = thr
        except Exception:
            QMessageBox.critical(None, "Error", "El threshold debe ser un entero > 0.")
            return

        self.accept()

    def values(self):
        return self._dem, self._cellsize_square, self._threshold

# -------------------------------------------------
# Main
# -------------------------------------------------
rasters = [lyr for lyr in QgsProject.instance().mapLayers().values() if isinstance(lyr, QgsRasterLayer)]
if not rasters:
    QMessageBox.critical(None, "Error", "No hay ráster cargados. Carga un MDT (DEM) primero.")
    raise Exception("Sin DEM.")

dlg = DemThresholdDialog(rasters)
if dlg.exec_() != QDialog.Accepted:
    raise Exception("Cancelado por el usuario.")

dem0, cellsize_square, threshold = dlg.values()

feedback = QgsProcessingFeedback()

# Iteración hasta Problem Areas < 5%
max_iters = 20
target_percent = 5.0

current_dem = dem0
final_outputs = {}
last_percent = None
iters_done = 0

for i in range(1, max_iters + 1):
    iters_done = i

    fill = processing.run("grass7:r.fill.dir", {
        "input": current_dem,
        "output": "TEMPORARY_OUTPUT",
        "direction": "TEMPORARY_OUTPUT",
        "areas": "TEMPORARY_OUTPUT",
        "format": 0,
        "GRASS_REGION_PARAMETER": None,
        "GRASS_REGION_CELLSIZE_PARAMETER": cellsize_square,
        "GRASS_RASTER_FORMAT_OPT": "",
        "GRASS_RASTER_FORMAT_META": ""
    }, feedback=feedback)

    filled_dem = fill["output"]
    problem_areas = fill["areas"]

    filled_layer = QgsRasterLayer(filled_dem, f"DEM_Filled_iter_{i}")
    if not filled_layer.isValid():
        raise Exception("No se pudo cargar el DEM rellenado de la iteración.")

    last_percent = problem_area_percent(problem_areas, filled_layer)
    print(f"[Iter {i}] Problem Areas = {last_percent:.2f}%")

    final_outputs = {
        "filled_dem": filled_dem,
        "problem_areas": problem_areas
    }

    if last_percent < target_percent:
        break

    current_dem = filled_dem

# Productos finales
filled_dem_final = final_outputs["filled_dem"]
problem_areas_final = final_outputs["problem_areas"]

watershed = processing.run("grass7:r.watershed", {
    "elevation": filled_dem_final,
    "threshold": threshold,
    "accumulation": "TEMPORARY_OUTPUT",
    "drainage": "TEMPORARY_OUTPUT",
    "stream": "TEMPORARY_OUTPUT",
    "convergence": 5,
    "memory": 300,
    "GRASS_REGION_PARAMETER": None,
    "GRASS_REGION_CELLSIZE_PARAMETER": cellsize_square,
    "GRASS_RASTER_FORMAT_OPT": "",
    "GRASS_RASTER_FORMAT_META": ""
}, feedback=feedback)

acc = watershed["accumulation"]
drain_dir = watershed["drainage"]
stream_raster = watershed["stream"]

# r.stream.extract
streams = processing.run("grass7:r.stream.extract", {
    "elevation": filled_dem_final,
    "accumulation": acc,
    "direction": drain_dir,
    "threshold": threshold,
    "stream_raster": "TEMPORARY_OUTPUT",
    "stream_vector": "TEMPORARY_OUTPUT",
    "memory": 300,
    "GRASS_REGION_PARAMETER": None,
    "GRASS_REGION_CELLSIZE_PARAMETER": cellsize_square,
    "GRASS_RASTER_FORMAT_OPT": "",
    "GRASS_RASTER_FORMAT_META": ""
}, feedback=feedback)

stream_vector = streams["stream_vector"]

# -------------------------------------------------
# Añadir SOLO las capas que quieres conservar
# (NO se crea/añade la capa de streams en puntos y NO se crea/añade problem areas en polígonos)
# -------------------------------------------------
add_layer(filled_dem_final, "DEM_Filled_final")
add_layer(problem_areas_final, "Problem_Areas_raster_final")
add_layer(acc, "Flow_Accumulation_final")
add_layer(drain_dir, "Drainage_Direction_final")
add_layer(stream_raster, "Stream_Raster_final")
add_layer(stream_vector, "Stream_Segments_final")

QMessageBox.information(
    None, "OK",
    "Proceso completado (todo temporal).\n\n"
    f"Iteraciones r.fill.dir: {iters_done} (máx. {max_iters})\n"
    f"Problem Areas final: {last_percent:.2f}% (objetivo < {target_percent:.1f}%)\n"
    f"Celda usada (cuadrada): {cellsize_square:.3f}\n"
    f"Threshold: {threshold}\n\n"
    "Capas finales añadidas:\n"
    "• DEM_Filled_final\n"
    "• Problem_Areas_raster_final\n"
    "• Flow_Accumulation_final\n"
    "• Drainage_Direction_final\n"
    "• Stream_Raster_final\n"
    "• Stream_Segments_final\n"
)
