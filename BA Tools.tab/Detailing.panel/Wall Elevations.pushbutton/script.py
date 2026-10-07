# -*- coding: utf-8 -*-
"""Creates one elevation view per SELECTED WALL (if nothing is selected,
asks you to pick the walls).

Each elevation looks straight at the exterior face of its wall (any angle;
arc walls are viewed along their chord), is cropped to the wall plus 1 ft
and its far clip stops 1 ft behind the wall. The view is named
"Wall Elevation <Mark or Id>". Non-bearing walls get a Coordination view
when the default elevation is Structural, so the wall is not hidden.

SHIFT+CLICK: look at the interior face instead."""
__title__ = "Wall\nElevations"
__author__ = "Luis Guerrero"

from pyrevit import revit, DB, forms, script

import wall_elevations as we

doc = revit.doc
uidoc = revit.uidoc
interior = __shiftclick__  # noqa: F821 (pyRevit builtin)

walls = [e for e in revit.get_selection().elements if isinstance(e, DB.Wall)]
if not walls:
    with forms.WarningBar(title='Pick the walls to elevate and press Finish'):
        picked = revit.pick_elements_by_category(DB.BuiltInCategory.OST_Walls)
    walls = [e for e in picked or [] if isinstance(e, DB.Wall)]
if not walls:
    script.exit()

type_id = we.elevation_type_id(doc)
if type_id is None:
    forms.alert('The project has no elevation view type.', exitscript=True)

if we.host_plan(doc, walls[0], doc.ActiveView) is None:
    forms.alert('The project needs at least one plan view to host the elevation markers.', exitscript=True)

created, errors = [], []
with revit.Transaction('Wall elevations'):
    for wall in walls:
        plan = we.host_plan(doc, wall, doc.ActiveView)
        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            created.append(we.create(doc, wall, plan, type_id, interior))
            sub.Commit()
        except Exception as ex:
            sub.RollBack()
            errors.append((we.eid(wall.Id), str(ex)))

if len(created) == 1:
    uidoc.ActiveView = created[0]

if errors or len(created) > 1:
    output = script.get_output()
    output.print_md('## Wall elevations')
    output.print_md('Created **%d** · failed **%d**' % (len(created), len(errors)))
    if created:
        output.print_table([(output.linkify(v.Id), v.Name) for v in created], columns=['View', 'Name'])
    if errors:
        output.print_table(errors, columns=['Wall Id', 'Reason'])
