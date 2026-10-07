# -*- coding: utf-8 -*-
"""Dimensions the grids VISIBLE IN THE ACTIVE VIEW (plan, section or
elevation).

Parallel grids are grouped (any angle). At both ends of the grids (top,
bottom, left and right) an overall dimension is placed 2 ft inward from the
grid heads and a chain dimension 4 ft inward. Only grids that reach each
dimension line are dimensioned, so grids trimmed short in the view are left
out. Dimensions that already exist in the view are skipped. Runs without dialogs; the report only opens if Revit fails to
create a dimension."""
__title__ = "Grid\nDimensions"
__author__ = "Luis Guerrero"

from pyrevit import revit, DB, forms, script

import grid_dims as gd

doc = revit.doc
view = doc.ActiveView

if view.ViewType not in (DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.EngineeringPlan,
                         DB.ViewType.AreaPlan, DB.ViewType.Section, DB.ViewType.Elevation, DB.ViewType.Detail):
    forms.alert('Activate a plan, section or elevation view.', exitscript=True)
if view.IsTemplate:
    forms.alert('The active view is a view template.', exitscript=True)

grps, _ = gd.groups(doc, view)
if not any(len(rows) > 1 for _, rows in grps):
    forms.alert('There are no parallel grids visible in the active view.', exitscript=True)

with revit.Transaction('Grid dimensions'):
    created, skipped = gd.create(doc, view, gd.BOTH)

errors = [(desc, why) for desc, grids, why in skipped if why.startswith('Revit error')]
if errors:
    output = script.get_output()
    output.print_md('## Grid dimensions · %s' % view.Name)
    output.print_md('Created **%d** · failed **%d**' % (len(created), len(errors)))
    output.print_table(errors, columns=['Grids', 'Reason'])
