# -*- coding: utf-8 -*-
"""Elevation views framed on individual walls (any angle, straight or arc)."""
import math

from pyrevit import DB

MARKER_OFFSET = 3.0   # ft from the wall face to the elevation marker
CROP_MARGIN = 1.0     # ft around the wall in the crop region
FAR_MARGIN = 1.0      # ft behind the wall for the far clip


def eid(element_id):
    return getattr(element_id, 'Value', None) or element_id.IntegerValue


def elevation_type_id(doc):
    """Default elevation view type, or the first one available."""
    tid = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.ViewTypeElevation)
    if tid != DB.ElementId.InvalidElementId and doc.GetElement(tid) is not None:
        return tid
    for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if vft.ViewFamily == DB.ViewFamily.Elevation:
            return vft.Id
    return None


def host_plan(doc, wall, active_view=None):
    """Plan view that will host the marker: the active plan, else a plan of
    the wall's level, else any plan."""
    if isinstance(active_view, DB.ViewPlan) and not active_view.IsTemplate:
        return active_view
    plans = [v for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan)
             if not v.IsTemplate and v.ViewType in (DB.ViewType.FloorPlan, DB.ViewType.EngineeringPlan)]
    for v in plans:
        if v.GenLevel is not None and v.GenLevel.Id == wall.LevelId:
            return v
    return plans[0] if plans else None


def _frame(wall, interior):
    """(corner points of the wall, unit normal pointing to the viewer)."""
    loc = wall.Location
    if not isinstance(loc, DB.LocationCurve) or not loc.Curve.IsBound:
        return None, None
    crv = loc.Curve
    chord = crv.GetEndPoint(1) - crv.GetEndPoint(0)
    chord = DB.XYZ(chord.X, chord.Y, 0)
    if chord.GetLength() < 1e-6:
        return None, None
    normal = chord.Normalize().CrossProduct(DB.XYZ.BasisZ)
    if normal.DotProduct(wall.Orientation) < 0:
        normal = normal.Negate()
    if interior:
        normal = normal.Negate()

    bb = wall.get_BoundingBox(None)
    half = wall.Width / 2.0
    pts = []
    for p in crv.Tessellate():
        for z in (bb.Min.Z, bb.Max.Z):
            for s in (-half, half):
                pts.append(DB.XYZ(p.X, p.Y, z) + normal * s)
    return pts, normal


def _unique_name(doc, base):
    names = set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    name, n = base, 1
    while name in names:
        n += 1
        name = '%s (%d)' % (base, n)
    return name


def create(doc, wall, plan, type_id, interior=False):
    """Creates an elevation looking at the exterior (or interior) face of the
    wall, cropped to it. Must run inside a transaction. Returns the view."""
    pts, normal = _frame(wall, interior)
    if pts is None:
        raise ValueError('The wall has no bound location curve.')

    right = DB.XYZ.BasisZ.CrossProduct(normal)
    along = [p.DotProduct(right) for p in pts]
    depth = max(p.DotProduct(normal) for p in pts) + MARKER_OFFSET
    zmin = min(p.Z for p in pts)
    origin = right * ((min(along) + max(along)) / 2.0) + normal * depth + DB.XYZ.BasisZ * zmin

    marker = DB.ElevationMarker.CreateElevationMarker(doc, type_id, origin, plan.Scale)
    view = marker.CreateElevation(doc, plan.Id, 0)
    doc.Regenerate()

    # turn the marker so the view looks straight at the wall face
    cur = view.ViewDirection
    angle = math.atan2(cur.X * normal.Y - cur.Y * normal.X, cur.X * normal.X + cur.Y * normal.Y)
    if abs(angle) > 1e-9:
        axis = DB.Line.CreateBound(origin, origin + DB.XYZ.BasisZ)
        DB.ElementTransformUtils.RotateElement(doc, marker.Id, axis, angle)
        doc.Regenerate()

    crop = view.CropBox
    inv = crop.Transform.Inverse
    local = [inv.OfPoint(p) for p in pts]
    crop.Min = DB.XYZ(min(p.X for p in local) - CROP_MARGIN, min(p.Y for p in local) - CROP_MARGIN, crop.Min.Z)
    crop.Max = DB.XYZ(max(p.X for p in local) + CROP_MARGIN, max(p.Y for p in local) + CROP_MARGIN, crop.Max.Z)
    view.CropBox = crop
    view.CropBoxActive = True
    view.CropBoxVisible = True

    far = view.get_Parameter(DB.BuiltInParameter.VIEWER_BOUND_OFFSET_FAR)
    if far is not None and not far.IsReadOnly:
        far.Set(FAR_MARGIN - min(p.Z for p in local))

    # structural views hide non-bearing walls: show the wall anyway
    disc = view.get_Parameter(DB.BuiltInParameter.VIEW_DISCIPLINE)
    bearing = wall.get_Parameter(DB.BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT)
    if (disc is not None and not disc.IsReadOnly and disc.AsInteger() == int(DB.ViewDiscipline.Structural)
            and (bearing is None or bearing.AsInteger() == 0)):
        disc.Set(int(DB.ViewDiscipline.Coordination))

    mark = wall.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
    label = mark.AsString() if mark is not None and mark.AsString() else str(eid(wall.Id))
    view.Name = _unique_name(doc, 'Wall Elevation %s%s' % (label, ' (Interior)' if interior else ''))
    return view
