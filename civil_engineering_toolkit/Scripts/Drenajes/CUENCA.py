from qgis.PyQt.QtWidgets import QMessageBox, QInputDialog
from qgis.PyQt.QtCore import Qt
from qgis.gui import QgsMapToolEmitPoint
from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsProcessingFeedback,
    QgsMapLayerType
)
import processing
import re

# ---------------------------------------------------------
# FIX: iface puede no existir si ejecutas desde un plugin
# ---------------------------------------------------------
try:
    from qgis.utils import iface  # <- evita "name 'iface' is not defined"
except Exception:
    iface = None


class RWaterOutletFromSelectedLayerTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, drainage_layer, parent=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.drainage_layer = drainage_layer
        self.parent = parent
        self._busy = False  # evita reentradas por doble ESC

    def canvasReleaseEvent(self, event):
        if self._busy:
            return
        point = self.toMapCoordinates(event.pos())
        self.run_r_water_outlet(point)

    def keyPressEvent(self, event):
        if event.key() != Qt.Key_Escape or self._busy:
            return

        self._busy = True
        try:
            print("ESC presionado: iniciando vectorización, unión y disolución de cuencas...")

            ok, processed_rasters = self.vectorize_and_merge_cuencas()
            if not ok:
                QMessageBox.information(self.parent, "Sin cuencas", "No había cuencas Cuenca_XX para procesar.")
                return

            resp = QMessageBox.question(
                self.parent,
                "Continuar",
                "Proceso terminado.\n¿Quieres seguir utilizando la herramienta para crear más cuencas?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )

            # BORRAR SIEMPRE el lote de ráster procesado (Sí o No)
            self.remove_layers(processed_rasters)

            if resp == QMessageBox.Yes:
                print("✔ Continuando: herramienta sigue activa. (ráster procesados borrados)")
                QMessageBox.information(
                    self.parent,
                    "Continuar",
                    "La herramienta sigue activa.\n"
                    "El lote anterior de cuencas ráster ha sido borrado.\n"
                    "Haz clic para crear nuevas cuencas.\nPulsa ESC para procesar el nuevo lote."
                )
            else:
                print("✔ Finalizando: herramienta desactivada. (ráster procesados borrados)")
                QMessageBox.information(self.parent, "Herramienta finalizada", "Has salido de la herramienta.")
                self.canvas.unsetMapTool(self)

        finally:
            self._busy = False

    def get_next_cuenca_name(self):
        existing_names = [layer.name() for layer in QgsProject.instance().mapLayers().values()]
        cuenca_numbers = []
        for name in existing_names:
            match = re.match(r"Cuenca_(\d+)$", name)
            if match:
                cuenca_numbers.append(int(match.group(1)))
        next_number = max(cuenca_numbers) + 1 if cuenca_numbers else 1
        return f"Cuenca_{str(next_number).zfill(2)}"

    def get_next_unified_name(self):
        existing = [lyr.name() for lyr in QgsProject.instance().mapLayers().values()]
        nums = []
        for n in existing:
            m = re.match(r"Cuencas_Unificadas_(\d+)$", n)
            if m:
                nums.append(int(m.group(1)))
        next_number = max(nums) + 1 if nums else 1
        return f"Cuencas_Unificadas_{str(next_number).zfill(2)}"

    def remove_layers(self, layers):
        prj = QgsProject.instance()
        for lyr in layers:
            try:
                prj.removeMapLayer(lyr.id())
            except Exception as e:
                print(f"✘ No se pudo eliminar {lyr.name()}: {e}")

    def run_r_water_outlet(self, point):
        input_layer = self.drainage_layer

        if input_layer.type() != QgsMapLayerType.RasterLayer:
            msg = "La capa seleccionada no es una capa raster."
            print(f"Error: {msg}")
            QMessageBox.critical(self.parent, "Error", msg)
            return

        x, y = point.x(), point.y()
        print(f"Coordenadas seleccionadas: {x:.2f}, {y:.2f}")

        params = {
            'input': input_layer,
            'coordinates': f'{x},{y}',
            'output': 'TEMPORARY_OUTPUT',
            'GRASS_REGION_PARAMETER': None,
            'GRASS_REGION_CELLSIZE_PARAMETER': 0,
            'GRASS_RASTER_FORMAT_OPT': '',
            'GRASS_RASTER_FORMAT_META': ''
        }

        feedback = QgsProcessingFeedback()
        try:
            result = processing.run("grass7:r.water.outlet", params, feedback=feedback)
            output_path = result['output']

            layer_name = self.get_next_cuenca_name()
            output_layer = QgsRasterLayer(output_path, layer_name)

            if output_layer.isValid():
                QgsProject.instance().addMapLayer(output_layer)
                print(f"✔ Cuenca generada: {layer_name}")
            else:
                print("✘ No se pudo cargar la capa de salida.")
                QMessageBox.critical(self.parent, "Error", "No se pudo cargar la capa de salida.")
        except Exception as e:
            print(f"✘ Error ejecutando r.water.outlet: {e}")
            QMessageBox.critical(self.parent, "Error ejecutando r.water.outlet", str(e))

    def vectorize_and_merge_cuencas(self):
        """
        Procesa SOLO el lote actual de Cuenca_XX (ráster presentes en el proyecto en el momento de pulsar ESC)
        y devuelve el listado de ráster procesados para borrarlos después.
        Robusto para el caso de 1 sola cuenca:
          - no intenta union
          - no llama deletecolumn si no hay columnas a borrar
        """
        feedback = QgsProcessingFeedback()

        # 1) Capturar lote actual
        cuenca_rasters = []
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and re.match(r"Cuenca_\d+$", layer.name()):
                cuenca_rasters.append(layer)

        if not cuenca_rasters:
            print("✘ No hay capas Cuenca_XX para vectorizar.")
            return False, []

        # 2) Vectorizar cada raster
        vector_layers = []
        for raster in cuenca_rasters:
            try:
                vect_result = processing.run("gdal:polygonize", {
                    'INPUT': raster,
                    'BAND': 1,
                    'FIELD': 'value',
                    'EIGHT_CONNECTEDNESS': False,
                    'EXTRA': '',
                    'OUTPUT': 'TEMPORARY_OUTPUT'
                }, feedback=feedback)

                vector_layers.append(vect_result['OUTPUT'])
                print(f"✔ Vectorizada: {raster.name()}")
            except Exception as e:
                print(f"✘ Error vectorizando {raster.name()}: {e}")
                QMessageBox.warning(self.parent, "Error vectorizando", f"No se pudo vectorizar {raster.name()}: {e}")

        if not vector_layers:
            QMessageBox.warning(self.parent, "Error", "No se pudo vectorizar ninguna cuenca del lote actual.")
            return False, []

        try:
            # 3) Unión SOLO si hay más de una capa
            if len(vector_layers) == 1:
                union_result = vector_layers[0]
                print("✔ Solo 1 cuenca: se omite la unión.")
            else:
                union_result = vector_layers[0]
                for next_layer in vector_layers[1:]:
                    union_result = processing.run("native:union", {
                        'INPUT': union_result,
                        'OVERLAY': next_layer,
                        'OUTPUT': 'TEMPORARY_OUTPUT'
                    }, feedback=feedback)['OUTPUT']
                print("✔ Unión de capas completada.")

            # 4) Dissolve (siempre)
            dissolved_layer = processing.run("native:dissolve", {
                'INPUT': union_result,
                'FIELD': [],
                'OUTPUT': 'TEMPORARY_OUTPUT'
            }, feedback=feedback)['OUTPUT']
            print("✔ Disolución completada.")

            # 5) Limpieza de campos (solo si hay algo que borrar)
            #    Mantener 'fid' y 'value' si existen, y si no, no forzar borrados peligrosos.
            keep = {'fid', 'value'}
            all_fields = [f.name() for f in dissolved_layer.fields()]
            fields_to_delete = [f for f in all_fields if f not in keep]

            if fields_to_delete:
                cleaned = processing.run("qgis:deletecolumn", {
                    'INPUT': dissolved_layer,
                    'COLUMN': fields_to_delete,
                    'OUTPUT': 'TEMPORARY_OUTPUT'
                }, feedback=feedback)['OUTPUT']
                final_layer = cleaned
                print("✔ Campos eliminados correctamente.")
            else:
                final_layer = dissolved_layer
                print("✔ No hay campos extra que eliminar (se omite deletecolumn).")

            final_layer.setName(self.get_next_unified_name())
            QgsProject.instance().addMapLayer(final_layer)

            QMessageBox.information(self.parent, "Éxito", f"Se generó la capa final '{final_layer.name()}'.")
            return True, cuenca_rasters

        except Exception as e:
            print(f"✘ Error durante la unión/disolución/limpieza: {e}")
            QMessageBox.critical(self.parent, "Error durante la unión o disolución", str(e))
            return False, []


# ======== SELECCIÓN DE CAPA DRAINAGE DIRECTION ========
parent = iface.mainWindow() if iface is not None else None

raster_layers = [layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsRasterLayer)]
layer_names = [layer.name() for layer in raster_layers]

if not raster_layers:
    print("✘ No hay capas raster cargadas.")
    QMessageBox.critical(parent, "Error", "No hay capas raster cargadas en el proyecto.")
else:
    layer_name, ok = QInputDialog.getItem(parent, "Seleccionar capa DRAINAGE DIRECTION", "Capa raster:", layer_names, 0, False)

    if ok and layer_name:
        selected_layer = next((lyr for lyr in raster_layers if lyr.name() == layer_name), None)

        if selected_layer:
            if iface is None:
                QMessageBox.critical(parent, "Error", "No se pudo acceder a 'iface'. Ejecuta esto dentro de QGIS.")
                raise Exception("iface no disponible")

            canvas = iface.mapCanvas()
            tool = RWaterOutletFromSelectedLayerTool(canvas, selected_layer, parent=parent)
            canvas.setMapTool(tool)

            print(f"✔ Herramienta activada. Capa seleccionada: {layer_name}")
            QMessageBox.information(
                parent,
                "Herramienta activada",
                f"Capa seleccionada: {layer_name}\n"
                "Haz clic para crear cuencas.\n"
                "Presiona ESC para vectorizar, unir (si aplica) y disolver el lote actual.\n"
                "Después podrás elegir si continuar o salir.\n"
                "Los ráster Cuenca_XX procesados se borrarán en cada ESC."
            )
        else:
            print("✘ No se pudo encontrar la capa seleccionada.")
            QMessageBox.critical(parent, "Error", "No se pudo encontrar la capa seleccionada.")
    else:
        print("✘ Selección de capa cancelada.")
        QMessageBox.information(parent, "Cancelado", "No se seleccionó ninguna capa.")
