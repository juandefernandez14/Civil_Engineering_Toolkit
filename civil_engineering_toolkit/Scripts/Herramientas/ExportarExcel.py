# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QCheckBox, QPushButton, QFileDialog, QMessageBox
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsVectorLayer

# -------------------------
# Dependencia Excel
# -------------------------
try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
except Exception as e:
    raise Exception(
        "No se pudo importar openpyxl.\n"
        "Instálalo en el Python de QGIS o pide una versión a CSV.\n"
        f"Detalle: {e}"
    )

# -------------------------
# Diálogo de selección
# -------------------------
class ExportLayersDialog(QDialog):
    def __init__(self, layers):
        super().__init__()
        self.setWindowTitle("Exportar capas a Excel")
        self.layers = layers
        self.output_path = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Selecciona las capas vectoriales a exportar:"))

        self.listw = QListWidget()
        self.listw.setSelectionMode(QListWidget.NoSelection)

        for lyr in layers:
            item = QListWidgetItem(lyr.name())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.listw.addItem(item)

        layout.addWidget(self.listw)

        self.chk_wkt = QCheckBox("Incluir geometría (WKT) en una columna 'WKT'")
        self.chk_wkt.setChecked(False)
        layout.addWidget(self.chk_wkt)

        # Botones
        btn_row = QHBoxLayout()

        self.btn_path = QPushButton("Elegir archivo .xlsx…")
        self.btn_path.clicked.connect(self.pick_path)
        btn_row.addWidget(self.btn_path)

        self.btn_ok = QPushButton("Exportar")
        self.btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_ok)

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)

    def pick_path(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar Excel",
            "",
            "Excel (*.xlsx)"
        )
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self.output_path = path

    def selected_layers(self):
        selected = []
        for i, lyr in enumerate(self.layers):
            if self.listw.item(i).checkState() == Qt.Checked:
                selected.append(lyr)
        return selected

# -------------------------
# Utilidades Excel
# -------------------------
def safe_sheet_name(name: str) -> str:
    # Excel: máximo 31 caracteres y sin []:*?/\
    bad = ['[', ']', ':', '*', '?', '/', '\\']
    for b in bad:
        name = name.replace(b, "_")
    name = name.strip()
    return name[:31] if len(name) > 31 else name

def autosize_columns(ws, max_rows_scan=200):
    # Auto-ajuste ligero (escaneando solo primeras filas para rendimiento)
    for col in range(1, ws.max_column + 1):
        max_len = 0
        col_letter = get_column_letter(col)
        for row in range(1, min(ws.max_row, max_rows_scan) + 1):
            v = ws.cell(row=row, column=col).value
            if v is None:
                continue
            max_len = max(max_len, len(str(v)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)

# -------------------------
# Exportación
# -------------------------
def export_layers_to_excel(layers, output_path, include_wkt=False):
    wb = Workbook()
    # hoja inicial vacía -> la eliminamos al final si no se usa
    default_ws = wb.active
    default_ws.title = "README"

    used_sheets = 0

    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer):
            continue

        fields = lyr.fields()
        headers = [f.name() for f in fields]
        if include_wkt:
            headers.append("WKT")

        ws = wb.create_sheet(title=safe_sheet_name(lyr.name()))
        used_sheets += 1

        # Escribir cabecera
        ws.append(headers)

        # Escribir filas
        # Nota: para capas grandes puede tardar; esto es “directo” y estable.
        for feat in lyr.getFeatures():
            row = [feat[f.name()] for f in fields]
            if include_wkt:
                geom = feat.geometry()
                row.append(geom.asWkt() if geom and not geom.isEmpty() else None)
            ws.append(row)

        # Congelar cabecera
        ws.freeze_panes = "A2"
        autosize_columns(ws)

    # Quitar hoja por defecto si no se usa
    if used_sheets > 0 and "README" in wb.sheetnames:
        wb.remove(wb["README"])
    else:
        default_ws["A1"] = "No se exportó ninguna capa."

    wb.save(output_path)

# -------------------------
# MAIN
# -------------------------
all_layers = list(QgsProject.instance().mapLayers().values())
vector_layers = [l for l in all_layers if isinstance(l, QgsVectorLayer)]

if not vector_layers:
    QMessageBox.critical(None, "Error", "No hay capas vectoriales cargadas en el proyecto.")
else:
    dlg = ExportLayersDialog(vector_layers)
    if dlg.exec_() == QDialog.Accepted:
        selected = dlg.selected_layers()
        if not selected:
            QMessageBox.warning(None, "Aviso", "No seleccionaste ninguna capa.")
        elif not dlg.output_path:
            QMessageBox.warning(None, "Aviso", "No elegiste un archivo de salida .xlsx.")
        else:
            try:
                export_layers_to_excel(selected, dlg.output_path, include_wkt=dlg.chk_wkt.isChecked())
                QMessageBox.information(None, "OK", f"Excel exportado correctamente:\n{dlg.output_path}")
            except Exception as e:
                QMessageBox.critical(None, "Error", f"No se pudo exportar:\n{e}")
