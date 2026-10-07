# -*- coding: utf-8 -*-
"""Attaches the bases and/or tops of walls and columns VISIBLE IN THE
ACTIVE VIEW to levels.

For each end, its actual elevation is referenced to a level:
- within the snap tolerance of a level -> that level, offset 0 (it snaps);
- farther -> bases go to the level at or below, tops to the level at or
  above, with the offset that keeps the element exactly where it is.
Walls with an unconnected height get their top constrained to a level.
Tops attached to floors/roofs and slanted columns are not changed."""
__title__ = "Attach to\nLevels"
__author__ = "Luis Guerrero"

from pyrevit import revit, DB, forms, script

import level_attach as la

doc = revit.doc
view = doc.ActiveView
cfg = script.get_config()
output = script.get_output()

if view.IsTemplate or view.ViewType in (DB.ViewType.DrawingSheet, DB.ViewType.Schedule,
                                         DB.ViewType.DraftingView, DB.ViewType.Legend):
    forms.alert('Activate a model view (plan, section, elevation or 3D).', exitscript=True)

# 1. Categories visible in the view
found = {}
for label, bic in la.CATEGORIES:
    els = list(DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType())
    if els:
        found[u'%s (%d)' % (label, len(els))] = els
if not found:
    forms.alert('There are no walls or columns visible in the active view.', exitscript=True)
chosen = forms.SelectFromList.show([forms.TemplateListItem(k, checked=True) for k in sorted(found)],
                                   title='1/3 · Categories to attach', multiselect=True,
                                   button_name='Next', width=450)
if not chosen:
    script.exit()
elements = [e for k in chosen for e in found[k]]

# 2. Which ends
BOTH, BASES, TOPS = 'Bases and tops', 'Bases only', 'Tops only'
ends = forms.CommandSwitchWindow.show([BOTH, BASES, TOPS], message='2/3 · What to attach?')
if not ends:
    script.exit()

# 3. Snap tolerance (remembers the last value)
last = cfg.get_option('snap_tolerance_mm', 25)
tol = None
while tol is None:
    txt = forms.ask_for_string(default=str(last), title='3/3 · Snap tolerance',
                               prompt='Ends closer than this distance (mm) to the nearest level snap onto it\n'
                                      '(offset 0). Farther ends keep their position with an offset.')
    if txt is None:
        script.exit()
    try:
        tol = float(txt.replace(',', '.'))
        if tol < 0:
            raise ValueError()
    except ValueError:
        forms.alert('Enter a number greater than or equal to 0 (mm).')
        tol = None
cfg.snap_tolerance_mm = tol
script.save_config()

plan = la.plan(doc, elements, ends in (BOTH, BASES), ends in (BOTH, TOPS), tol / 304.8)
todo = [p for p in plan if p[1]]
skipped = [p for p in plan if not p[1] and p[3]]
if not todo:
    forms.alert('Nothing to change: the elements are already attached to their nearest levels.',
                exitscript=True)
if not forms.alert('%d element(s) will be changed (%d already OK, %d skipped).\nContinue?'
                   % (len(todo), len(plan) - len(todo) - len(skipped), len(skipped)), yes=True, no=True):
    script.exit()

done, failed = [], []
with revit.Transaction('Attach to levels'):
    for e, changes, desc, _ in todo:
        st = DB.SubTransaction(doc)
        st.Start()
        try:
            la.apply(doc, e, changes)
            st.Commit()
            done.append((output.linkify(e.Id), e.Category.Name, u' · '.join(desc)))
        except Exception as ex:
            st.RollBack()
            failed.append((output.linkify(e.Id), e.Category.Name, str(ex)))

output.print_md('## Attach to levels · %s' % view.Name)
output.print_md('Changed **%d** · failed **%d** · skipped **%d** · snap tolerance %s mm'
                % (len(done), len(failed), len(skipped), tol))
if done:
    output.print_table(done, columns=['Element', 'Category', 'Change'])
if failed:
    output.print_md('### Failed')
    output.print_table(failed, columns=['Element', 'Category', 'Error'])
if skipped:
    output.print_md('### Skipped')
    output.print_table([(output.linkify(e.Id), e.Category.Name, why) for e, _, _, why in skipped],
                       columns=['Element', 'Category', 'Reason'])
