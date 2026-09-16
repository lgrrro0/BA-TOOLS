# -*- coding: utf-8 -*-
"""Splits selected vertical elements (columns and/or walls) at every floor
they cross, turning a single multi-level element into one separate element
per level segment.

Workflow:
1. Select one or more columns and/or walls (Finish to confirm).
2. Choose the floor reference to cut at: Top, Center, or Bottom.
3. For each selected element, the script finds the floors that overlap it
   in plan, computes the cut elevation for each one, shrinks the original
   element down to the first segment, and creates a copy for every
   remaining segment. Each new segment's Base Level (or Base Constraint,
   for walls) is set to the actual Level the cutting floor is hosted on,
   not just a numeric offset from the original base level.

Limitations (first version):
- Assumes vertical (non-slanted) columns and straight walls.
- Does not handle columns/walls attached to a floor or roof (attachment
  offsets are ignored).
- Floor top/bottom are approximated from the floor's bounding box, so
  sloped floors are only approximately handled.
- Walls with hosted elements (doors/windows) may lose those hosts on the
  segments that no longer contain their insertion point.
"""
__title__ = "Split Verticals\nat Floors"
__author__ = "Luis"

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc

COLUMN_CATS = (
    int(DB.BuiltInCategory.OST_Columns),
    int(DB.BuiltInCategory.OST_StructuralColumns),
)
WALL_CAT = int(DB.BuiltInCategory.OST_Walls)
VERTICAL_CATS = COLUMN_CATS + (WALL_CAT,)

TOLERANCE = 0.01  # feet (~3 mm), used to avoid zero-length segments and to dedupe cuts


# ---------------------------------------------------------------------------
# 1. Select columns / walls
# ---------------------------------------------------------------------------
# Clear any pre-existing selection first: if it contains elements that do
# not pass the filter below, PickObjects fails immediately without letting
# you pick anything.
uidoc.Selection.SetElementIds(List[DB.ElementId]())

try:
    refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        "Select columns/walls to split at floors, then click Finish"
    )
except OperationCanceledException:
    forms.alert("Selection cancelled.", exitscript=True)
except Exception:
    import traceback
    output = script.get_output()
    output.print_md("### Selection error")
    output.print_code(traceback.format_exc())
    forms.alert("An error occurred while selecting. See the output window for details.", exitscript=True)

all_selected = [doc.GetElement(r) for r in refs]
elements = [e for e in all_selected if e.Category and e.Category.Id.Value in VERTICAL_CATS]
skipped_invalid = len(all_selected) - len(elements)

if not elements:
    forms.alert("No valid columns or walls were selected.", exitscript=True)

if skipped_invalid:
    forms.alert(
        "{} selected element(s) were not columns or walls and were ignored.".format(skipped_invalid)
    )

# ---------------------------------------------------------------------------
# 2. Ask which floor reference to cut at
# ---------------------------------------------------------------------------
cut_options = ["Floor Top", "Floor Center", "Floor Bottom"]
cut_choice = forms.CommandSwitchWindow.show(
    cut_options,
    message="Cut the selected verticals at which floor reference?"
)
if not cut_choice:
    forms.alert("Operation cancelled.", exitscript=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
all_floors = DB.FilteredElementCollector(doc) \
    .OfCategory(DB.BuiltInCategory.OST_Floors) \
    .WhereElementIsNotElementType() \
    .ToElements()


def get_vertical_extent(element, is_column):
    """Returns (base_z, top_z, base_level, top_is_unconnected)."""
    if is_column:
        base_level_param = element.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
        base_offset_param = element.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        top_level_param = element.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        top_offset_param = element.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)

        base_level = doc.GetElement(base_level_param.AsElementId())
        base_z = base_level.Elevation + base_offset_param.AsDouble()

        top_level = doc.GetElement(top_level_param.AsElementId())
        top_z = top_level.Elevation + top_offset_param.AsDouble()
        top_is_unconnected = False
    else:
        base_level_param = element.get_Parameter(DB.BuiltInParameter.WALL_BASE_CONSTRAINT)
        base_offset_param = element.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET)
        top_level_param = element.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
        top_offset_param = element.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET)
        height_param = element.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)

        base_level = doc.GetElement(base_level_param.AsElementId())
        base_z = base_level.Elevation + base_offset_param.AsDouble()

        top_level_id = top_level_param.AsElementId()
        top_is_unconnected = (top_level_id == DB.ElementId.InvalidElementId)
        if top_is_unconnected:
            top_z = base_z + height_param.AsDouble()
        else:
            top_level = doc.GetElement(top_level_id)
            top_z = top_level.Elevation + top_offset_param.AsDouble()

    return base_z, top_z, base_level, top_is_unconnected


def get_cut_levels(element, base_z, top_z, choice):
    """Finds floors overlapping the element in plan and returns the sorted,
    deduplicated list of (cut_z, floor_level) tuples strictly inside
    (base_z, top_z). floor_level is the Level element the floor is hosted
    on, so each new segment's Base Level can match the floor that cut it."""
    elem_bb = element.get_BoundingBox(None)
    if not elem_bb:
        return []

    cuts = []
    for floor in all_floors:
        floor_bb = floor.get_BoundingBox(None)
        if not floor_bb:
            continue

        # quick plan (XY) overlap test
        if (elem_bb.Max.X < floor_bb.Min.X or elem_bb.Min.X > floor_bb.Max.X or
                elem_bb.Max.Y < floor_bb.Min.Y or elem_bb.Min.Y > floor_bb.Max.Y):
            continue

        floor_bottom = floor_bb.Min.Z
        floor_top = floor_bb.Max.Z

        if choice == "Floor Top":
            cut_z = floor_top
        elif choice == "Floor Bottom":
            cut_z = floor_bottom
        else:
            cut_z = (floor_top + floor_bottom) / 2.0

        if base_z + TOLERANCE < cut_z < top_z - TOLERANCE:
            try:
                floor_level = doc.GetElement(floor.LevelId)
            except Exception:
                floor_level = None
            cuts.append((cut_z, floor_level))

    cuts.sort(key=lambda c: c[0])
    deduped = []
    for z, lvl in cuts:
        if not deduped or abs(z - deduped[-1][0]) > TOLERANCE:
            deduped.append((z, lvl))
    return deduped


def set_segment(elem, seg_bottom, seg_top, seg_base_level, ref_level, is_column, top_is_unconnected):
    base_elev = seg_base_level.Elevation
    ref_elev = ref_level.Elevation
    if is_column:
        elem.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM).Set(seg_base_level.Id)
        elem.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM).Set(seg_bottom - base_elev)
        elem.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(ref_level.Id)
        elem.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(seg_top - ref_elev)
    else:
        elem.get_Parameter(DB.BuiltInParameter.WALL_BASE_CONSTRAINT).Set(seg_base_level.Id)
        elem.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(seg_bottom - base_elev)
        if top_is_unconnected:
            elem.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM).Set(seg_top - seg_bottom)
        else:
            elem.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(ref_level.Id)
            elem.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(seg_top - ref_elev)


# ---------------------------------------------------------------------------
# 3. Split each element
# ---------------------------------------------------------------------------
split_count = 0
segments_created = 0
skipped_no_cuts = 0
error_count = 0

with revit.Transaction("Split verticals at floors"):
    for element in elements:
        try:
            is_column = element.Category.Id.Value in COLUMN_CATS
            base_z, top_z, base_level, top_is_unconnected = get_vertical_extent(element, is_column)

            cut_data = get_cut_levels(element, base_z, top_z, cut_choice)
            if not cut_data:
                skipped_no_cuts += 1
                continue

            # boundaries: (elevation, base_level_for_the_segment_that_starts_here)
            # the last entry has no "base level" since it only serves as the
            # upper bound of the last segment.
            boundaries = [(base_z, base_level)] + cut_data + [(top_z, None)]

            # Create copies for every segment EXCEPT the first, while the
            # original element is still at full height (so the copies are
            # not accidentally created already-shrunk).
            new_segments = []
            for i in range(1, len(boundaries) - 1):
                new_ids = DB.ElementTransformUtils.CopyElement(doc, element.Id, DB.XYZ.Zero)
                new_elem = doc.GetElement(list(new_ids)[0])
                seg_bottom, seg_base_level = boundaries[i]
                seg_top, _ = boundaries[i + 1]
                seg_base_level = seg_base_level or base_level  # fallback if the floor had no level
                new_segments.append((new_elem, seg_bottom, seg_top, seg_base_level))

            # Now shrink the original element down to the first segment
            # (keeps its original Base Level, since it was not cut by a floor).
            seg0_bottom, seg0_base_level = boundaries[0]
            seg0_top, _ = boundaries[1]
            set_segment(element, seg0_bottom, seg0_top, seg0_base_level, base_level, is_column, top_is_unconnected)
            segments_created += 1

            # Apply the remaining segments to the copies, each referencing
            # the level of the floor that produced its lower cut.
            for new_elem, seg_bottom, seg_top, seg_base_level in new_segments:
                set_segment(new_elem, seg_bottom, seg_top, seg_base_level, base_level, is_column, top_is_unconnected)
                segments_created += 1

            split_count += 1

        except Exception:
            error_count += 1
            continue

forms.alert(
    "Split completed.\n\n"
    "Elements split: {}\n"
    "Total segments created: {}\n"
    "Skipped (no floors crossed): {}\n"
    "Errors: {}".format(split_count, segments_created, skipped_no_cuts, error_count)
)