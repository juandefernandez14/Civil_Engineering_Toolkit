# -*- coding: utf-8 -*-
"""
MOVIMIENTO DE TIERRAS (EJECUTIVO) — EJES + HINCAS
=================================================
Script "normal" para QGIS (no ProcessingAlgorithm). Pensado para ejecutarse
igual que vuestros otros scripts del Toolkit (desde el dock / botón / exec).

Qué hace (ejecutivo):
- Usa Ejes (líneas) + Hincas (puntos).
- Cada hinca DEBE coincidir con algún eje (distancia <= 0.02 m). Si no, el script falla.
- Agrupa hincas por tracker (eje más cercano) y ordena por chainage a lo largo del eje.
- Ajusta una superficie de proyecto cumpliendo:
  * Pendiente máx N-S (%): |dZ/dY|
  * Pendiente máx E-O (%): |dZ/dX|
  * Pendiente máx entre hincas consecutivas (%) por tracker
- Interpola una superficie de proyecto (IDW) dentro de la huella grading.
- Calcula corte/relleno (proy - natural) dentro de la huella.
- Ajusta offset global Δ para que Desmonte/Relleno ≈ 1.2,
  con preferencia |Δ|≤0.02 m y máximo |Δ|≤0.03 m, SIEMPRE priorizando Desmonte ≥ Relleno.
- Calcula TOPSOIL independiente: Área huella * espesor.
- Exporta:
  * Puntos usados (hincas) a una capa en memoria (con Z_nat/Z_proj/DZ)
  * Raster MDT proyecto (GeoTIFF)
  * Raster corte/relleno (GeoTIFF)
  * Reporte TXT

Entradas (en diálogo):
- Ejes (líneas)
- Hincas (puntos) -> debe tener campos: ID, X, Y (según tu requisito)
- MDT natural (raster)
- Huella grading (polígono)
- Pendiente máx N-S (%)
- Pendiente máx E-O (%)
- Pendiente máx entre hincas (%)
- Tallaño celda (m): 0 => resolución del MDT
- Espesor topsoil (m)
- Esponjamiento (%): vacío/0 => 15%
- Carpeta salida y prefijo

Notas:
- CRS proyectado (metros) obligatorio.
- Recomendado que MDT / huella / ejes / hincas estén en el mismo CRS.
"""

import os
import math
import traceback

try:
    import numpy as np
except Exception:
    np = None

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDoubleSpinBox,
    QLineEdit, QPushButton, QFileDialog, QMessageBox, QFormLayout
)

from qgis.core import (
    QgsProject, QgsWkbTypes, QgsMessageLog, Qgis,
    QgsGeometry, QgsPointXY, QgsRectangle,
    QgsRaster, QgsRasterLayer, QgsRasterDataProvider, QgsRasterBlock,
    QgsRasterFileWriter,
    QgsFeature, QgsFields, QgsField, QgsVectorLayer,
    QgsSpatialIndex
)

# -------------------------
# Ajustes fijos por requisito
# -------------------------
TARGET_RATIO = 1.2
DELTA_PREF = 0.02
DELTA_MAX = 0.03
DELTA_STEP = 0.0005  # 0.5 mm

SNAP_TOL = 0.02  # 2 cm: hinca debe estar sobre un eje


# -------------------------
# Utilidades base
# -------------------------
def _layers_by_type(wkb_family: str):
    prj = QgsProject.instance()
    out = []
    for lyr in prj.mapLayers().values():
        if wkb_family == 'raster':
            if isinstance(lyr, QgsRasterLayer) and lyr.isValid():
                out.append(lyr)
        else:
            if hasattr(lyr, "wkbType"):
                gt = QgsWkbTypes.geometryType(lyr.wkbType())
                if wkb_family == 'line' and gt == QgsWkbTypes.LineGeometry:
                    out.append(lyr)
                if wkb_family == 'polygon' and gt == QgsWkbTypes.PolygonGeometry:
                    out.append(lyr)
                if wkb_family == 'point' and gt == QgsWkbTypes.PointGeometry:
                    out.append(lyr)
    return out


def _require_fields(layer, names):
    existing = {f.name() for f in layer.fields()}
    missing = [n for n in names if n not in existing]
    if missing:
        raise RuntimeError(f"Faltan campos obligatorios en hincas: {missing}. Deben existir: {names}")


def _sample_dem_z(dem: QgsRasterLayer, x: float, y: float):
    prov: QgsRasterDataProvider = dem.dataProvider()
    ident = prov.identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue)
    if not ident.isValid():
        return None
    z = ident.results().get(1, None)
    if z is None:
        return None
    try:
        zf = float(z)
    except Exception:
        return None
    nd = prov.sourceNoDataValue(1)
    if nd is not None:
        try:
            nd = float(nd)
            if not math.isnan(nd) and abs(zf - nd) < 1e-12:
                return None
        except Exception:
            pass
    return zf


def _union_grading_geom(grading_layer):
    geoms = []
    for f in grading_layer.getFeatures():
        g = f.geometry()
        if g and not g.isEmpty():
            geoms.append(g)
    if not geoms:
        raise RuntimeError("La huella grading no tiene geometrías válidas.")
    ug = QgsGeometry.unaryUnion(geoms)
    if ug is None or ug.isEmpty():
        raise RuntimeError("No se pudo disolver la huella grading (unaryUnion).")
    return ug


def _axis_main_part(geom: QgsGeometry):
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        if not parts:
            return None
        best = None
        best_len = -1.0
        for part in parts:
            g = QgsGeometry.fromPolylineXY(part)
            L = g.length()
            if L > best_len:
                best_len = L
                best = g
        return best
    return geom


def _fit_plane_limited(xs, ys, zs, max_slope_ew_pct, max_slope_ns_pct):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS. No puedo ajustar/interpolar sin numpy.")
    X = np.column_stack([xs, ys, np.ones_like(xs)])
    beta, _, _, _ = np.linalg.lstsq(X, zs, rcond=None)
    a, b, c = beta.tolist()

    a_lim = max_slope_ew_pct / 100.0
    b_lim = max_slope_ns_pct / 100.0
    a2 = max(-a_lim, min(a, a_lim))
    b2 = max(-b_lim, min(b, b_lim))

    c2 = float(np.mean(zs - a2 * xs - b2 * ys))
    return a2, b2, c2


def _enforce_max_slope_between_ordered(xs, ys, zproj, max_slope_between_pct, order_idx):
    slope = max_slope_between_pct / 100.0
    z = zproj.copy()

    # forward
    for k in range(1, len(order_idx)):
        i0 = order_idx[k - 1]
        i1 = order_idx[k]
        d = math.hypot(xs[i1] - xs[i0], ys[i1] - ys[i0])
        if d <= 1e-9:
            continue
        dzmax = slope * d
        dz = z[i1] - z[i0]
        if dz > dzmax:
            z[i1] = z[i0] + dzmax
        elif dz < -dzmax:
            z[i1] = z[i0] - dzmax

    # backward
    for k in range(len(order_idx) - 2, -1, -1):
        i0 = order_idx[k + 1]
        i1 = order_idx[k]
        d = math.hypot(xs[i1] - xs[i0], ys[i1] - ys[i0])
        if d <= 1e-9:
            continue
        dzmax = slope * d
        dz = z[i1] - z[i0]
        if dz > dzmax:
            z[i1] = z[i0] + dzmax
        elif dz < -dzmax:
            z[i1] = z[i0] - dzmax

    return z


def _make_grid(extent: QgsRectangle, cell: float):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS. No puedo crear grids sin numpy.")
    xmin, xmax = extent.xMinimum(), extent.xMaximum()
    ymin, ymax = extent.yMinimum(), extent.yMaximum()
    cols = int(math.ceil((xmax - xmin) / cell))
    rows = int(math.ceil((ymax - ymin) / cell))
    if rows <= 0 or cols <= 0:
        raise RuntimeError("Extensión/celda inválidas.")
    xs = xmin + (np.arange(cols) + 0.5) * cell
    ys = ymax - (np.arange(rows) + 0.5) * cell
    Xg, Yg = np.meshgrid(xs, ys)
    gt = (xmin, cell, 0.0, ymax, 0.0, -cell)
    return Xg, Yg, rows, cols, gt


def _mask_from_polygon(Xg, Yg, poly_geom: QgsGeometry):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS.")
    engine = QgsGeometry.createGeometryEngine(poly_geom.constGet())
    engine.prepareGeometry()

    gx = Xg.ravel()
    gy = Yg.ravel()
    mask = np.zeros(gx.size, dtype=bool)

    chunk = 50000
    for s in range(0, gx.size, chunk):
        e = min(gx.size, s + chunk)
        for i in range(s, e):
            if engine.contains(QgsPointXY(float(gx[i]), float(gy[i]))):
                mask[i] = True

    return mask.reshape(Xg.shape)


def _idw_to_grid(Xg, Yg, mask, pts_xy, pts_z, power=2.0, k=12, nodata=-9999.0):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS.")
    P = np.array(pts_xy, dtype=float)
    Z = np.array(pts_z, dtype=float)
    out = np.full(Xg.shape, nodata, dtype=np.float32)

    idx_inside = np.where(mask.ravel())[0]
    if idx_inside.size == 0:
        raise RuntimeError("La huella grading no cubre ninguna celda del grid (revisa CRS/extensión).")

    gx = Xg.ravel()
    gy = Yg.ravel()
    kk = min(k, P.shape[0])
    if kk < 3:
        raise RuntimeError("Insuficientes puntos para interpolación (mínimo 3).")

    chunk = 20000
    for s in range(0, idx_inside.size, chunk):
        e = min(idx_inside.size, s + chunk)
        ids = idx_inside[s:e]
        qx = gx[ids][:, None]
        qy = gy[ids][:, None]

        dx = qx - P[None, :, 0]
        dy = qy - P[None, :, 1]
        d2 = dx * dx + dy * dy

        exact = d2 == 0.0
        if exact.any():
            ex = np.where(exact)
            for qi, pi in zip(ex[0], ex[1]):
                out.ravel()[ids[qi]] = float(Z[pi])
            d2[exact] = 1e-12

        idx = np.argpartition(d2, kk - 1, axis=1)[:, :kk]
        d2k = np.take_along_axis(d2, idx, axis=1)
        zk = Z[idx]

        w = 1.0 / np.power(d2k, power / 2.0)
        zhat = np.sum(w * zk, axis=1) / np.sum(w, axis=1)
        out.ravel()[ids] = zhat.astype(np.float32)

    return out


def _sample_nat_dem_to_grid(dem: QgsRasterLayer, Xg, Yg, mask):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS.")
    nat = np.full(Xg.shape, np.nan, dtype=np.float32)

    gx = Xg.ravel()
    gy = Yg.ravel()
    idx_inside = np.where(mask.ravel())[0]

    chunk = 20000
    for s in range(0, idx_inside.size, chunk):
        e = min(idx_inside.size, s + chunk)
        ids = idx_inside[s:e]
        for j in ids:
            z = _sample_dem_z(dem, float(gx[j]), float(gy[j]))
            if z is None:
                continue
            nat.ravel()[j] = np.float32(z)
    return nat


def _volumes_from_diff(diff, cell_area):
    fill = np.nansum(np.where(diff > 0, diff, 0.0)) * cell_area
    cut = np.nansum(np.where(diff < 0, diff, 0.0)) * cell_area  # negativo
    return float(fill), float(cut)


def _choose_delta(proj_base, nat, mask, cell_area, log_fn):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS.")
    def eval_delta(delta):
        diff = (proj_base + delta) - nat
        diff = np.where(mask, diff, np.nan)
        diff = np.where(np.isfinite(nat), diff, np.nan)
        fill, cut = _volumes_from_diff(diff, cell_area)
        cut_bank = abs(cut)
        ratio = float("inf") if fill <= 1e-12 else cut_bank / fill
        okp = (cut_bank + 1e-9) >= fill
        return fill, cut, ratio, okp

    def scan(limit):
        deltas = np.arange(-limit, limit + DELTA_STEP / 2, DELTA_STEP)
        best = None
        for d in deltas:
            fill, cut, ratio, okp = eval_delta(float(d))
            cand = {
                "delta": float(d),
                "fill": float(fill),
                "cut": float(cut),
                "ratio": float(ratio),
                "okp": bool(okp),
                "err": abs(float(ratio) - TARGET_RATIO),
                "absd": abs(float(d)),
            }
            if best is None:
                best = cand
            else:
                def key(x):
                    return (0 if x["okp"] else 1, x["err"], x["absd"], x["delta"])
                if key(cand) < key(best):
                    best = cand
        return best

    best_pref = scan(DELTA_PREF)
    best_max = scan(DELTA_MAX)

    best = best_pref
    if (best is None) or ((not best["okp"]) and best_max is not None and best_max["okp"]):
        best = best_max

    if best is not None and not best["okp"]:
        log_fn("No se pudo cumplir Desmonte ≥ Relleno dentro de ±3 cm. Se fuerza Δ=-0.03 m (máximo desmonte).")
        fill, cut, ratio, okp = eval_delta(-DELTA_MAX)
        best = {"delta": -DELTA_MAX, "fill": fill, "cut": cut, "ratio": ratio, "okp": okp}

    return best


def _write_geotiff(path, arr, gt, crs, nodata=-9999.0):
    if np is None:
        raise RuntimeError("Numpy no está disponible en este QGIS.")
    rows, cols = arr.shape
    xmin, px, _, ymax, _, py = gt
    py = abs(py)
    extent = QgsRectangle(xmin, ymax - rows * py, xmin + cols * px, ymax)

    writer = QgsRasterFileWriter(path)
    writer.setOutputFormat("GTiff")
    writer.setCreateOptions(["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])

    dp = writer.createOneBandRaster(QgsRaster.DataType.Float32, cols, rows, extent, crs)
    if dp is None:
        raise RuntimeError("No se pudo crear el GeoTIFF de salida.")
    dp.setNoDataValue(1, nodata)

    block_size = 256
    for r0 in range(0, rows, block_size):
        for c0 in range(0, cols, block_size):
            r1 = min(rows, r0 + block_size)
            c1 = min(cols, c0 + block_size)
            sub = arr[r0:r1, c0:c1].astype(np.float32)
            block = QgsRasterBlock(QgsRaster.DataType.Float32, c1 - c0, r1 - r0)
            block.setData(sub.tobytes())
            ok = dp.writeBlock(block, 1, c0, r0)
            if not ok:
                raise RuntimeError("Error escribiendo bloques raster.")
    dp.flush()


# -------------------------
# Diálogo
# -------------------------
class EarthworksAxesPilesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Movimiento de tierras (Ejecutivo) — Ejes + Hincas")
        self.setWindowModality(Qt.WindowModal)
        self.resize(560, 290)

        self.axes_cb = QComboBox()
        self.piles_cb = QComboBox()
        self.dem_cb = QComboBox()
        self.grading_cb = QComboBox()

        self.ns_sb = QDoubleSpinBox(); self.ns_sb.setRange(0.0, 100.0); self.ns_sb.setDecimals(3); self.ns_sb.setValue(5.0)
        self.ew_sb = QDoubleSpinBox(); self.ew_sb.setRange(0.0, 100.0); self.ew_sb.setDecimals(3); self.ew_sb.setValue(5.0)
        self.between_sb = QDoubleSpinBox(); self.between_sb.setRange(0.0, 100.0); self.between_sb.setDecimals(3); self.between_sb.setValue(2.0)

        self.cell_sb = QDoubleSpinBox(); self.cell_sb.setRange(0.0, 1000.0); self.cell_sb.setDecimals(3); self.cell_sb.setValue(0.0)
        self.topsoil_sb = QDoubleSpinBox(); self.topsoil_sb.setRange(0.0, 10.0); self.topsoil_sb.setDecimals(3); self.topsoil_sb.setValue(0.30)
        self.swell_sb = QDoubleSpinBox(); self.swell_sb.setRange(0.0, 100.0); self.swell_sb.setDecimals(3); self.swell_sb.setValue(0.0)

        self.out_dir_le = QLineEdit()
        self.prefix_le = QLineEdit("Earthworks_AxesPiles")

        browse_btn = QPushButton("...")
        browse_btn.clicked.connect(self._browse)

        form = QFormLayout()
        form.addRow("Ejes (líneas):", self.axes_cb)
        form.addRow("Hincas (puntos):", self.piles_cb)
        form.addRow("MDT natural (raster):", self.dem_cb)
        form.addRow("Huella grading (polígono):", self.grading_cb)
        form.addRow("Pendiente máx N-S (%):", self.ns_sb)
        form.addRow("Pendiente máx E-O (%):", self.ew_sb)
        form.addRow("Pendiente máx entre hincas (%):", self.between_sb)
        form.addRow("Tamaño celda (m) (0=>MDT):", self.cell_sb)
        form.addRow("Espesor topsoil (m):", self.topsoil_sb)
        form.addRow("Esponjamiento (%) (0=>15):", self.swell_sb)

        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir_le, 1)
        out_row.addWidget(browse_btn)
        form.addRow("Carpeta salida:", out_row)
        form.addRow("Prefijo ficheros:", self.prefix_le)

        btns = QHBoxLayout()
        self.run_btn = QPushButton("Ejecutar")
        self.cancel_btn = QPushButton("Cancelar")
        self.run_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.cancel_btn)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addLayout(btns)
        self.setLayout(layout)

        self._populate()

    def _populate(self):
        self.axes_cb.clear()
        self.piles_cb.clear()
        self.dem_cb.clear()
        self.grading_cb.clear()

        self._axes = _layers_by_type('line')
        self._piles = _layers_by_type('point')
        self._dems = _layers_by_type('raster')
        self._grading = _layers_by_type('polygon')

        for lyr in self._axes:
            self.axes_cb.addItem(lyr.name())
        for lyr in self._piles:
            self.piles_cb.addItem(lyr.name())
        for lyr in self._dems:
            self.dem_cb.addItem(lyr.name())
        for lyr in self._grading:
            self.grading_cb.addItem(lyr.name())

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Selecciona carpeta de salida")
        if d:
            self.out_dir_le.setText(d)

    def get_values(self):
        axes = self._axes[self.axes_cb.currentIndex()] if self._axes else None
        piles = self._piles[self.piles_cb.currentIndex()] if self._piles else None
        dem = self._dems[self.dem_cb.currentIndex()] if self._dems else None
        grading = self._grading[self.grading_cb.currentIndex()] if self._grading else None

        return {
            "axes": axes,
            "piles": piles,
            "dem": dem,
            "grading": grading,
            "max_ns": float(self.ns_sb.value()),
            "max_ew": float(self.ew_sb.value()),
            "max_between": float(self.between_sb.value()),
            "cell_size": float(self.cell_sb.value()),
            "topsoil": float(self.topsoil_sb.value()),
            "swell_pct": float(self.swell_sb.value()),
            "out_dir": self.out_dir_le.text().strip(),
            "prefix": self.prefix_le.text().strip() or "Earthworks_AxesPiles",
        }


# -------------------------
# Ejecutable principal
# -------------------------
def run_earthworks_axes_with_piles(iface=None):
    """
    Ejecuta el script con diálogo.
    Llamable desde vuestro dock/botón:
        run_earthworks_axes_with_piles(iface)
    """
    try:
        dlg = EarthworksAxesPilesDialog(parent=iface.mainWindow() if iface else None)
        if dlg.exec_() != QDialog.Accepted:
            return

        v = dlg.get_values()
        axes = v["axes"]
        piles = v["piles"]
        dem = v["dem"]
        grading = v["grading"]

        if axes is None or piles is None or dem is None or grading is None:
            raise RuntimeError("Faltan capas de entrada (ejes, hincas, MDT o huella grading).")

        if np is None:
            raise RuntimeError("Este script requiere numpy. En tu instalación QGIS no está disponible.")

        # CRS checks
        if axes.crs().isGeographic():
            raise RuntimeError("CRS de ejes es geográfico (grados). Reproyecta a CRS en metros (UTM).")

        # hincas requieren campos
        _require_fields(piles, ["ID", "X", "Y"])

        # Defaults
        swell_pct = 15.0 if v["swell_pct"] <= 0.0 else float(v["swell_pct"])
        swell_factor = 1.0 + swell_pct / 100.0

        cell_size = float(v["cell_size"])
        if cell_size <= 0.0:
            cell_size = dem.rasterUnitsPerPixelX()
            if cell_size is None or cell_size <= 0:
                cell_size = 1.0

        max_ns = v["max_ns"]
        max_ew = v["max_ew"]
        max_between = v["max_between"]
        topsoil_thick = v["topsoil"]

        out_dir = v["out_dir"]
        if not out_dir or not os.path.isdir(out_dir):
            raise RuntimeError("Carpeta de salida inválida. Selecciona una carpeta existente.")

        prefix = v["prefix"]
        out_proj_path = os.path.join(out_dir, f"{prefix}_MDT_PROY.tif")
        out_cutfill_path = os.path.join(out_dir, f"{prefix}_CUT_FILL.tif")
        out_report_path = os.path.join(out_dir, f"{prefix}_REPORTE.txt")

        def log(msg):
            QgsMessageLog.logMessage(msg, "CivilEngineeringToolkit", Qgis.Info)

        log(f"SNAP_TOL (punto-eje): {SNAP_TOL:.3f} m | Resolución MDT proyecto: {cell_size} m | Esponjamiento: {swell_pct:.2f}%")

        grading_geom = _union_grading_geom(grading)
        extent = grading_geom.boundingBox()
        area_grading = grading_geom.area()
        v_topsoil = area_grading * topsoil_thick

        # 1) Index de ejes para búsqueda
        axis_feats = list(axes.getFeatures())
        if not axis_feats:
            raise RuntimeError("La capa de ejes no tiene entidades.")
        sindex = QgsSpatialIndex(axes.getFeatures())

        # Cache geometrías de ejes (main part)
        axis_geoms = {}
        axis_ids = {}
        axes_fields = [fld.name() for fld in axes.fields()]
        for f in axis_feats:
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            g2 = _axis_main_part(g)
            if g2 is None or g2.isEmpty() or g2.length() <= 0:
                continue
            axis_geoms[f.id()] = g2
            axis_id = f["ID"] if "ID" in axes_fields else str(f.id())
            axis_ids[f.id()] = str(axis_id)

        if not axis_geoms:
            raise RuntimeError("No hay geometrías de ejes válidas (líneas con longitud).")

        # 2) Construir puntos de trabajo: hincas asignadas a eje + chainage
        working = []
        pile_feats = list(piles.getFeatures())
        if not pile_feats:
            raise RuntimeError("La capa de hincas está vacía.")

        for pf in pile_feats:
            pg = pf.geometry()
            if not pg or pg.isEmpty():
                continue
            p = pg.asPoint()
            px, py = float(p.x()), float(p.y())
            pgeom = QgsGeometry.fromPointXY(QgsPointXY(px, py))

            # buscar candidatos por bbox cercano (5 m)
            search_rect = QgsRectangle(px - 5.0, py - 5.0, px + 5.0, py + 5.0)
            cands = sindex.intersects(search_rect)
            if not cands:
                raise RuntimeError(f"Hinca fuera de ejes (sin candidatos): ({px:.3f}, {py:.3f})")

            best = None
            for afid in cands:
                if afid not in axis_geoms:
                    continue
                ageom = axis_geoms[afid]
                d = ageom.distance(pgeom)
                if best is None or d < best[0]:
                    best = (d, afid, ageom)

            if best is None or best[0] > SNAP_TOL:
                raise RuntimeError(
                    f"Hinca no coincide con ningún eje (distancia {best[0] if best else None:.4f} m > {SNAP_TOL} m)."
                )

            dmin, afid, ageom = best
            chain = float(ageom.lineLocatePoint(pgeom))

            working.append({
                "x": px,
                "y": py,
                "axis_fid": afid,
                "axis_id": axis_ids.get(afid, str(afid)),
                "chain": chain,
                "orig": pf
            })

        if len(working) < 3:
            raise RuntimeError("Insuficientes hincas válidas (mínimo 3) para ajustar superficie.")

        xs = np.array([w["x"] for w in working], dtype=float)
        ys = np.array([w["y"] for w in working], dtype=float)

        # 3) Muestrear Z natural en hincas
        z_nat = []
        for w in working:
            z = _sample_dem_z(dem, w["x"], w["y"])
            z_nat.append(np.nan if z is None else float(z))
        z_nat = np.array(z_nat, dtype=float)

        valid = np.isfinite(z_nat)
        if valid.sum() < 3:
            raise RuntimeError("Insuficientes hincas con Z válida del MDT (NODATA).")

        # 4) Plano base limitado por pendientes N-S y E-O
        a, b, c = _fit_plane_limited(xs[valid], ys[valid], z_nat[valid], max_ew, max_ns)
        z_base = a * xs + b * ys + c
        z_proj = z_base.copy()

        # 5) Limitar pendiente entre hincas consecutivas por tracker (orden chainage)
        groups = {}
        for i, w in enumerate(working):
            groups.setdefault(w["axis_id"], []).append(i)

        for aid,

