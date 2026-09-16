# -*- coding: utf-8 -*-
"""Detects every model category currently visible in the active view (both
in the current model and in any linked models), lets the user pick which
categories to tag via two checkbox dialogs, then places the default
(by-category) tag at the center of the VISIBLE (crop-clipped) portion of
each selected element.
"""

from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView

output = script.get_output()

# Tagging only works on 2D views (plan, section, elevation, drafting) -------
UNSUPPORTED_VIEW_TYPES = [
    DB.ViewType.ThreeD,
    DB.ViewType.Schedule,
    DB.ViewType.Legend,
    DB.ViewType.DrawingSheet,
]

if view.ViewType in UNSUPPORTED_VIEW_TYPES:
    forms.alert(
        "The active view type does not support tagging.\n"
        "Switch to a plan, section, elevation, or drafting view and try again.",
        title="Unsupported view"
    )
    script.exit()


# ---- Helpers ----------------------------------------------------------------

def world_bbox_from_local(bbox, transform):
    """Transforms a BoundingBoxXYZ's 8 corners through `transform` and
    returns an axis-aligned (min, max) pair in the target coordinate
    system. Needed because rotated views/links don't share the same
    local axes as the model."""
    corners = [
        DB.XYZ(x, y, z)
        for x in (bbox.Min.X, bbox.Max.X)
        for y in (bbox.Min.Y, bbox.Max.Y)
        for z in (bbox.Min.Z, bbox.Max.Z)
    ]
    world_corners = [transform.OfPoint(c) for c in corners]
    xs = [p.X for p in world_corners]
    ys = [p.Y for p in world_corners]
    zs = [p.Z for p in world_corners]
    return DB.XYZ(min(xs), min(ys), min(zs)), DB.XYZ(max(xs), max(ys), max(zs))


def clipped_center(bbox_min, bbox_max, crop_min, crop_max):
    """Center of the overlap between an element's bounding box and the
    view's crop area (both already in the same world coordinate system).
    Falls back to the full element center if there is no crop, or if the
    element doesn't overlap the crop in X/Y."""
    if crop_min is not None:
        min_x = max(bbox_min.X, crop_min.X)
        max_x = min(bbox_max.X, crop_max.X)
        min_y = max(bbox_min.Y, crop_min.Y)
        max_y = min(bbox_max.Y, crop_max.Y)
        if min_x <= max_x and min_y <= max_y:
            return DB.XYZ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0,
                           (bbox_min.Z + bbox_max.Z) / 2.0)
    return DB.XYZ((bbox_min.X + bbox_max.X) / 2.0,
                   (bbox_min.Y + bbox_max.Y) / 2.0,
                   (bbox_min.Z + bbox_max.Z) / 2.0)


# ---- World-space crop rectangle for the active view --------------------------
crop_min, crop_max = None, None
if view.CropBoxActive:
    crop_min, crop_max = world_bbox_from_local(view.CropBox, view.CropBox.Transform)


# ---- 1. Detect categories in the CURRENT MODEL, visible in the active view ---
host_elements = DB.FilteredElementCollector(doc, view.Id) \
    .WhereElementIsNotElementType() \
    .ToElements()

host_categories = {}  # {category name: Category}
host_elements_by_cat = {}  # {category name: [elements]}
for el in host_elements:
    cat = el.Category
    if cat is None or cat.CategoryType != DB.CategoryType.Model:
        continue
    host_categories[cat.Name] = cat
    host_elements_by_cat.setdefault(cat.Name, []).append(el)


# ---- 2. Detect linked models visible in the view, and their categories -------
link_instances = DB.FilteredElementCollector(doc, view.Id) \
    .OfCategory(DB.BuiltInCategory.OST_RvtLinks) \
    .WhereElementIsNotElementType() \
    .ToElements()

# {link_name: {"instance": RevitLinkInstance, "doc": Document,
#              "elements_by_cat": {cat_name: [elements]}}}
linked_data = {}
for link in link_instances:
    link_doc = link.GetLinkDocument()
    if link_doc is None:
        continue  # link unloaded

    link_elements = DB.FilteredElementCollector(link_doc) \
        .WhereElementIsNotElementType() \
        .ToElements()

    elements_by_cat = {}
    for el in link_elements:
        cat = el.Category
        if cat is None or cat.CategoryType != DB.CategoryType.Model:
            continue
        elements_by_cat.setdefault(cat.Name, []).append(el)

    if elements_by_cat:
        linked_data[link.Name] = {
            "instance": link,
            "doc": link_doc,
            "elements_by_cat": elements_by_cat,
        }


if not host_categories and not linked_data:
    forms.alert(
        "No taggable model categories were found in the active view.",
        title="Nothing to tag"
    )
    script.exit()


# ---- 3. Section 1: pick categories from the CURRENT MODEL ---------------------
selected_host_cats = []
if host_categories:
    selected_host_cats = forms.SelectFromList.show(
        sorted(host_categories.keys()),
        title="Current Model - Select Categories to Tag",
        button_name="Next",
        multiselect=True
    ) or []


# ---- 4. Section 2: pick categories from LINKED MODELS --------------------------
# label -> (link_name, category_name)
link_choice_map = {}
if linked_data:
    display_list = []
    for link_name, info in linked_data.items():
        for cat_name in sorted(info["elements_by_cat"].keys()):
            label = "{}  :  {}".format(link_name, cat_name)
            display_list.append(label)
            link_choice_map[label] = (link_name, cat_name)

    selected_labels = forms.SelectFromList.show(
        sorted(display_list),
        title="Linked Models - Select Categories to Tag",
        button_name="Tag Elements",
        multiselect=True
    ) or []
else:
    selected_labels = []

selected_link_pairs = [link_choice_map[l] for l in selected_labels]

if not selected_host_cats and not selected_link_pairs:
    forms.alert("No categories selected.", title="Nothing to do")
    script.exit()


# ---- 5. Figure out which elements already have a tag in this view -----------
existing_tags = DB.FilteredElementCollector(doc, view.Id) \
    .OfClass(DB.IndependentTag) \
    .ToElements()

already_tagged_local_ids = set()
already_tagged_link_keys = set()  # (linkInstanceId, linkedElementId)

for t in existing_tags:
    try:
        for leid in t.GetTaggedElementIds():
            if leid.LinkInstanceId != DB.ElementId.InvalidElementId:
                already_tagged_link_keys.add(
                    (leid.LinkInstanceId.Value, leid.LinkedElementId.Value))
            else:
                already_tagged_local_ids.add(leid.HostElementId.Value)
    except Exception:
        continue


# ---- 6. Place tags -------------------------------------------------------------
tagged_count = 0
skipped_already_tagged = 0
error_count = 0
error_categories = set()

with revit.Transaction("Tag elements by category (current + linked models)"):

    # -- Current model elements --
    for cat_name in selected_host_cats:
        for el in host_elements_by_cat.get(cat_name, []):
            if el.Id.Value in already_tagged_local_ids:
                skipped_already_tagged += 1
                continue

            bbox = el.get_BoundingBox(view)
            if bbox is None:
                error_count += 1
                continue

            center = clipped_center(bbox.Min, bbox.Max, crop_min, crop_max)

            try:
                reference = DB.Reference(el)
                DB.IndependentTag.Create(
                    doc, view.Id, reference, False,
                    DB.TagMode.TM_ADDBY_CATEGORY,
                    DB.TagOrientation.Horizontal, center
                )
                tagged_count += 1
            except Exception:
                error_count += 1
                error_categories.add(cat_name)
                continue

    # -- Linked model elements --
    for link_name, cat_name in selected_link_pairs:
        info = linked_data[link_name]
        link_instance = info["instance"]
        link_transform = link_instance.GetTransform()

        for el in info["elements_by_cat"].get(cat_name, []):
            key = (link_instance.Id.Value, el.Id.Value)
            if key in already_tagged_link_keys:
                skipped_already_tagged += 1
                continue

            bbox = el.get_BoundingBox(None)  # bbox in the LINKED doc's own space
            if bbox is None:
                error_count += 1
                continue

            world_min, world_max = world_bbox_from_local(bbox, link_transform)
            center = clipped_center(world_min, world_max, crop_min, crop_max)

            try:
                base_ref = DB.Reference(el)
                link_ref = base_ref.CreateLinkReference(link_instance)
                DB.IndependentTag.Create(
                    doc, view.Id, link_ref, False,
                    DB.TagMode.TM_ADDBY_CATEGORY,
                    DB.TagOrientation.Horizontal, center
                )
                tagged_count += 1
            except Exception:
                error_count += 1
                error_categories.add("{} : {}".format(link_name, cat_name))
                continue


# ---- 7. Summary -----------------------------------------------------------------
msg = (
    "Elements tagged: {}\n"
    "Already tagged (skipped): {}\n"
    "Errors: {}"
).format(tagged_count, skipped_already_tagged, error_count)

if error_categories:
    msg += (
        "\n\nCategories with errors (likely no tag family loaded "
        "for that category):\n- " + "\n- ".join(sorted(error_categories))
    )

forms.alert(msg, title="Tagging completed")