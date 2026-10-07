# -*- coding: utf-8 -*-
"""Attach tops/bases of vertical elements to levels.

For each end (base / top) of a wall or column, takes its actual elevation
and references it to a level:
  - if the end is within `snap_tol` of a level, it goes to that level with
    offset 0 (the element moves slightly to sit exactly on the level);
  - otherwise the base goes to the level at or below it and the top to the
    level at or above it, with the offset that keeps the geometry exactly
    where it is.
Walls with an unconnected height get their top constrained to a level.
Tops attached to floors/roofs, and slanted columns, are left alone.
"""
import Autodesk.Revit.DB as DB

BIP = DB.BuiltInParameter

CATEGORIES = [
    ('Walls', DB.BuiltInCategory.OST_Walls),
    ('Structural Columns', DB.BuiltInCategory.OST_StructuralColumns),
    ('Columns', DB.BuiltInCategory.OST_Columns),
]


def _p(e, bip):
    return e.get_Parameter(bip)


def _levels(doc):
    return sorted(DB.FilteredElementCollector(doc).OfClass(DB.Level), key=lambda l: l.Elevation)


def _nearest(levels, z):
    return min(levels, key=lambda l: abs(l.Elevation - z))


def _params(e):
    """(base level p, base offset p, top level p, top offset p, unconnected height p or None).
    None if the element is not a supported wall/column."""
    if isinstance(e, DB.Wall):
        return (_p(e, BIP.WALL_BASE_CONSTRAINT), _p(e, BIP.WALL_BASE_OFFSET),
                _p(e, BIP.WALL_HEIGHT_TYPE), _p(e, BIP.WALL_TOP_OFFSET), _p(e, BIP.WALL_USER_HEIGHT_PARAM))
    if isinstance(e, DB.FamilyInstance):
        return (_p(e, BIP.FAMILY_BASE_LEVEL_PARAM), _p(e, BIP.FAMILY_BASE_LEVEL_OFFSET_PARAM),
                _p(e, BIP.FAMILY_TOP_LEVEL_PARAM), _p(e, BIP.FAMILY_TOP_LEVEL_OFFSET_PARAM), None)
    return None


def _skip_reason(e):
    if isinstance(e, DB.Wall):
        if e.WallType.Kind != DB.WallKind.Basic:
            return 'curtain/stacked wall'
        if not isinstance(e.Location, DB.LocationCurve):
            return 'no location line'
        return None
    if isinstance(e, DB.FamilyInstance):
        if getattr(e, 'IsSlantedColumn', False):
            return 'slanted column'
        if not isinstance(e.Location, DB.LocationPoint):
            return 'not a vertical column'
        return None
    return 'unsupported element'


def plan(doc, elements, do_base=True, do_top=True, snap_tol=1.0 / 12):
    """What would change, without modifying anything.
    Returns [(element, changes dict, description list, skip reason)].
    changes: {'base': (level, offset), 'top': (level, offset)}"""
    levels = _levels(doc)
    out = []
    for e in elements:
        why = _skip_reason(e)
        ps = _params(e) if not why else None
        if not why and (ps is None or ps[0] is None or ps[2] is None):
            why = 'no level parameters'
        if why:
            out.append((e, {}, [], why))
            continue
        bl_p, bo_p, tl_p, to_p, uh_p = ps
        base_lvl = doc.GetElement(bl_p.AsElementId())
        if base_lvl is None:
            out.append((e, {}, [], 'no base level'))
            continue
        z_base = base_lvl.Elevation + bo_p.AsDouble()
        top_id = tl_p.AsElementId()
        top_lvl = doc.GetElement(top_id) if top_id != DB.ElementId.InvalidElementId else None
        if top_lvl is not None:
            z_top = top_lvl.Elevation + to_p.AsDouble()
        elif uh_p is not None:
            z_top = z_base + uh_p.AsDouble()
        else:
            z_top = None
        changes, desc = {}, []

        def target(z, end):
            """Snap to any level within snap_tol; otherwise the base goes to the level
            at or below it (offset >= 0) and the top to the level at or above it
            (offset <= 0), the usual structural convention."""
            near = _nearest(levels, z)
            if abs(z - near.Elevation) <= snap_tol:
                return near, 0.0
            if end == 'base':
                cand = [l for l in levels if l.Elevation <= z]
                lvl = cand[-1] if cand else near
            else:
                cand = [l for l in levels if l.Elevation >= z]
                lvl = cand[0] if cand else near
            return lvl, z - lvl.Elevation

        base_locked = bo_p.IsReadOnly or bl_p.IsReadOnly
        if do_base and not base_locked:
            lvl, off = target(z_base, 'base')
            if lvl.Id != base_lvl.Id or abs(off - bo_p.AsDouble()) > 1e-6:
                changes['base'] = (lvl, off)
                desc.append(u'Base: %s %+.0f mm -> %s %+.0f mm' % (
                    base_lvl.Name, bo_p.AsDouble() * 304.8, lvl.Name, off * 304.8))
        # attached tops (walls to floors/roofs, columns to beams/floors) lock their offset
        attached = to_p.IsReadOnly or tl_p.IsReadOnly or (
            isinstance(e, DB.Wall) and _p(e, BIP.WALL_TOP_IS_ATTACHED) is not None
            and _p(e, BIP.WALL_TOP_IS_ATTACHED).AsInteger() == 1)
        if do_top and z_top is not None and not attached:
            lvl, off = target(z_top, 'top')
            if top_lvl is None or lvl.Id != top_lvl.Id or abs(off - to_p.AsDouble()) > 1e-6:
                new_base_z = (changes['base'][0].Elevation + changes['base'][1]) if 'base' in changes else z_base
                if lvl.Elevation + off > new_base_z + 1e-3:
                    changes['top'] = (lvl, off)
                    desc.append(u'Top: %s %+.0f mm -> %s %+.0f mm' % (
                        top_lvl.Name if top_lvl else 'Unconnected', (to_p.AsDouble() if top_lvl else 0) * 304.8,
                        lvl.Name, off * 304.8))
        notes = []
        if base_locked and do_base:
            notes.append('base attached (not changed)')
        if attached and do_top:
            notes.append('top attached (not changed)')
        out.append((e, changes, desc, ', '.join(notes) or None))
    return out


def apply(doc, e, changes):
    """Applies one plan() entry. Must run inside an open transaction."""
    bl_p, bo_p, tl_p, to_p, uh_p = _params(e)
    if 'base' in changes:
        lvl, off = changes['base']
        bl_p.Set(lvl.Id)
        bo_p.Set(off)
    if 'top' in changes:
        lvl, off = changes['top']
        tl_p.Set(lvl.Id)
        to_p.Set(off)
