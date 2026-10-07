# -*- coding: utf-8 -*-
"""Joins the geometry of all elements in contact between two categories.

Workflow:
1. Select one element from category A (gets join priority).
2. Select one element from category B.
3. The script collects ALL elements of category A and B visible in the
   active view, detects which ones are in real contact (not just
   bounding box overlap), and joins them, giving geometric priority
   to category A.
"""

from pyrevit import revit, DB, forms
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView

# 1. Select the two reference elements ---------------------------------------
try:
    ref1 = uidoc.Selection.PickObject(
        ObjectType.Element,
        "Select the FIRST element (its category will get join priority)"
    )
    ref2 = uidoc.Selection.PickObject(
        ObjectType.Element,
        "Select the SECOND element (the other category)"
    )
except OperationCanceledException:
    forms.alert("Selection cancelled.", exitscript=True)

elem1 = doc.GetElement(ref1)
elem2 = doc.GetElement(ref2)

if not elem1.Category or not elem2.Category:
    forms.alert("One of the elements has no valid category.", exitscript=True)

cat1_id = elem1.Category.Id
cat2_id = elem2.Category.Id

if cat1_id == cat2_id:
    forms.alert("Select elements from TWO different categories.", exitscript=True)

# 2. Collect all elements of each category in the active view ---------------
elems_cat1 = DB.FilteredElementCollector(doc, view.Id) \
    .OfCategoryId(cat1_id) \
    .WhereElementIsNotElementType() \
    .ToElements()

elems_cat2 = DB.FilteredElementCollector(doc, view.Id) \
    .OfCategoryId(cat2_id) \
    .WhereElementIsNotElementType() \
    .ToElements()

# 3. Detect real contact and join, priority to category 1 -------------------
joined_count = 0
skipped_count = 0
error_count = 0

with revit.Transaction("Join geometry by category"):
    for e1 in elems_cat1:
        bb1 = e1.get_BoundingBox(view)
        if not bb1:
            continue

        for e2 in elems_cat2:
            if e1.Id == e2.Id:
                continue

            bb2 = e2.get_BoundingBox(view)
            if not bb2:
                continue

            # Quick bounding box filter before the real geometric check
            if (bb1.Max.X < bb2.Min.X or bb1.Min.X > bb2.Max.X or
                    bb1.Max.Y < bb2.Min.Y or bb1.Min.Y > bb2.Max.Y or
                    bb1.Max.Z < bb2.Min.Z or bb1.Min.Z > bb2.Max.Z):
                continue

            try:
                already_joined = DB.JoinGeometryUtils.AreElementsJoined(doc, e1, e2)

                if not already_joined:
                    intersects = DB.ElementIntersectsElementFilter(e1).PassesFilter(e2)
                    if not intersects:
                        continue

                    DB.JoinGeometryUtils.JoinGeometry(doc, e1, e2)
                    joined_count += 1
                else:
                    skipped_count += 1

                # The argument order in JoinGeometry does NOT define which
                # element cuts which; Revit decides that using its internal
                # category join-priority hierarchy. We force e1 (category 1)
                # to be the cutting element, regardless of that hierarchy.
                if not DB.JoinGeometryUtils.IsCuttingElementInJoin(doc, e1, e2):
                    DB.JoinGeometryUtils.SwitchJoinOrder(doc, e1, e2)

            except Exception:
                error_count += 1
                continue

forms.alert(
    "Join completed.\n\n"
    "New joins: {}\n"
    "Already joined: {}\n"
    "Errors: {}".format(joined_count, skipped_count, error_count)
)