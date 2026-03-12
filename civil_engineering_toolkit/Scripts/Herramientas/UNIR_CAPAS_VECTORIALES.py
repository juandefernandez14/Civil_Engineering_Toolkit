#SCRIPT para unión y orden de capas vectoriales orientados para las cuencas D y V, con las áreas calculadas.
#Coge todas las capas de una carpeta.
#Para que se ordenen bien, las primeras D y V deberán tener un 0 delante en el nombre de las capas. 
#Por ejemplo la 1 será 01.



import os
import processing
from qgis.core import QgsProject, QgsVectorLayer, QgsField
from PyQt5.QtCore import QVariant
from qgis.PyQt.QtWidgets import QFileDialog

# Función para abrir el cuadro de diálogo para seleccionar la carpeta
def seleccionar_carpeta():
    carpeta = QFileDialog.getExistingDirectory(None, "Seleccionar carpeta", "", QFileDialog.ShowDirsOnly)
    return carpeta

# Abrir el cuadro de diálogo para seleccionar la carpeta
folder_path = seleccionar_carpeta()

if not folder_path:
    print("No se seleccionó ninguna carpeta.")
else:
    # Listar todos los archivos en la carpeta
    vector_files = [f for f in os.listdir(folder_path) if f.endswith('.shp')]  # Filtrar archivos .shp

    # Verificar si hay capas vectoriales en la carpeta
    if not vector_files:
        print("No se encontraron capas vectoriales en la carpeta.")
    else:
        # Cargar las capas vectoriales
        layers = []
        for file in vector_files:
            layer_path = os.path.join(folder_path, file)
            layer_name = os.path.splitext(file)[0]  # Eliminar el sufijo .shp
            layer = QgsVectorLayer(layer_path, layer_name, "ogr")
            if layer.isValid():
                layers.append(layer)
            else:
                print(f"No se pudo cargar la capa: {file}")
        
        if not layers:
            print("No se cargaron capas válidas.")
        else:
            # Parámetros para la herramienta de fusión
            params = {
                'LAYERS': layers,  # Lista de capas vectoriales a fusionar
                'CRS': layers[0].crs(),  # Usar el CRS de la primera capa
                'OUTPUT': 'memory:'  # Resultado temporal en memoria
            }

            # Ejecutar la herramienta de fusión
            result = processing.run("native:mergevectorlayers", params)

            # Obtener la capa fusionada
            merged_layer = result['OUTPUT']

            # Renombrar la capa fusionada
            merged_layer.setName("CUENCAS")

            # Agregar la capa fusionada al proyecto
            QgsProject.instance().addMapLayer(merged_layer)

            print(f"{len(layers)} capas fusionadas exitosamente.")

            # **Añadir el campo para el área**
            merged_layer.startEditing()
            
            # Verificar si ya existe el campo 'area' y eliminar si es necesario
            if "area" in [field.name() for field in merged_layer.fields()]:
                idx_to_remove = merged_layer.fields().indexFromName("area")
                merged_layer.dataProvider().deleteAttributes([idx_to_remove])
                merged_layer.updateFields()

            # Crear el campo 'area' para almacenar el área con 2 decimales
            merged_layer.dataProvider().addAttributes([QgsField("area", QVariant.Double)])
            merged_layer.updateFields()

            # Obtener el índice del nuevo campo 'area'
            idx_area = merged_layer.fields().indexFromName("area")

            # Calcular y actualizar el área de cada entidad con 2 decimales
            for feature in merged_layer.getFeatures():
                area = feature.geometry().area()  # Calcular el área
                rounded_area = round(area, 2)  # Redondear el área a 2 decimales
                merged_layer.changeAttributeValue(feature.id(), idx_area, rounded_area)  # Actualizar el valor

            # Guardar los cambios
            merged_layer.commitChanges()

            print("Áreas calculadas y guardadas en el campo 'area' con 2 decimales.")

            # **Eliminar todos los campos excepto 'layer' y 'area'**
            merged_layer.startEditing()
            fields_to_keep = ["layer", "area"]
            fields_to_delete = [field.name() for field in merged_layer.fields() if field.name() not in fields_to_keep]
            
            if fields_to_delete:
                merged_layer.dataProvider().deleteAttributes([merged_layer.fields().indexFromName(f) for f in fields_to_delete])
                merged_layer.updateFields()
                print("Se eliminaron los campos innecesarios, dejando solo 'layer' y 'area'.")
            
            merged_layer.commitChanges()

