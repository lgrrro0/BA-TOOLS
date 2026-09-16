# -*- coding: utf-8 -*-
"""Adds a prefix, a suffix, or applies a preset to the names of multiple
views selected in the Project Browser.

Workflow:
1. In the Project Browser, select the views you want to rename (Ctrl/Shift
   click for multiple selection), THEN run this script. Views cannot be
   picked graphically, so the selection must already be made before
   running.
2. Choose "Add Prefix", "Add Suffix", or "Presets".
   - Add Prefix / Add Suffix: type the text to add.
   - Presets: choose from a growing list of ready-made renaming rules.
     Currently available:
       * Cardinal Orientation (True North): for section and elevation
         views, appends " - Elevation Looking {Direction}" or
         " - Section Looking {Direction}" based on the view's direction
         and the project's True North angle, using the 8 principal
         compass points spelled out in full (North, Northeast, East,
         Southeast, South, Southwest, West, Northwest).

Notes:
- View templates in the selection are always ignored.
- If a resulting name is invalid (duplicate, or contains a character Revit
  doesn't allow in names) that view is skipped and reported at the end;
  the rest of the views are still renamed.
"""
__title__ = "Prefix/Suffix\nViews"
__author__ = "Luis"

import math
from pyrevit import revit, DB, forms

doc = revit.doc
uidoc = revit.uidoc

COMPASS_DIRECTIONS = ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"]

# Flip this to -1 if the compass labels come out mirrored/rotated after
# testing against a view whose real-world orientation you already know.
TRUE_NORTH_ANGLE_SIGN = -1


def get_true_north_angle(doc):
    """Reads the project's True North rotation from the Survey Point's
    "Angle to True North" parameter (the same value edited by the
    Manage > Coordinates > Rotate True North tool). Falls back to 0 (no
    rotation) if the Survey Point or the parameter can't be found."""
    survey_point = DB.FilteredElementCollector(doc) \
        .OfCategory(DB.BuiltInCategory.OST_SharedBasePoint) \
        .WhereElementIsNotElementType() \
        .FirstElement()

    if not survey_point:
        return 0.0

    angle_param = survey_point.get_Parameter(DB.BuiltInParameter.BASEPOINT_ANGLETON_PARAM)
    if not angle_param:
        return 0.0

    return TRUE_NORTH_ANGLE_SIGN * angle_param.AsDouble()

# ---------------------------------------------------------------------------
# 1. Read the current selection made in the Project Browser
# ---------------------------------------------------------------------------
selected_ids = uidoc.Selection.GetElementIds()

if not selected_ids:
    forms.alert(
        "No views are selected.\n\n"
        "Select one or more views in the Project Browser (Ctrl/Shift-click "
        "for multiple) before running this script.",
        exitscript=True
    )

all_selected = [doc.GetElement(eid) for eid in selected_ids]
views = [e for e in all_selected if isinstance(e, DB.View) and not e.IsTemplate]
skipped_invalid = len(all_selected) - len(views)

if not views:
    forms.alert(
        "None of the selected items are views.\n\n"
        "Select views in the Project Browser before running this script.",
        exitscript=True
    )

if skipped_invalid:
    forms.alert(
        "{} selected item(s) were not views (or were view templates) "
        "and were ignored.".format(skipped_invalid)
    )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def get_cardinal_direction(view, true_north_angle):
    """Returns the nearest of the 8 principal compass points a section or
    elevation view is looking towards, relative to the project's True
    North."""
    direction = view.ViewDirection

    # Empirically, ViewDirection points opposite to where the camera is
    # actually looking, so it must be inverted for "Looking {Direction}"
    # to match the real-world direction.
    vx = -direction.X
    vy = -direction.Y

    # Rotate from Project North into True North.
    theta = true_north_angle
    east = vx * math.cos(theta) - vy * math.sin(theta)
    north = vx * math.sin(theta) + vy * math.cos(theta)

    bearing = math.degrees(math.atan2(east, north)) % 360
    index = int(round(bearing / 45.0)) % 8
    return COMPASS_DIRECTIONS[index]


def apply_cardinal_orientation_preset(target_views):
    true_north_angle = get_true_north_angle(doc)

    applicable = [v for v in target_views if v.ViewType in (DB.ViewType.Elevation, DB.ViewType.Section)]
    skipped_type = len(target_views) - len(applicable)

    renamed_count = 0
    error_count = 0
    errors = []

    with revit.Transaction("Add cardinal orientation to view names"):
        for view in applicable:
            try:
                direction_label = get_cardinal_direction(view, true_north_angle)
                view_type_label = "Elevation" if view.ViewType == DB.ViewType.Elevation else "Section"
                current_name = view.Name
                new_name = "{} - {} Looking {}".format(current_name, view_type_label, direction_label)
                if new_name != current_name:
                    view.Name = new_name
                    renamed_count += 1
            except Exception as ex:
                error_count += 1
                errors.append("{}: {}".format(view.Name, str(ex)))

    message = (
        "Cardinal Orientation preset completed.\n\n"
        "Views renamed: {}\n"
        "Skipped (not a section/elevation): {}\n"
        "Errors: {}".format(renamed_count, skipped_type, error_count)
    )
    if errors:
        message += "\n\nFirst error(s):\n" + "\n".join(errors[:5])

    forms.alert(message)


# Presets registry: add new entries here as more presets are built.
PRESETS = {
    "Cardinal Orientation (True North) - Sections & Elevations": apply_cardinal_orientation_preset,
}


# ---------------------------------------------------------------------------
# 2. Ask what to do
# ---------------------------------------------------------------------------
mode_options = ["Add Prefix", "Add Suffix", "Presets"]
mode = forms.CommandSwitchWindow.show(
    mode_options,
    message="What do you want to do with the {} selected view(s)?".format(len(views))
)
if not mode:
    forms.alert("Operation cancelled.", exitscript=True)

if mode == "Presets":
    preset_choice = forms.CommandSwitchWindow.show(
        list(PRESETS.keys()),
        message="Choose a preset to apply:"
    )
    if not preset_choice:
        forms.alert("Operation cancelled.", exitscript=True)

    PRESETS[preset_choice](views)

else:
    # -----------------------------------------------------------------
    # Add Prefix / Add Suffix
    # -----------------------------------------------------------------
    prefix = ""
    suffix = ""

    if mode == "Add Prefix":
        prefix = forms.ask_for_string(
            default="",
            prompt="Enter the prefix to add (placed before the current name):",
            title="Prefix"
        )
        if prefix is None:
            forms.alert("Operation cancelled.", exitscript=True)

    elif mode == "Add Suffix":
        suffix = forms.ask_for_string(
            default="",
            prompt="Enter the suffix to add (placed after the current name):",
            title="Suffix"
        )
        if suffix is None:
            forms.alert("Operation cancelled.", exitscript=True)

    if not prefix and not suffix:
        forms.alert("No prefix or suffix was entered. Nothing to do.", exitscript=True)

    renamed_count = 0
    error_count = 0
    errors = []

    with revit.Transaction("Add prefix/suffix to view names"):
        for view in views:
            try:
                current_name = view.Name
                new_name = "{}{}{}".format(prefix, current_name, suffix)
                if new_name != current_name:
                    view.Name = new_name
                    renamed_count += 1
            except Exception as ex:
                error_count += 1
                errors.append("{}: {}".format(view.Name, str(ex)))

    message = (
        "Renaming completed.\n\n"
        "Views renamed: {}\n"
        "Errors: {}".format(renamed_count, error_count)
    )
    if errors:
        message += "\n\nFirst error(s):\n" + "\n".join(errors[:5])

    forms.alert(message)