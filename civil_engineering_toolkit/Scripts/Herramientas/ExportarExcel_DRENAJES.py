# -*- coding: utf-8 -*-
# Exportador específico de DRENAJES a Excel


from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QCheckBox, QPushButton, QFileDialog, QMessageBox, QDialogButtonBox
)
from qgis.core import QgsProject, QgsVectorLayer

try:
    from qgis.utils import iface
except Exception:
    iface = None

parent = iface.mainWindow() if iface else None

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import PatternFill, Font, Alignment
except Exception as e:
    raise Exception(
        "No se pudo importar openpyxl.\n"
        "Instálalo en el Python de QGIS o pide una versión a CSV.\n"
        f"Detalle: {e}"
    )

try:
    from qgis.core import NULL
except Exception:
    NULL = None

# -------------------------
# Utilidades
# -------------------------
def safe_sheet_name(name: str) -> str:
    bad = ['[', ']', ':', '*', '?', '/', '\\']
    for b in bad:
        name = name.replace(b, "_")
    name = name.strip()
    return name[:31] if len(name) > 31 else name

def autosize_columns(ws, max_rows_scan=300):
    for col in range(1, ws.max_column + 1):
        max_len = 0
        col_letter = get_column_letter(col)
        for row in range(1, min(ws.max_row, max_rows_scan) + 1):
            v = ws.cell(row=row, column=col).value
            if v is None:
                continue
            max_len = max(max_len, len(str(v)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)

def autosize_all_sheets(wb):
    for ws in wb.worksheets:
        autosize_columns(ws)

def normalize_for_excel(val):
    if val is None:
        return 0
    if NULL is not None and val == NULL:
        return 0
    try:
        import math
        if isinstance(val, float) and math.isnan(val):
            return 0
    except Exception:
        pass
    return val

def to_float_or_none(v):
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", ".")
        if s == "":
            return None
        return float(s)
    except Exception:
        return None

def natural_key(s):
    import re
    if s is None:
        return ("", 10**12, "")
    s = str(s)
    m = re.match(r"^([A-Za-z]+)\s*0*([0-9]+)(.*)$", s)
    if m:
        return (m.group(1).upper(), int(m.group(2)), m.group(3))
    return (s.upper(), 10**12, "")

def col_letter_to_index(letter: str) -> int:
    letter = str(letter).strip().upper()
    n = 0
    for ch in letter:
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - ord("A") + 1)
    return n

def find_col_by_header(ws, header_name: str):
    target = str(header_name).strip().lower()
    for c in range(1, ws.max_column + 1):
        h = ws.cell(row=1, column=c).value
        if h is None:
            continue
        if str(h).strip().lower() == target:
            return c
    return None

# -------------------------
# Post 1: CUENCAS area -> area_km2 (amarillo)
# -------------------------
def add_km2_column_to_cuencas_sheet(wb, cuencas_sheet_name="CUENCAS", area_field_name="area"):
    if cuencas_sheet_name not in wb.sheetnames:
        return
    ws = wb[cuencas_sheet_name]

    area_col = find_col_by_header(ws, area_field_name)
    if area_col is None:
        return

    new_col = area_col + 1
    ws.insert_cols(new_col, 1)

    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    header_font = Font(bold=True)
    center = Alignment(horizontal="center")

    ws.cell(row=1, column=new_col).value = "area_km2"
    ws.cell(row=1, column=new_col).fill = yellow
    ws.cell(row=1, column=new_col).font = header_font
    ws.cell(row=1, column=new_col).alignment = center

    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=area_col).value
        try:
            km2 = 0 if v is None else float(v) / 1_000_000.0
        except Exception:
            km2 = 0
        cell = ws.cell(row=r, column=new_col)
        cell.value = km2
        cell.fill = yellow

# -------------------------
# Post 2: CAUCES layout + columnas amarillas (KM, DIFF, longitud_der)
# -------------------------
def format_cauces_sheet(wb, sheet_name="CAUCES"):
    if sheet_name not in wb.sheetnames:
        return

    ws = wb[sheet_name]
    if ws.max_row < 2:
        return

    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    bold = Font(bold=True)
    center = Alignment(horizontal="center")

    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]

    def find_header_idx(candidates):
        cands = [x.lower() for x in candidates]
        for i, h in enumerate(headers, start=1):
            if h is None:
                continue
            if str(h).strip().lower() in cands:
                return i
        return None

    name_col = find_header_idx(["cuenca", "cauce", "name", "nombre", "id"]) or 1
    long_col = find_header_idx(["longitud", "length", "len"])
    min_col  = find_header_idx(["min"])
    max_col  = find_header_idx(["max"])
    diff_col = find_header_idx(["diff", "dif", "diferencia"])
    pte_col  = find_header_idx(["pte", "pendiente", "slope"])

    groups = {}
    for r in range(2, ws.max_row + 1):
        nm = ws.cell(row=r, column=name_col).value
        if nm is None or nm == 0:
            continue
        nm = str(nm).strip()
        if not nm:
            continue

        def get(c):
            return ws.cell(row=r, column=c).value if c else None

        rec = {"NAME": nm, "LONG": get(long_col), "MIN": get(min_col),
               "MAX": get(max_col), "DIFF": get(diff_col), "PTE": get(pte_col)}

        if "_" in nm:
            base = nm.split("_", 1)[0]
            groups.setdefault(base, {"main": None, "vars": []})["vars"].append(rec)
        else:
            base = nm
            groups.setdefault(base, {"main": None, "vars": []})["main"] = rec

    bases = sorted(groups.keys(), key=natural_key)

    ws.delete_rows(1, ws.max_row)
    ws.delete_cols(1, ws.max_column)

    left_headers = ["Cuenca", "longitud", "KM", "MIN", "MAX", "DIFF", "PTE"]
    right_headers = ["Cuenca", "longitud", "MIN", "MAX", "DIFF", "PTE"]

    gap_cols = 2
    left_start = 1
    right_start = left_start + len(left_headers) + gap_cols

    for i, h in enumerate(left_headers):
        cell = ws.cell(row=1, column=left_start + i, value=h)
        cell.font = bold
        cell.alignment = center

    for i, h in enumerate(right_headers):
        cell = ws.cell(row=1, column=right_start + i, value=h)
        cell.font = bold
        cell.alignment = center

    out_r = 2
    for base in bases:
        g = groups[base]
        main = g["main"]
        vars_list = g["vars"]
        first_var = vars_list[0] if vars_list else None

        def write_left(rec, row_idx):
            ws.cell(row=row_idx, column=left_start + 0, value=(rec["NAME"] if rec else None))
            vlong = rec["LONG"] if rec else None
            ws.cell(row=row_idx, column=left_start + 1, value=vlong)

            if rec:
                v = to_float_or_none(vlong)
                km = (v / 1000.0) if v is not None else 0
                ws.cell(row=row_idx, column=left_start + 2, value=km)
            else:
                ws.cell(row=row_idx, column=left_start + 2, value=None)

            ws.cell(row=row_idx, column=left_start + 3, value=(rec["MIN"] if rec else None))
            ws.cell(row=row_idx, column=left_start + 4, value=(rec["MAX"] if rec else None))
            ws.cell(row=row_idx, column=left_start + 5, value=(rec["DIFF"] if rec else None))
            ws.cell(row=row_idx, column=left_start + 6, value=(rec["PTE"] if rec else None))

        def write_right(rec, row_idx):
            ws.cell(row=row_idx, column=right_start + 0, value=(rec["NAME"] if rec else None))
            ws.cell(row=row_idx, column=right_start + 1, value=(rec["LONG"] if rec else None))
            ws.cell(row=row_idx, column=right_start + 2, value=(rec["MIN"] if rec else None))
            ws.cell(row=row_idx, column=right_start + 3, value=(rec["MAX"] if rec else None))
            ws.cell(row=row_idx, column=right_start + 4, value=(rec["DIFF"] if rec else None))
            ws.cell(row=row_idx, column=right_start + 5, value=(rec["PTE"] if rec else None))

        write_left(main, out_r)
        write_right(first_var, out_r)
        out_r += 1

        for extra in vars_list[1:]:
            write_left(None, out_r)
            write_right(extra, out_r)
            out_r += 1

    # Columnas amarillas: KM (izq), DIFF (izq), longitud (der)
    extra_start = right_start + len(right_headers) + gap_cols
    extra_headers = ["KM", "DIFF", "longitud"]
    for i, h in enumerate(extra_headers):
        cell = ws.cell(row=1, column=extra_start + i, value=h)
        cell.font = bold
        cell.alignment = center
        cell.fill = yellow

    last_row = out_r - 1
    for r in range(2, last_row + 1):
        v_km = ws.cell(row=r, column=left_start + 2).value
        v_diff = ws.cell(row=r, column=left_start + 5).value
        v_long_right = ws.cell(row=r, column=right_start + 1).value

        ws.cell(row=r, column=extra_start + 0, value=v_km).fill = yellow
        ws.cell(row=r, column=extra_start + 1, value=v_diff).fill = yellow
        ws.cell(row=r, column=extra_start + 2, value=v_long_right).fill = yellow

# -------------------------
# Post 3: UNIONxCUENCAS copiar columnas desde M y en amarillo
# -------------------------
def copy_columns_unionxcuencas_fixed(wb, sheet_name="UNIONxCUENCAS",
                                     cols_to_copy=None, start_col_letter="M"):
    if cols_to_copy is None:
        cols_to_copy = ["layer", "P0i", "USOS_IC", "Area_zona"]

    if sheet_name not in wb.sheetnames:
        return
    ws = wb[sheet_name]
    if ws.max_row < 2:
        return

    start_col = col_letter_to_index(start_col_letter)

    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    bold = Font(bold=True)

    header_to_idx = {}
    for c in range(1, ws.max_column + 1):
        h = ws.cell(row=1, column=c).value
        if h is None:
            continue
        header_to_idx[str(h).strip().lower()] = c

    src_cols = [header_to_idx.get(str(h).strip().lower()) for h in cols_to_copy]

    for i, h in enumerate(cols_to_copy):
        cell = ws.cell(row=1, column=start_col + i, value=h)
        cell.font = bold
        cell.fill = yellow

    for r in range(2, ws.max_row + 1):
        for i, src in enumerate(src_cols):
            v = ws.cell(row=r, column=src).value if src else None
            cell = ws.cell(row=r, column=start_col + i, value=v)
            cell.fill = yellow

# -------------------------
# Post 4: Crear hoja Resultados con layout exacto
# -------------------------
def build_resultados_sheet(wb,
                           cuencas_sheet="CUENCAS",
                           cauces_sheet="CAUCES",
                           union_sheet="UNIONxCUENCAS"):
    if "Resultados" in wb.sheetnames:
        wb.remove(wb["Resultados"])
    ws = wb.create_sheet("Resultados")

    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    bold = Font(bold=True)
    center = Alignment(horizontal="center")

    # ---- Tabla izquierda: A:E (pero NO sombrear A) ----
    left_headers = ["layer", "area_km2", "KM", "DIFF", "longitud"]
    for i, h in enumerate(left_headers, start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = bold
        cell.alignment = center
        if i != 1:  # NO sombrear columna A
            cell.fill = yellow

    left_rows = []
    if cuencas_sheet in wb.sheetnames:
        wcu = wb[cuencas_sheet]
        c_layer = find_col_by_header(wcu, "layer")
        c_area_km2 = find_col_by_header(wcu, "area_km2")
        if c_layer and c_area_km2:
            for r in range(2, wcu.max_row + 1):
                layer_val = wcu.cell(row=r, column=c_layer).value
                if layer_val is None or layer_val == 0 or str(layer_val).strip() == "":
                    continue
                left_rows.append([layer_val, wcu.cell(row=r, column=c_area_km2).value, None, None, None])

    # CAUCES: coger la ÚLTIMA ocurrencia de KM/DIFF/longitud (las amarillas finales)
    cauces_map = {}
    if cauces_sheet in wb.sheetnames:
        wca = wb[cauces_sheet]

        def find_last_col(ws_, header):
            target = header.strip().lower()
            last = None
            for c in range(1, ws_.max_column + 1):
                h = ws_.cell(row=1, column=c).value
                if h is None:
                    continue
                if str(h).strip().lower() == target:
                    last = c
            return last

        km_col = find_last_col(wca, "KM")
        diff_col = find_last_col(wca, "DIFF")
        long_col = find_last_col(wca, "longitud")
        name_col = find_col_by_header(wca, "Cuenca") or find_col_by_header(wca, "cuenca")

        if name_col and km_col and diff_col and long_col:
            for r in range(2, wca.max_row + 1):
                nm = wca.cell(row=r, column=name_col).value
                if nm is None or nm == 0:
                    continue
                nm = str(nm).strip()
                if nm == "":
                    continue
                base = nm.split("_", 1)[0]
                cauces_map[base] = [
                    wca.cell(row=r, column=km_col).value,
                    wca.cell(row=r, column=diff_col).value,
                    wca.cell(row=r, column=long_col).value
                ]

    for row in left_rows:
        base = str(row[0]).split("_", 1)[0]
        if base in cauces_map:
            row[2], row[3], row[4] = cauces_map[base]

    # Escribir tabla izquierda (datos). No sombrear A, sí B:E
    out_r_left = 2
    for row in left_rows:
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=out_r_left, column=c, value=v)
            if c != 1:
                cell.fill = yellow
        out_r_left += 1

    # ---- Tabla derecha: H:K (8..11), sin huecos (empieza fila 2) ----
    right_start = 8  # H
    right_headers = ["layer", "P0i", "USOS_IC", "Area_zona"]

    for i, h in enumerate(right_headers):
        cell = ws.cell(row=1, column=right_start + i, value=h)
        cell.font = bold
        cell.alignment = center
        cell.fill = yellow

    out_r_right = 2

    if union_sheet in wb.sheetnames:
        wun = wb[union_sheet]
        src_start = col_letter_to_index("M")  # donde ya copiamos en UNIONxCUENCAS

        for r in range(2, wun.max_row + 1):
            vals = [
                wun.cell(row=r, column=src_start + 0).value,
                wun.cell(row=r, column=src_start + 1).value,
                wun.cell(row=r, column=src_start + 2).value,
                wun.cell(row=r, column=src_start + 3).value,
            ]
            if all(v is None or v == 0 or str(v).strip() == "" for v in vals):
                continue

            for i, v in enumerate(vals):
                cell = ws.cell(row=out_r_right, column=right_start + i, value=v)
                cell.fill = yellow

            out_r_right += 1

    ws.freeze_panes = "A2"

# -------------------------
# Pestañas de color
# -------------------------
def set_tab_colors(wb):
    tab_blue = "4F81BD"
    tab_green = "9BBB59"
    tab_yellow = "F2C811"

    if "CUENCAS" in wb.sheetnames:
        wb["CUENCAS"].sheet_properties.tabColor = tab_blue
    if "CAUCES" in wb.sheetnames:
        wb["CAUCES"].sheet_properties.tabColor = tab_blue
    if "UNIONxCUENCAS" in wb.sheetnames:
        wb["UNIONxCUENCAS"].sheet_properties.tabColor = tab_green
    if "Resultados" in wb.sheetnames:
        wb["Resultados"].sheet_properties.tabColor = tab_yellow

# -------------------------
# Exportación
# -------------------------
def export_layers_to_excel(layer_map, output_path, include_wkt=False):
    wb = Workbook()
    default_ws = wb.active
    default_ws.title = "README"
    used = 0

    for sheet_label, lyr in layer_map.items():
        if lyr is None:
            continue
        if not isinstance(lyr, QgsVectorLayer):
            raise Exception(f"La capa '{sheet_label}' no es vectorial.")

        fields = lyr.fields()
        headers = [f.name() for f in fields]
        if include_wkt:
            headers.append("WKT")

        ws = wb.create_sheet(title=safe_sheet_name(sheet_label))
        used += 1
        ws.append(headers)

        for c in range(1, len(headers) + 1):
            ws.cell(row=1, column=c).font = Font(bold=True)

        for feat in lyr.getFeatures():
            row = [normalize_for_excel(feat[f.name()]) for f in fields]
            if include_wkt:
                geom = feat.geometry()
                row.append(geom.asWkt() if geom and not geom.isEmpty() else 0)
            ws.append(row)

        ws.freeze_panes = "A2"

    if used > 0 and "README" in wb.sheetnames:
        wb.remove(wb["README"])
    else:
        default_ws["A1"] = "No se exportó ninguna capa."

    # Post-procesos
    add_km2_column_to_cuencas_sheet(wb, "CUENCAS", "area")
    format_cauces_sheet(wb, "CAUCES")
    copy_columns_unionxcuencas_fixed(
        wb,
        sheet_name="UNIONxCUENCAS",
        cols_to_copy=["layer", "P0i", "USOS_IC", "Area_zona"],
        start_col_letter="M"
    )
    build_resultados_sheet(wb, "CUENCAS", "CAUCES", "UNIONxCUENCAS")
    set_tab_colors(wb)

    # Autoajuste de columnas en TODAS las hojas
    autosize_all_sheets(wb)

    wb.save(output_path)

# -------------------------
# Diálogo específico drenajes
# -------------------------
class DrainageExportDialog(QDialog):
    def __init__(self, vector_layers):
        super().__init__(parent)
        self.setWindowTitle("Exportar Drenajes a Excel")
        self.output_path = None

        self.layers = vector_layers
        self.name_to_layer = {l.name(): l for l in self.layers}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Selecciona las capas a exportar:"))

        def make_combo():
            cb = QComboBox()
            cb.addItem("— seleccionar —")
            for nm in sorted(self.name_to_layer.keys()):
                cb.addItem(nm)
            return cb

        layout.addWidget(QLabel("CUENCAS (obligatoria):"))
        self.cb_cuencas = make_combo()
        layout.addWidget(self.cb_cuencas)

        layout.addWidget(QLabel("CAUCES (obligatoria):"))
        self.cb_cauces = make_combo()
        layout.addWidget(self.cb_cauces)

        layout.addWidget(QLabel("UNIONxCUENCAS (obligatoria):"))
        self.cb_union = make_combo()
        layout.addWidget(self.cb_union)

        self.chk_cunetas = QCheckBox("Exportar también CUNETAS (opcional)")
        self.chk_cunetas.setChecked(False)
        layout.addWidget(self.chk_cunetas)

        self.cb_cunetas = make_combo()
        self.cb_cunetas.setEnabled(False)
        layout.addWidget(self.cb_cunetas)
        self.chk_cunetas.toggled.connect(self.cb_cunetas.setEnabled)

        self.chk_wkt = QCheckBox("Incluir geometría (WKT) en una columna 'WKT'")
        self.chk_wkt.setChecked(False)
        layout.addWidget(self.chk_wkt)

        path_row = QHBoxLayout()
        self.btn_path = QPushButton("Elegir archivo .xlsx…")
        self.btn_path.clicked.connect(self.pick_path)
        path_row.addWidget(self.btn_path)

        self.lbl_path = QLabel("Salida: (no seleccionada)")
        self.lbl_path.setWordWrap(True)
        path_row.addWidget(self.lbl_path)
        layout.addLayout(path_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._values = None

    def pick_path(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar Excel", "", "Excel (*.xlsx)")
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self.output_path = path
            self.lbl_path.setText(f"Salida: {path}")

    def _selected_layer_or_none(self, combo: QComboBox):
        nm = combo.currentText()
        if nm == "— seleccionar —":
            return None
        return self.name_to_layer.get(nm)

    def _validate_and_accept(self):
        cuencas = self._selected_layer_or_none(self.cb_cuencas)
        cauces = self._selected_layer_or_none(self.cb_cauces)
        unionx = self._selected_layer_or_none(self.cb_union)

        if cuencas is None or cauces is None or unionx is None:
            QMessageBox.critical(self, "Error", "CUENCAS, CAUCES y UNIONxCUENCAS son obligatorias.")
            return

        cunetas = None
        if self.chk_cunetas.isChecked():
            cunetas = self._selected_layer_or_none(self.cb_cunetas)
            if cunetas is None:
                QMessageBox.critical(self, "Error", "Marcaste CUNETAS pero no seleccionaste ninguna capa.")
                return

        if not self.output_path:
            QMessageBox.critical(self, "Error", "Debes elegir un archivo de salida .xlsx.")
            return

        self._values = {"CUENCAS": cuencas, "CAUCES": cauces, "UNIONxCUENCAS": unionx, "CUNETAS": cunetas}
        self.accept()

    def values(self):
        return self._values, self.chk_wkt.isChecked(), self.output_path

# -------------------------
# MAIN
# -------------------------
all_layers = list(QgsProject.instance().mapLayers().values())
vector_layers = [l for l in all_layers if isinstance(l, QgsVectorLayer)]

if not vector_layers:
    QMessageBox.critical(parent, "Error", "No hay capas vectoriales cargadas en el proyecto.")
else:
    dlg = DrainageExportDialog(vector_layers)
    if dlg.exec_() == QDialog.Accepted:
        layer_map, include_wkt, out_path = dlg.values()

        sheets = {
            "CUENCAS": layer_map["CUENCAS"],
            "CAUCES": layer_map["CAUCES"],
            "UNIONxCUENCAS": layer_map["UNIONxCUENCAS"],
        }
        if layer_map["CUNETAS"] is not None:
            sheets["CUNETAS"] = layer_map["CUNETAS"]

        try:
            export_layers_to_excel(sheets, out_path, include_wkt=include_wkt)
            QMessageBox.information(
                parent,
                "OK",
                "Excel exportado correctamente.\n"
                "• Resultados: sin sombrear columna A; UNIONxCUENCAS en H-I-J-K sin huecos\n"
                "• Pestañas: azul-azul-verde-amarillo + autoajuste columnas\n\n"
                f"{out_path}"
            )
        except Exception as e:
            QMessageBox.critical(parent, "Error", f"No se pudo exportar:\n{e}")