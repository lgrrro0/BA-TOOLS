# -*- coding: utf-8 -*-
"""Structural types <-> CSV: shared profiles and helpers for the Structural
Types buttons (import from CSV, export to CSV in inches or feet-inches).

CSV columns may carry a unit suffix in the header:
    "Width (in)"     -> decimal inches, e.g. 18.5
    "Width (ft)"     -> decimal feet, e.g. 1.5
    "Width (ft-in)"  -> feet-inches, e.g. 1'-6"  or  1'-4 1/2"
    "Width"          -> the profile's units (inches), as before
Values written with ' or " are always read as feet-inches.
"""
import csv
import io
import os


import Autodesk.Revit.DB as DB

# Name of the Google Sheets file (only used to recognize the prefix it adds
# when exporting each tab as CSV; update it if you rename the spreadsheet).
SPREADSHEET_NAME = "Structural Family Types List"


def _fmt_num(value):
    """'12' -> '12', '12.50' -> '12.5' (avoids an ugly '12.0' in names)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return str(value)


# ----------------------------- PROFILES -------------------------------------
# One profile per element type / expected CSV file.
#
#   kind : "family_symbol" (columns, beams, piers... loadable families)
#          "wall_type"     (walls: system family, edited by layers)
#          "floor_type"    (slabs: system family, edited by layers)
#
#   For kind == "family_symbol":
#     base_type_name   : EXACT name of the existing type to duplicate, or
#     base_family_name : name of the FAMILY (takes the first type found
#                         in it; more robust across templates)
#     param_map         : { "CSV column": "Revit parameter name" }
#
#   For kind == "wall_type" / "floor_type":
#     base_type_name    : EXACT name of the wall/floor type to duplicate
#     thickness_column   : CSV column that defines the thickness of the
#                          structural layer
#
#   Common to all:
#     units    : units of the numeric values in the CSV
#     name_fn  : function that builds the new type name from the row
#                (dict with that CSV's columns, values in `units`)
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
        "name_fn": lambda row: u"{0} - {1:.2f} Ø".format(
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
        "base_type_name": u'16" Ø',
        "units": DB.UnitTypeId.Inches,
        "param_map": {"Diameter": "Diameter", "Depth": "Depth"},
        "name_fn": lambda row: u'{0} - {1}" Ø x {2}"'.format(
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

_SYSTEM_TYPE_CLASSES = {"wall_type": DB.WallType, "floor_type": DB.FloorType}


def get_name(element):
    """Wrapper for Element.Name: avoids the AttributeError IronPython raises
    on explicit interface properties (Revit 2022+)."""
    return DB.Element.Name.__get__(element)


def value_columns(profile):
    """CSV columns that hold lengths for this profile."""
    if profile["kind"] == "family_symbol":
        return list(profile["param_map"].keys())
    return [profile["thickness_column"]]


# ---------------------- lookups ---------------------------------------------

def find_base_symbol(doc, profile):
    """Finds the base FamilySymbol to duplicate (kind == family_symbol).

    If the profile has "base_type_name", looks for that exact TYPE name.
    If it has "base_family_name", takes the first type found in that
    FAMILY (more robust across different templates). The family name
    comparison ignores case and extra whitespace.
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


def get_existing_type_names(doc, family):
    names = set()
    for type_id in family.GetFamilySymbolIds():
        names.add(get_name(doc.GetElement(type_id)))
    return names


def find_base_system_type(doc, profile):
    """Finds the base WallType/FloorType to duplicate by exact name."""
    rvt_class = _SYSTEM_TYPE_CLASSES[profile["kind"]]
    target = profile["base_type_name"]
    for t in DB.FilteredElementCollector(doc).OfClass(rvt_class):
        if get_name(t) == target:
            return t
    return None


def get_existing_system_type_names(doc, kind):
    return set(get_name(t) for t in DB.FilteredElementCollector(doc).OfClass(_SYSTEM_TYPE_CLASSES[kind]))


def _structural_layers(cs):
    layers = list(cs.GetLayers())
    idx = [i for i, layer in enumerate(layers) if layer.Function == DB.MaterialFunctionAssignment.Structure]
    if not idx and len(layers) == 1:
        idx = [0]
    return layers, idx


def set_structural_layer_width(type_elem, width_internal):
    """Sets the thickness of the layer(s) with function 'Structure' in the
    compound structure of the WallType/FloorType. If no layer is marked as
    Structure but there is only one layer in total, that one is used."""
    cs = type_elem.GetCompoundStructure()
    if cs is None:
        return False, "The type has no compound structure (check that it is not 'By Category')."
    layers, structural_indices = _structural_layers(cs)
    if not structural_indices:
        return False, "No 'Structure' layer found to set the thickness."
    for idx in structural_indices:
        cs.SetLayerWidth(idx, width_internal)
    type_elem.SetCompoundStructure(cs)
    return True, None


def structural_layer_info(type_elem):
    """(width in feet, structural material id) of the structural layer, or (None, None)."""
    cs = type_elem.GetCompoundStructure()
    if cs is None:
        return None, None
    layers, idx = _structural_layers(cs)
    if not idx:
        return None, None
    return sum(layers[i].Width for i in idx), layers[idx[0]].MaterialId


def find_similar_names(doc, target, kind):
    """Diagnostic helper: family names (or type names, for walls/slabs)
    similar to the one searched, to spot typos or template differences."""
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
        for t in DB.FilteredElementCollector(doc).OfClass(_SYSTEM_TYPE_CLASSES[kind]):
            name = get_name(t)
            if any(w in name.lower() for w in target_words):
                matches.append(name)
            if len(matches) >= 15:
                break
    return matches


# ---------------------- lengths ---------------------------------------------

def parse_feet_inches(text):
    """1'-6"  1' 6 1/2"  18"  1'  1.5'  3/4"  -> feet (float). ValueError if invalid.
    Plain string parsing on purpose: complex regexes have crashed IronPython in Revit."""
    t = (text or '').strip().replace(u'’', "'").replace(u'”', '"').replace("''", '"')
    neg = t.startswith('-')
    if neg:
        t = t[1:].strip()
    if not t:
        raise ValueError(text)
    ft, rest = 0.0, t
    if "'" in t:
        head, rest = t.split("'", 1)
        ft = float(head.strip())
    inch = 0.0
    for tok in rest.replace('"', ' ').replace('-', ' ').split():
        if '/' in tok:
            n, d = tok.split('/', 1)
            inch += float(n) / float(d)
        else:
            inch += float(tok)
    return (-1 if neg else 1) * (ft + inch / 12.0)


def to_internal(text, unit, profile_units):
    """CSV cell -> Revit internal units (feet). unit: 'in', 'ft', 'ft-in' or None."""
    t = (text or '').strip()
    if "'" in t or '"' in t or unit == 'ft-in':
        return parse_feet_inches(t)
    v = float(t)
    if unit == 'ft':
        return v
    if unit == 'in':
        return v / 12.0
    return DB.UnitUtils.ConvertToInternalUnits(v, profile_units)


def fmt_inches(feet):
    """1.5 -> 18   1.375 -> 16.5 (decimal inches, 1/16" precision)"""
    inches = round(feet * 12 * 16) / 16.0
    return ('%.4f' % inches).rstrip('0').rstrip('.')


def fmt_feet_inches(feet, denom=16):
    """1.5 -> 1'-6"   1.375 -> 1'-4 1/2"   0.5 -> 0'-6" """
    sign = '-' if feet < 0 else ''
    total = int(round(abs(feet) * 12 * denom))
    ft, rem = divmod(total, 12 * denom)
    whole, frac = divmod(rem, denom)
    s = '%s%d\'-%d' % (sign, ft, whole)
    if frac:
        g = _gcd(frac, denom)
        s += ' %d/%d' % (frac // g, denom // g)
    return s + '"'


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


# ---------------------- CSV -------------------------------------------------

def _split_unit(col):
    """'Width (ft-in)' -> ('Width', 'ft-in'); 'Width' -> ('Width', None)."""
    low = col.lower().replace(' ', '')
    for unit in ('ft-in', 'ft', 'in'):
        if low.endswith('(%s)' % unit):
            return col[:col.rfind('(')].strip(), unit
    return col, None


def read_csv_rows(path):
    """(rows, units): rows are dicts keyed by column name without the unit
    suffix; units is {column: 'in' | 'ft' | 'ft-in'} for the suffixed columns.
    The delimiter (',' or ';') is taken from the header line; csv.Sniffer is
    not used because its regexes can crash IronPython on quoted feet-inches."""
    with io.open(path, "r", encoding="utf-8") as f:
        text = f.read().replace(u'﻿', u'')
    header = text.split(u'\n', 1)[0]
    delim = ';' if header.count(';') > header.count(',') else ','
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    units, rows = {}, []
    for row in reader:
        clean = {}
        for k, v in row.items():
            if k is None:
                continue
            k, unit = _split_unit(k.strip())
            if unit:
                units[k] = unit
            clean[k] = v
        rows.append(clean)
    return rows, units


def normalize_row(row, profile, units):
    """Converts the length columns of row to the profile units (as numeric
    strings), so name_fn builds the same names whatever format the CSV uses.
    Returns (row, None) or (None, error message)."""
    out = dict(row)
    for col in value_columns(profile):
        if col not in row:
            return None, "missing column '{}'".format(col)
        try:
            feet = to_internal(row[col], units.get(col), profile["units"])
        except (TypeError, ValueError):
            return None, "invalid value '{}' in column '{}'".format(row[col], col)
        v = DB.UnitUtils.ConvertFromInternalUnits(feet, profile["units"])
        v = round(v * 16) / 16.0   # 1/16" precision (feet -> inches rounding noise)
        out[col] = ('%.4f' % v).rstrip('0').rstrip('.')
    return out, None


def find_csv_for_profile(folder, profile_name):
    """Accepts both '<SPREADSHEET_NAME> - <profile>.csv' (default name
    exported by Google Sheets) and '<profile>.csv' (plain name)."""
    for filename in ("{0} - {1}.csv".format(SPREADSHEET_NAME, profile_name),
                     "{0}.csv".format(profile_name)):
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            return path
    return None


# ---------------------- export ----------------------------------------------

def _type_mark(t):
    p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_MARK)
    mark = p.AsString() if p is not None and p.HasValue else ''
    if not mark:
        mark = get_name(t).split(' - ')[0].strip()
    return mark


def types_for_profile(doc, profile):
    """Existing types that belong to the profile: every type of the base family
    (loadable) or every wall/floor type with the same structural material as the
    base type (system). Returns (types, base or None)."""
    if profile["kind"] == "family_symbol":
        base = find_base_symbol(doc, profile)
        if base is None:
            return [], None
        return [doc.GetElement(i) for i in base.Family.GetFamilySymbolIds()], base
    base = find_base_system_type(doc, profile)
    if base is None:
        return [], None
    _, mat = structural_layer_info(base)
    out = []
    for t in DB.FilteredElementCollector(doc).OfClass(_SYSTEM_TYPE_CLASSES[profile["kind"]]):
        if profile["kind"] == "wall_type" and t.Kind != DB.WallKind.Basic:
            continue
        w, m = structural_layer_info(t)
        if w is not None and m == mat:
            out.append(t)
    return out, base


def export_profile(doc, profile_name, folder, fmt):
    """Writes '<profile>.csv' with the existing types. fmt: 'in' or 'ft-in'.
    Returns (path, rows written, note)."""
    profile = PROFILES[profile_name]
    cols = value_columns(profile)
    types, base = types_for_profile(doc, profile)
    note = '' if base is not None else 'base type/family not found: header only'
    rows = []
    for t in sorted(types, key=get_name):
        vals = []
        for col in cols:
            if profile["kind"] == "family_symbol":
                p = t.LookupParameter(profile["param_map"][col])
                feet = p.AsDouble() if p is not None and p.StorageType == DB.StorageType.Double else None
            else:
                feet = structural_layer_info(t)[0]
            vals.append('' if feet is None else (fmt_inches(feet) if fmt == 'in' else fmt_feet_inches(feet)))
        rows.append([_type_mark(t)] + vals + [get_name(t)])
    path = os.path.join(folder, '{0}.csv'.format(profile_name))
    header = ['Mark Type'] + ['{0} ({1})'.format(c, fmt) for c in cols] + ['Current Type Name']
    lines = [u','.join(header)]
    for r in rows:
        lines.append(u','.join(u'"%s"' % (c or u'').replace(u'"', u'""') for c in r))
    # one write with a single BOM: IronPython's utf-8-sig writes a BOM on every write()
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(u'﻿' + u'\r\n'.join(lines) + u'\r\n')
    return path, len(rows), note
