from qgis.core import (
    QgsProject,
    QgsWkbTypes,
    QgsField,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsRasterLayer
)
from PyQt5.QtCore import QVariant
from PyQt5.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QLabel,
    QComboBox, QLineEdit, QDialogButtonBox
)
import processing

# ---------------------------------------------------------
# FIX: iface seguro (ejecución desde plugin)
# ---------------------------------------------------------
try:
    from qgis.utils import iface
except Exception:
    iface = None

parent = iface.mainWindow() if iface else None
if iface is None:
    raise Exception("Este script debe ejecutarse dentro de QGIS GUI.")


# ---------------------------------------------------------
# Diálogo único: capa + buffer + MDT
# ---------------------------------------------------------
class CunetasDialog(QDialog):
    def __init__(self, line_layers, raster_layers):
        super().__init__(parent)
        self.setWindowTitle("Cunetas / Cauces - Parámetros")

        self._line_dict = {l.name(): l for l in line_layers}
        self._raster_dict = {r.name(): r for r in raster_layers}

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Selecciona la capa (líneas):"))
        self.combo_line = QComboBox()
        self.combo_line.addItems(list(self._line_dict.keys()))
        layout.addWidget(self.combo_line)

        layout.addWidget(QLabel("Distancia de buffer en metros (en blanco = 2 m):"))
        self.buffer_edit = QLineEdit()
        self.buffer_edit.setPlaceholderText("2.0")
        layout.addWidget(self.buffer_edit)

        layout.addWidget(QLabel("Selecciona el MDT:"))
        self.combo_raster = QComboBox()
        self.combo_raster.addItems(list(self._raster_dict.keys()))
        layout.addWidget(self.combo_raster)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._layer = None
        self._buffer = 2.0
        self._raster = None

    def _validate_and_accept(self):
        self._layer = self._line_dict[self.combo_line.currentText()]

        raw = self.buffer_edit.text().strip()
        if raw == "":
            self._buffer = 2.0
        else:
            try:
                val = float(raw.replace(",", "."))
                if val <= 0:
                    raise ValueError
                self._buffer = val
            except Exception:
                QMessageBox.critical(self, "Error", "Distancia de buffer inválida (usa número > 0).")
                return

        self._raster = self._raster_dict[self.combo_raster.currentText()]
        self.accept()

    def values(self):
        return self._layer, self._buffer, self._raster


# ---------------------------------------------------------
# Núcleo del proceso (una ejecución)
# ---------------------------------------------------------
def run_once(layer, buffer_dist_m, raster_layer):
    # 1) Calcular longitudes
    layer.startEditing()

    if "longitud" not in [f.name() for f in layer.fields()]:
        layer.dataProvider().addAttributes([QgsField("longitud", QVariant.Double)])
        layer.updateFields()

    idx_long = layer.fields().indexFromName("longitud")

    expr = QgsExpression("round(length($geometry), 2)")
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))

    for feat in layer.getFeatures():
        ctx.setFeature(feat)
        val = expr.evaluate(ctx)
        layer.changeAttributeValue(feat.id(), idx_long, val)

    layer.commitChanges()

    # 2) Buffer
    buffer_result = processing.run("native:buffer", {
        "INPUT": layer,
        "DISTANCE": buffer_dist_m,
        "SEGMENTS": 5,
        "DISSOLVE": False,
        "OUTPUT": "memory:"
    })
    buffer_layer = buffer_result["OUTPUT"]
    buffer_layer.setName(f"Buffer_{str(buffer_dist_m).replace('.', 'p')}m")
    QgsProject.instance().addMapLayer(buffer_layer)

    # 3) Zonal statistics (MIN/MAX)
    zonal = processing.run("native:zonalstatisticsfb", {
        "INPUT": buffer_layer,
        "INPUT_RASTER": raster_layer,
        "STATISTICS": [5, 6],  # 5=MAX, 6=MIN
        "OUTPUT": "memory:"
    })
    zonal_layer = zonal["OUTPUT"]
    zonal_layer.setName("Análisis_Zonal")
    QgsProject.instance().addMapLayer(zonal_layer)

    # 4) Campos MIN/MAX/DIFF/PTE
    zonal_layer.startEditing()

    existing = [f.name() for f in zonal_layer.fields()]
    for fn in ["MIN", "MAX", "DIFF", "PTE"]:
        if fn not in existing:
            zonal_layer.dataProvider().addAttributes([QgsField(fn, QVariant.Double)])

    zonal_layer.updateFields()
    idx_min = zonal_layer.fields().indexFromName("MIN")
    idx_max = zonal_layer.fields().indexFromName("MAX")
    idx_diff = zonal_layer.fields().indexFromName("DIFF")
    idx_pte = zonal_layer.fields().indexFromName("PTE")

    # Compatibilidad: asegurar que _min/_max existan
    fieldnames = [f.name() for f in zonal_layer.fields()]
    if "_min" not in fieldnames or "_max" not in fieldnames:
        zonal_layer.commitChanges()
        raise Exception("El zonalstatisticsfb no generó campos _min/_max. Revisa la versión de QGIS o STATISTICS.")

    for feat in zonal_layer.getFeatures():
        min_val = feat["_min"]
        max_val = feat["_max"]
        length_val = feat["longitud"] if "longitud" in feat.fields().names() else None

        if min_val is not None:
            zonal_layer.changeAttributeValue(feat.id(), idx_min, round(min_val, 2))
        if max_val is not None:
            zonal_layer.changeAttributeValue(feat.id(), idx_max, round(max_val, 2))

        if min_val is not None and max_val is not None:
            diff = round(max_val - min_val, 2)
            zonal_layer.changeAttributeValue(feat.id(), idx_diff, diff)

            if length_val not in (None, 0):
                zonal_layer.changeAttributeValue(feat.id(), idx_pte, round(diff / float(length_val), 4))

    zonal_layer.commitChanges()

    # 5) Eliminar _min/_max (si existen)
    zonal_layer.startEditing()
    fields_now = [f.name() for f in zonal_layer.fields()]
    delete_idx = []
    for f in ["_min", "_max"]:
        if f in fields_now:
            delete_idx.append(zonal_layer.fields().indexFromName(f))

    if delete_idx:
        zonal_layer.dataProvider().deleteAttributes(delete_idx)
        zonal_layer.updateFields()

    zonal_layer.commitChanges()

    return buffer_layer, zonal_layer


# ---------------------------------------------------------
# Bucle de ejecución: al terminar preguntar si continuar
# ---------------------------------------------------------
while True:
    all_layers = list(QgsProject.instance().mapLayers().values())

    line_layers = [
        l for l in all_layers
        if hasattr(l, "geometryType") and l.geometryType() == QgsWkbTypes.LineGeometry
    ]
    raster_layers = [r for r in all_layers if isinstance(r, QgsRasterLayer)]

    if not line_layers:
        QMessageBox.critical(parent, "Error", "No hay capas lineales en el proyecto.")
        break
    if not raster_layers:
        QMessageBox.critical(parent, "Error", "No hay ráster (MDT) cargados en el proyecto.")
        break

    dlg = CunetasDialog(line_layers, raster_layers)
    if dlg.exec_() != QDialog.Accepted:
        # Cancelar = salir sin preguntar (comportamiento típico)
        break

    layer, buffer_dist, dem = dlg.values()

    try:
        run_once(layer, buffer_dist, dem)
        QMessageBox.information(parent, "OK", "Proceso completado correctamente.")
    except Exception as e:
        QMessageBox.critical(parent, "Error", f"Error durante el proceso:\n{str(e)}")

    # Preguntar si continuar
    resp = QMessageBox.question(
        parent,
        "Continuar",
        "¿Quieres seguir utilizando el script?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes
    )

    if resp != QMessageBox.Yes:
        break

