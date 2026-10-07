# -*- coding: utf-8 -*-
"""Hides, in the active view, the linked model elements that are NOT in
contact (within the tolerance) with the visible elements of the current
model. Uses the native Hide in View › Elements command.

To revert: Reveal Hidden Elements › select › Unhide Element."""
__title__ = "Clean\nView"
__author__ = "Luis Guerrero"

from System.Collections.Generic import List
from Autodesk.Revit.UI import RevitCommandId, PostableCommand

from pyrevit import forms, script, revit, DB

import concrete_contact_diff as ccd

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
cfg = script.get_config()
output = script.get_output()

if view.IsTemplate or view.ViewType in (DB.ViewType.DrawingSheet, DB.ViewType.Schedule,
                                         DB.ViewType.DraftingView, DB.ViewType.Legend):
    forms.alert('Activate a model view (plan, section, elevation or 3D).', exitscript=True)

# 1. Linked models visible in the view (all checked by default)
links = {}
for l in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance):
    if l.GetLinkDocument() is not None:
        links[l.Name] = l
if not links:
    forms.alert('There are no loaded linked models visible in this view.', exitscript=True)
items = [forms.TemplateListItem(n, checked=True) for n in sorted(links)]
chosen = forms.SelectFromList.show(items, title='1/2 · Linked models to filter',
                                   multiselect=True, button_name='Next', width=620)
if not chosen:
    script.exit()

# 2. Tolerance (remembers the last value)
last_tol = cfg.get_option('hide_tolerance_mm', 25)
tolerance = None
while tolerance is None:
    txt = forms.ask_for_string(default=str(last_tol), title='2/2 · Contact tolerance',
                               prompt='Maximum distance (mm) to the visible elements of the current model.\n'
                                      'Linked elements farther away will be hidden in this view.')
    if txt is None:
        script.exit()
    try:
        tolerance = float(txt.replace(',', '.'))
        if tolerance < 0:
            raise ValueError()
    except ValueError:
        forms.alert('Enter a number greater than or equal to 0 (mm).')
        tolerance = None
cfg.hide_tolerance_mm = tolerance
script.save_config()

if isinstance(view, DB.View3D) and not view.IsSectionBoxActive:
    if not forms.alert('The 3D view has no section box: the full linked models will be checked '
                       'and it may take a while. Continue?', yes=True, no=True):
        script.exit()

# Compute contacts and collect references to hide
with forms.ProgressBar(title='Computing contacts...', indeterminate=True):
    result, n_host = ccd.view_contacts(doc, view, [links[n] for n in chosen], tolerance)

refs = List[DB.Reference]()
rows = []
for link, hits, cand in result:
    hide = [e for e in cand if ccd.eid(e.Id) not in hits]
    for e in hide:
        try:
            refs.Add(DB.Reference(e).CreateLinkReference(link))
        except Exception:
            pass
    rows.append((ccd.link_label(link), len(cand), len(hits), len(hide)))

output.print_md('## Clean View · %s' % view.Name)
output.print_md('Visible elements of the current model: **%d** · tolerance **%s mm**' % (n_host, tolerance))
output.print_table(rows, columns=['Linked model', 'In view', 'In contact', 'To hide'])

if n_host == 0:
    forms.alert('There are no visible elements of the current model in this view.', exitscript=True)
if refs.Count == 0:
    forms.alert('All linked elements in the view are in contact. Nothing to hide.', exitscript=True)

uidoc.Selection.SetReferences(refs)
uidoc.Application.PostCommand(RevitCommandId.LookupPostableCommandId(PostableCommand.HideElements))
output.print_md('**%d** linked elements were hidden in the view.' % refs.Count)
