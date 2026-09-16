# -*- coding: utf-8 -*-
"""Crea tipos de elementos estructurales (columnas, muros, vigas de liga,
pilas, losas...) a partir de una CARPETA con uno o mas archivos CSV, uno por
tipo de elemento. Cada archivo debe corresponder a una de las llaves del
diccionario PROFILES. Se acepta cualquiera de estos dos nombres de archivo:

    "<SPREADSHEET_NAME> - <NombreDelPerfil>.csv"   (nombre que exporta
                                                     Google Sheets por default)
    "<NombreDelPerfil>.csv"                        (nombre simple)

Ejemplo: para el perfil "Circular Columns", cualquiera de estos sirve:
    "Structural Family Types List - Circular Columns.csv"
    "Circular Columns.csv"

En Google Sheets: una pestana por tipo de elemento, y descargas cada pestana
por separado como CSV (Archivo -> Descargar -> Valores separados por comas),
guardandolas todas juntas en una misma carpeta.

100% IronPython, sin dependencias externas -- corre igual en cualquier
maquina del equipo sin instalar nada.
"""
import csv
import io
import os

from pyrevit import revit, DB, forms, script

# Nombre del archivo de Google Sheets (solo para reconocer el prefijo que
# agrega al exportar cada pestana como CSV; ajusta si le cambias el nombre).
SPREADSHEET_NAME = "Structural Family Types List"


# ----------------------------- PROFILES -------------------------------------
# Un perfil por cada tipo de elemento / archivo CSV esperado.
#
#   kind : "family_symbol" (columnas, vigas, pilas... familias cargables)
#          "wall_type"     (muros: system family, se edita por capas)
#          "floor_type"    (losas: system family, se edita por capas)
#
#   Para kind == "family_symbol":
#     base_type_name   : nombre EXACTO del tipo ya existente a duplicar, o
#     base_family_name : nombre de la FAMILIA (toma el primer tipo que
#                         encuentre ahi; mas robusto entre plantillas)
#     param_map         : { "columna en el CSV": "nombre del parametro en Revit" }
#
#   Para kind == "wall_type" / "floor_type":
#     base_type_name    : nombre EXACTO del tipo de muro/losa a duplicar
#     thickness_column   : columna del CSV que define el espesor de la
#                          capa estructural
#
#   Comun a todos:
#     units    : unidades en las que vienen los valores numericos del CSV
#     name_fn  : funcion que arma el nombre del tipo nuevo a partir de la
#                fila (dict con las columnas de ese CSV)

def _fmt_num(value):
    """'12' -> '12', '12.50' -> '12.5' (evita un feo '12.0' en los nombres)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return str(value)


PROFILES = {
    "Rectangular Columns": {
        "kind": "family_symbol",
        "base_type_name": 'C1 - 12" x 12"',
        "units": DB.UnitTypeId.Inches,
        "param_map": {"Width": "Width", "Length": "Length"},
        "name_fn": lambda row: '{0} - {1}" x {2}"'.format(
            row["Mark Type"], _fmt_num(row["Width"]), _fmt_num(row["Length"])
        ),
    },
    "Round Columns": {
        "kind": "family_symbol",
        "base_family_name": "Round Column",
        "units": DB.UnitTypeId.Inches,
        "param_map": {"Diameter": "Diameter"},
        "name_fn": lambda row: u"{0} - {1:.2f} \u00d8".format(
            row["Mark Type"], float(row["Diameter"])
        ),
    },
    "Concrete Walls": {
        "kind": "wall_type",
        "base_type_name": 'Cast in Place - 12"',
        "units": DB.UnitTypeId.Inches,
        "thickness_column": "Width",
        "name_fn": lambda row: '{0} - {1}"'.format(
            row["Mark Type"], _fmt_num(row["Width"])
        ),
    },
    "CMU Walls": {
        "kind": "wall_type",
        "base_type_name": 'CMU - 06"',
        "units": DB.UnitTypeId.Inches,
        "thickness_column": "Width",
        "name_fn": lambda row: '{0} - {1}"'.format(
            row["Mark Type"], _fmt_num(row["Width"])
        ),
    },
    "Grade Beams": {
        "kind": "family_symbol",
        "base_type_name": 'GB1 - 12" x 24"',
        "units": DB.UnitTypeId.Inches,
        "param_map": {"Width": "Width", "Thickness": "Thickness"},
        "name_fn": lambda row: '{0} - {1}" x {2}"'.format(
            row["Mark Type"], _fmt_num(row["Width"]), _fmt_num(row["Thickness"])
        ),
    },
    "Drilled Piers": {
        "kind": "family_symbol",
        "base_type_name": u'16" \u00d8',
        "units": DB.UnitTypeId.Inches,
        "param_map": {"Diameter": "Diameter", "Depth": "Depth"},
        "name_fn": lambda row: u'{0} - {1}" \u00d8 x {2}"'.format(
            row["Mark Type"], _fmt_num(row["Diameter"]), _fmt_num(row["Depth"])
        ),
    },
    "Slabs": {
        "kind": "floor_type",
        "base_type_name": 'Slab on Grade - 4"',
        "units": DB.UnitTypeId.Inches,
        "thickness_column": "Thickness",
        "name_fn": lambda row: '{0} - {1}"'.format(
            row["Mark Type"], _fmt_num(row["Thickness"])
        ),
    },
}
# -----------------------------------------------------------------------------

output = script.get_output()
doc = revit.doc


def get_name(element):
    """Wrapper para Element.Name: evita el AttributeError que da IronPython
    con propiedades de interfaz explicita (Revit 2022+)."""
    return DB.Element.Name.__get__(element)


# ---------------------- familias cargables (columnas, vigas...) ------------

def find_base_symbol(profile):
    """Busca el FamilySymbol base a duplicar (kind == family_symbol).

    Si el perfil trae "base_type_name", busca ese nombre de TIPO exacto.
    Si trae "base_family_name", toma el primer tipo que encuentre dentro
    de esa FAMILIA (mas robusto entre plantillas distintas). La
    comparacion de nombre de familia ignora mayusculas/espacios extra.
    """
    collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)

    if profile.get("base_type_name"):
        target = profile["base_type_name"]
        for sym in collector:
            if get_name(sym) == target:
                return sym
        return None

    if profile.get("base_family_name"):
        target = profile["base_family_name"].strip().lower()
        for sym in collector:
            if sym.Family.Name.strip().lower() == target:
                return sym
        return None

    return None


def get_existing_type_names(family):
    names = set()
    for type_id in family.GetFamilySymbolIds():
        sym = doc.GetElement(type_id)
        names.add(get_name(sym))
    return names


# ---------------------- system family types (muros, losas) -----------------

_SYSTEM_TYPE_CLASSES = {"wall_type": DB.WallType, "floor_type": DB.FloorType}


def find_base_system_type(profile):
    """Busca el WallType/FloorType base a duplicar por nombre exacto."""
    rvt_class = _SYSTEM_TYPE_CLASSES[profile["kind"]]
    target = profile["base_type_name"]
    for t in DB.FilteredElementCollector(doc).OfClass(rvt_class):
        if get_name(t) == target:
            return t
    return None


def get_existing_system_type_names(kind):
    rvt_class = _SYSTEM_TYPE_CLASSES[kind]
    names = set()
    for t in DB.FilteredElementCollector(doc).OfClass(rvt_class):
        names.add(get_name(t))
    return names


def set_structural_layer_width(type_elem, width_internal):
    """Ajusta el espesor de la(s) capa(s) con funcion 'Structure' dentro de
    la estructura compuesta del WallType/FloorType. Si no hay ninguna capa
    marcada como Structure pero solo hay una capa en total, usa esa."""
    cs = type_elem.GetCompoundStructure()
    if cs is None:
        return False, "El tipo no tiene compound structure (verifica que no sea 'By Category')."

    layers = list(cs.GetLayers())
    structural_indices = [
        i for i, layer in enumerate(layers)
        if layer.Function == DB.MaterialFunctionAssignment.Structure
    ]
    if not structural_indices:
        if len(layers) == 1:
            structural_indices = [0]
        else:
            return False, "No se encontro una capa 'Structure' para ajustar el espesor."

    for idx in structural_indices:
        cs.SetLayerWidth(idx, width_internal)

    type_elem.SetCompoundStructure(cs)
    return True, None


# ---------------------- utilerias comunes -----------------------------------

def find_similar_names(target, kind):
    """Ayuda de diagnostico: nombres de familia (o de tipo, para muros/losas)
    parecidos al buscado, para detectar typos o diferencias de plantilla."""
    target_words = [w for w in target.strip().lower().split() if w]
    matches = []
    seen = set()

    if kind == "family_symbol":
        for sym in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol):
            name = sym.Family.Name
            if name in seen:
                continue
            seen.add(name)
            if any(w in name.lower() for w in target_words):
                matches.append(name)
            if len(matches) >= 15:
                break
    else:
        rvt_class = _SYSTEM_TYPE_CLASSES[kind]
        for t in DB.FilteredElementCollector(doc).OfClass(rvt_class):
            name = get_name(t)
            if any(w in name.lower() for w in target_words):
                matches.append(name)
            if len(matches) >= 15:
                break

    return matches


def read_csv_rows(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel  # fallback: coma por default
        reader = csv.DictReader(f, dialect=dialect)
        rows = []
        for row in reader:
            clean_row = {(k.strip() if k else k): v for k, v in row.items()}
            rows.append(clean_row)
        return rows


def find_csv_for_profile(folder, profile_name):
    """Acepta tanto '<SPREADSHEET_NAME> - <perfil>.csv' (nombre que exporta
    Google Sheets por default) como '<perfil>.csv' (nombre simple)."""
    candidates = [
        "{0} - {1}.csv".format(SPREADSHEET_NAME, profile_name),
        "{0}.csv".format(profile_name),
    ]
    for filename in candidates:
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            return path
    return None


# 1. Elegir la carpeta con los CSV --------------------------------------------
folder = forms.pick_folder(title="Selecciona la carpeta con los CSV de elementos")
if not folder:
    script.exit()

matched_files = {}
for profile_name in PROFILES:
    found = find_csv_for_profile(folder, profile_name)
    if found:
        matched_files[profile_name] = found

if not matched_files:
    forms.alert(
        "No se encontro ningun CSV reconocible en esa carpeta.\n\n"
        "Nombres de archivo esperados (cualquiera de los dos por perfil):\n{}".format(
            "\n".join(
                "- {0} - {1}.csv  o  {1}.csv".format(SPREADSHEET_NAME, p)
                for p in PROFILES
            )
        ),
        exitscript=True,
    )

summary = {}

# 2. Procesar cada CSV que coincida con un perfil -----------------------------
with revit.Transaction("Crear tipos desde CSV"):
    for profile_name, csv_path in matched_files.items():
        profile = PROFILES[profile_name]
        kind = profile["kind"]
        rows = read_csv_rows(csv_path)
        created, skipped, errors = [], [], []

        # --- Familias cargables (columnas, vigas, pilas...) -----------------
        if kind == "family_symbol":
            base_symbol = find_base_symbol(profile)
            if not base_symbol:
                base_ref = profile.get("base_type_name") or profile.get("base_family_name")
                msg = "No se encontro el tipo/familia base '{}' en el proyecto.".format(base_ref)
                similar = find_similar_names(base_ref, kind)
                if similar:
                    msg += " Nombres parecidos si encontrados: {}".format(", ".join(similar))
                errors.append(msg)
                summary[profile_name] = (created, skipped, errors)
                continue

            family = base_symbol.Family
            existing_names = get_existing_type_names(family)

            for i, row in enumerate(rows, start=2):  # fila 1 = encabezado
                try:
                    new_name = profile["name_fn"](row)
                except Exception as e:
                    errors.append("Fila {0}: no se pudo construir el nombre ({1})".format(i, e))
                    continue

                if new_name in existing_names:
                    skipped.append(new_name)
                    continue

                new_symbol = base_symbol.Duplicate(new_name)
                existing_names.add(new_name)

                row_ok = True
                for csv_col, revit_param in profile["param_map"].items():
                    param = new_symbol.LookupParameter(revit_param)
                    if param is None:
                        errors.append(
                            "{0}: no se encontro el parametro '{1}'.".format(new_name, revit_param)
                        )
                        row_ok = False
                        continue
                    try:
                        value = float(row[csv_col])
                    except (KeyError, TypeError, ValueError):
                        errors.append(
                            "{0}: valor invalido en la columna '{1}'.".format(new_name, csv_col)
                        )
                        row_ok = False
                        continue
                    internal_value = DB.UnitUtils.ConvertToInternalUnits(value, profile["units"])
                    param.Set(internal_value)

                if row_ok:
                    created.append(new_name)

        # --- System family types (muros, losas) -----------------------------
        else:
            base_type = find_base_system_type(profile)
            if not base_type:
                msg = "No se encontro el tipo base '{}' en el proyecto.".format(
                    profile["base_type_name"]
                )
                similar = find_similar_names(profile["base_type_name"], kind)
                if similar:
                    msg += " Nombres parecidos si encontrados: {}".format(", ".join(similar))
                errors.append(msg)
                summary[profile_name] = (created, skipped, errors)
                continue

            existing_names = get_existing_system_type_names(kind)
            thickness_col = profile["thickness_column"]

            for i, row in enumerate(rows, start=2):
                try:
                    new_name = profile["name_fn"](row)
                except Exception as e:
                    errors.append("Fila {0}: no se pudo construir el nombre ({1})".format(i, e))
                    continue

                if new_name in existing_names:
                    skipped.append(new_name)
                    continue

                try:
                    value = float(row[thickness_col])
                except (KeyError, TypeError, ValueError):
                    errors.append(
                        "{0}: valor invalido en la columna '{1}'.".format(new_name, thickness_col)
                    )
                    continue

                new_type = base_type.Duplicate(new_name)
                existing_names.add(new_name)

                internal_value = DB.UnitUtils.ConvertToInternalUnits(value, profile["units"])
                ok, err = set_structural_layer_width(new_type, internal_value)
                if ok:
                    created.append(new_name)
                else:
                    errors.append("{0}: {1}".format(new_name, err))

        summary[profile_name] = (created, skipped, errors)

# 3. Reporte -------------------------------------------------------------------
for profile_name, (created, skipped, errors) in summary.items():
    output.print_md("## {}".format(profile_name))
    output.print_md("**Creados:** {}".format(len(created)))
    for n in created:
        output.print_md("- {}".format(n))
    if skipped:
        output.print_md("**Omitidos (ya existian):** {}".format(len(skipped)))
        for n in skipped:
            output.print_md("- {}".format(n))
    if errors:
        output.print_md("**Errores:**")
        for e in errors:
            output.print_md("- {}".format(e))

ignored_profiles = [p for p in PROFILES if p not in matched_files]
if ignored_profiles:
    output.print_md("## Perfiles sin CSV en la carpeta (no procesados)")
    for p in ignored_profiles:
        output.print_md("- {}".format(p))