#Antes de ejecutar el SCRIPT
#Tener cargadas en el proyecto las capas de:VALLADO, USOS_SIOSE/CORINE, MDT a utilizar y la capa de CUENCAS
#Para la unión tener cargada la hoja de excel de los usos del SIOSE/CORINE

from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox, QFileDialog
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsProcessingFeatureSourceDefinition, Qgis, QgsField, edit,
    QgsRasterLayer, QgsProcessing, QgsVectorLayerJoinInfo, QgsVectorFileWriter,
)
from PyQt5.QtCore import QVariant
import processing

def print_message(tipo, mensaje):
    icons = {
        "info": "ℹ️",
        "success": "✅",
        "error": "❌"
    }
    print(f"{icons.get(tipo, 'ℹ️')} {mensaje}")

def seleccionar_capa(mensaje, tipo="vector"):
    capas = QgsProject.instance().mapLayers().values()
    if tipo == "vector":
        capas_filtradas = [c for c in capas if isinstance(c, QgsVectorLayer)]
    else:
        capas_filtradas = [c for c in capas if isinstance(c, QgsRasterLayer)]

    nombres = [c.name() for c in capas_filtradas]

    if not nombres:
        QMessageBox.critical(None, "Error", f"No se encontraron capas de tipo {tipo} en el proyecto.")
        raise Exception("Sin capas disponibles")

    seleccion, ok = QInputDialog.getItem(None, mensaje, "Selecciona una capa:", nombres, 0, False)
    if not ok:
        raise Exception("Selección cancelada por el usuario")

    return next(c for c in capas_filtradas if c.name() == seleccion)

capas_dialogo = []

# === Seleccionar capas ya cargadas en el proyecto ===
cuencas_layer = seleccionar_capa("Seleccionar la capa de CUENCAS", "vector")
siose_layer = seleccionar_capa("Seleccionar la capa SIOSE/CORINE", "vector")
vallado_layer = seleccionar_capa("Seleccionar la capa VALLADO", "vector")
mdt_layer = seleccionar_capa("Seleccionar el MDT (raster)", "raster")

# === Procesamiento ===
capas_dialogo.append(cuencas_layer)
print_message("success", "Capa CUENCAS seleccionada correctamente")

buffer_result = processing.run("native:buffer", {
    'INPUT': QgsProcessingFeatureSourceDefinition(cuencas_layer.source(), selectedFeaturesOnly=False),
    'DISTANCE': 100,
    'SEGMENTS': 10,
    'DISSOLVE': True,
    'OUTPUT': 'memory:'
})
buffer_layer = buffer_result['OUTPUT']
buffer_layer.setName("Buffer100m")
QgsProject.instance().addMapLayer(buffer_layer)
print_message("success", "Buffer de 100m generado con disolución")

# Clip con SIOSE/CORINE
clip_result = processing.run("native:clip", {
    'INPUT': siose_layer,
    'OVERLAY': buffer_layer,
    'OUTPUT': 'memory:'
})
clip_layer = clip_result['OUTPUT']
clip_layer.setName("Usos_SIOSE_CORINE")
QgsProject.instance().addMapLayer(clip_layer)
print_message("success", "Clip realizado con éxito")

# Procesar Vallado
polygons_result = processing.run("native:polygonize", {
    'INPUT': vallado_layer,
    'OUTPUT': 'memory:'
})
polygons_layer = polygons_result['OUTPUT']
polygons_layer.setName("Polígonos_Vallado")
QgsProject.instance().addMapLayer(polygons_layer)

dissolve_result = processing.run("native:dissolve", {
    'INPUT': polygons_layer,
    'FIELD': [],
    'OUTPUT': 'memory:'
})
dissolved_layer = dissolve_result['OUTPUT']
dissolved_layer.setName("Polígonos_Vallado_Disolución")
QgsProject.instance().addMapLayer(dissolved_layer)

field = QgsField("Layer", QVariant.String)
dissolved_layer.dataProvider().addAttributes([field])
dissolved_layer.updateFields()

with edit(dissolved_layer):
    for f in dissolved_layer.getFeatures():
        f["Layer"] = "Vallado"
        dissolved_layer.updateFeature(f)
print_message("success", "Líneas convertidas a polígonos y campo 'Layer' agregado")

# Unión de uso + vallado
union_result = processing.run("native:union", {
    'INPUT': clip_layer,
    'OVERLAY': dissolved_layer,
    'OUTPUT': 'memory:'
})
union_layer = union_result['OUTPUT']
union_layer.setName("Usos_SIOSE_CORINE_VALLADO")
QgsProject.instance().addMapLayer(union_layer)

# Filtrar campos
keep_fields = ['CODIIGE', 'CODE_18', 'Layer']
drop_fields = [f.name() for f in union_layer.fields() if f.name() not in keep_fields]
union_layer.dataProvider().deleteAttributes([union_layer.fields().indexFromName(f) for f in drop_fields])
union_layer.updateFields()

# Añadir campos adicionales
fields_to_add = [
    QgsField("Uso", QVariant.String),
    QgsField("P0i_>3", QVariant.Int),
    QgsField("P0i_<3", QVariant.Int),
    QgsField("Area_zona", QVariant.Double)
]
union_layer.dataProvider().addAttributes(fields_to_add)
union_layer.updateFields()

with edit(union_layer):
    for f in union_layer.getFeatures():
        f["Uso"] = "Desconocido"
        f["P0i_>3"] = 0
        f["P0i_<3"] = 0
        f["Area_zona"] = 0.0
        union_layer.updateFeature(f)

print_message("success", "Campos agregados y actualizados en unión")

# Intersección
intersection_result = processing.run("native:intersection", {
    'INPUT': cuencas_layer,
    'OVERLAY': union_layer,
    'OUTPUT': 'memory:'
})
intersection_layer = intersection_result['OUTPUT']
intersection_layer.setName("Intersección_CUENCAS_USOS")
QgsProject.instance().addMapLayer(intersection_layer)

# MDT: calcular pendiente
if mdt_layer.isValid():
    slope_result = processing.run("gdal:slope", {
        'INPUT': mdt_layer,
        'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
    })
    slope_layer = QgsRasterLayer(slope_result['OUTPUT'], "Slope")
    if slope_layer.isValid():
        QgsProject.instance().addMapLayer(slope_layer)
        print_message("success", "Cálculo de pendiente realizado")
    else:
        print_message("error", "No se pudo cargar la capa de pendiente")

# Zonal statistics
if QgsProject.instance().mapLayersByName("Intersección_CUENCAS_USOS") and QgsProject.instance().mapLayersByName("Slope"):
    intersection_layer = QgsProject.instance().mapLayersByName("Intersección_CUENCAS_USOS")[0]
    slope_layer = QgsProject.instance().mapLayersByName("Slope")[0]

    zonal_result = processing.run("qgis:zonalstatisticsfb", {
        'INPUT': intersection_layer,
        'INPUT_RASTER': slope_layer,
        'STATISTICS': [2],  # Mean
        'OUTPUT': 'memory:'
    })

    zonal_layer = zonal_result['OUTPUT']
    zonal_layer.setName("USOSxCUENCAS")
    QgsProject.instance().addMapLayer(zonal_layer)

    # Agregar y actualizar campos
    if "Area_zona" not in [f.name() for f in zonal_layer.fields()]:
        zonal_layer.dataProvider().addAttributes([QgsField("Area_zona", QVariant.Double)])
        zonal_layer.updateFields()

    with edit(zonal_layer):
        for f in zonal_layer.getFeatures():
            f["Area_zona"] = round(f.geometry().area(), 2)
            zonal_layer.updateFeature(f)

    if "P0i" not in [f.name() for f in zonal_layer.fields()]:
        zonal_layer.dataProvider().addAttributes([QgsField("P0i", QVariant.Int)])
        zonal_layer.updateFields()

    with edit(zonal_layer):
        for f in zonal_layer.getFeatures():
            f["P0i"] = 1
            zonal_layer.updateFeature(f)

    print_message("success", "Campos 'Area_zona' y 'P0i' actualizados correctamente")

print_message("success", "Campos 'Area_zona' y 'P0i' actualizados correctamente")

        # === ACTUALIZAR CODE_18 o CODIIGE A 0 SI Layer_2 NO ES NULO ===
field_names = [f.name() for f in zonal_layer.fields()]
field_to_update = "CODE_18" if "CODE_18" in field_names else "CODIIGE" if "CODIIGE" in field_names else None

if field_to_update and "Layer_2" in field_names:
            updated = 0
            with edit(zonal_layer):
                for f in zonal_layer.getFeatures():
                    if f["Layer_2"] is not None and str(f["Layer_2"]).strip() != "":
                        f[field_to_update] = 0  # valor numérico
                        zonal_layer.updateFeature(f)
                        updated += 1

            print_message("success", f"Campo '{field_to_update}' actualizado con 0 en {updated} registros con 'Layer_2'")


else:
                print_message("error", "No se pudo realizar el análisis zonal. Verifica las capas.")

def get_layer_by_name(name):
    for lyr in QgsProject.instance().mapLayers().values():
        if lyr.name() == name:
            return lyr
    return None

# Preguntar si se desea continuar
respuesta = QMessageBox.question(None, "Confirmar ejecución",
                                 "¿Deseas continuar con la unión de la tabla de usos?",
                                 QMessageBox.Yes | QMessageBox.No)

if respuesta != QMessageBox.Yes:
    print("🚫 El usuario canceló la ejecución del script.")
else:
    print("🟢 Iniciando script de unión de capas...")

    usos_layer_name = "USOSxCUENCAS"
    usos_layer = get_layer_by_name(usos_layer_name)

    if not usos_layer:
        QMessageBox.critical(None, "Error", f"No se encontró la capa '{usos_layer_name}'. Asegúrate de que esté cargada en el proyecto.")
        raise Exception(f"Capa '{usos_layer_name}' no encontrada.")

    print(f"✅ Capa base seleccionada: {usos_layer_name}")
    print(f"Campos capa base: {[f.name() for f in usos_layer.fields()]}")

    # Obtener nombres de capas cargadas
    layers = QgsProject.instance().mapLayers().values()
    layer_names = [layer.name() for layer in layers if layer.name() != usos_layer_name]

    # Selección de tabla de unión
    join_layer_name, ok = QInputDialog.getItem(None, "Seleccionar tabla de unión", "Selecciona la tabla para unir:", layer_names, 0, False)
    if not ok:
        print("❌ Proceso cancelado al seleccionar tabla de unión.")
        raise Exception("Cancelado por el usuario")
    join_layer = get_layer_by_name(join_layer_name)
    print(f"✅ Tabla de unión seleccionada: {join_layer_name}")
    print(f"Campos tabla unión: {[f.name() for f in join_layer.fields()]}")

    # Selección fuente CORINE/SIOSE
    tipo_fuente, ok = QInputDialog.getItem(None, "Fuente de datos", "Seleccionar fuente:", ["CORINE", "SIOSE"], 0, False)
    if not ok:
        print("❌ Proceso cancelado al seleccionar fuente.")
        raise Exception("Cancelado por el usuario")

    if tipo_fuente == "CORINE":
        field_usos = "CODE_18"
        field_join = "CODIGOS_CLC"
    else:
        field_usos = "CODIIGE"
        field_join = "CODIGOS_SIOSE"

    print(f"ℹ️ Fuente seleccionada: {tipo_fuente} → Target field: {field_usos} | Join field: {field_join}")

    # Selección tipo de uso
    uso_tipo, ok = QInputDialog.getItem(None, "Uso de suelo", "Seleccionar tipo de uso:", ["A", "B", "C", "D"], 0, False)
    if not ok:
        print("❌ Proceso cancelado al seleccionar tipo de uso.")
        raise Exception("Cancelado por el usuario")

    campo_1 = f"{uso_tipo}_P0i_>3"
    campo_2 = f"{uso_tipo}_P0i_<3"
    print(f"ℹ️ Campos POi seleccionados: {campo_1}, {campo_2}")

    # Validación de campos
    campos_base = [f.name() for f in usos_layer.fields()]
    if field_usos not in campos_base:
        raise Exception(f"❌ El campo '{field_usos}' NO existe en la capa base '{usos_layer_name}'.")

    campos_tabla = [f.name() for f in join_layer.fields()]
    for campo in [field_join, "USOS_IC", campo_1, campo_2]:
        if campo not in campos_tabla:
            raise Exception(f"❌ El campo '{campo}' NO existe en la tabla unión '{join_layer_name}'.")

    print("✅ Todos los campos requeridos existen. Procediendo con la unión...")

    # Crear y aplicar la unión
    join_info = QgsVectorLayerJoinInfo()
    join_info.setJoinLayer(join_layer)
    join_info.setJoinFieldName(field_join)
    join_info.setTargetFieldName(field_usos)
    join_info.setUsingMemoryCache(True)
    join_info.setPrefix("")
    join_info.setJoinFieldNamesSubset(["USOS_IC", campo_1, campo_2])
    usos_layer.addJoin(join_info)

    print(f"🎉 Unión agregada correctamente.")

    QMessageBox.information(None, "Unión completada",
        f"Unión agregada correctamente:\n"
        f"Capa base: {usos_layer_name}\n"
        f"Tabla unión: {join_layer_name}\n"
        f"Join field (tabla unión): {field_join}\n"
        f"Target field (capa base): {field_usos}\n"
        f"Campos unidos: USOS_IC, {campo_1}, {campo_2}")

    # Exportar con nombre personalizado
    output_path, _ = QFileDialog.getSaveFileName(None, "Guardar shapefile como...", f"{usos_layer_name}_union.shp", "Shapefile (*.shp)")
    if not output_path:
        print("⚠️ Exportación cancelada: no se seleccionó archivo.")
        QMessageBox.warning(None, "Exportación cancelada", "No se seleccionó ningún archivo. El archivo no fue guardado.")
    else:
        if not output_path.lower().endswith(".shp"):
            output_path += ".shp"
        print(f"ℹ️ Exportando capa a: {output_path}")

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "ESRI Shapefile"
        options.fileEncoding = "UTF-8"

        error = QgsVectorFileWriter.writeAsVectorFormatV2(
            usos_layer,
            output_path,
            QgsProject.instance().transformContext(),
            options
        )

        if error[0] == QgsVectorFileWriter.NoError:
            print(f"✅ Exportación completada con éxito en:\n  {output_path} 🎉")
            QMessageBox.information(None, "Exportación exitosa", f"La capa se exportó correctamente a:\n{output_path}")

            nueva_capa = QgsVectorLayer(output_path, f"{usos_layer_name}_union", "ogr")
            if nueva_capa.isValid():
                QgsProject.instance().addMapLayer(nueva_capa)
                print(f"✅ Capa exportada cargada correctamente en el proyecto.")
                QMessageBox.information(None, "Capa cargada", "La capa exportada se ha cargado en el proyecto.")
            else:
                print(f"❌ La capa exportada NO es válida y no se pudo cargar.")
                QMessageBox.warning(None, "Carga fallida", "La capa exportada no es válida, no se pudo cargar en el proyecto.")
        else:
            print(f"❌ Error en exportación: {error}")
            QMessageBox.critical(None, "Error de exportación", f"No se pudo exportar la capa.\nError: {error}")

        # Actualizar campo P0i
        print("🔄 Actualizando campo P0i en la nueva capa...")

        campo_mean = "_mean"
        if campo_mean not in [f.name() for f in nueva_capa.fields()]:
            QMessageBox.warning(None, "Campo faltante", f"No se encontró el campo '{campo_mean}' en la capa exportada.")
            raise Exception(f"Campo '{campo_mean}' no encontrado en la capa exportada.")

        with edit(nueva_capa):
            for feature in nueva_capa.getFeatures():
                mean_val = feature[campo_mean]
                if mean_val is None:
                    continue

                new_val = feature[campo_1] if mean_val > 3 else feature[campo_2]
                feature["P0i"] = new_val
                nueva_capa.updateFeature(feature)

        print("✅ Campo 'P0i' actualizado exitosamente.")
        QMessageBox.information(None, "Campo actualizado", "El campo 'P0i' ha sido actualizado correctamente según el valor de '_mean'.")

        # Eliminar campos 'Uso', 'P0i_>3', 'P0i_<3'
        campos_eliminar = ["Uso", "P0i_>3", "P0i_<3"]
        campos_existentes = [f.name() for f in nueva_capa.fields()]
        campos_a_eliminar = [c for c in campos_eliminar if c in campos_existentes]

        with edit(nueva_capa):
            for campo in campos_a_eliminar:
                idx = nueva_capa.fields().indexOf(campo)
                if idx != -1:
                    nueva_capa.deleteAttribute(idx)
            nueva_capa.updateFields()

        print(f"✅ Campos eliminados: {campos_a_eliminar}")

    print("✔️ Script finalizado correctamente.")
