# -*- coding: utf-8 -*-
"""Compares two versions of a linked model and reports the new, modified and
deleted elements in contact with the concrete in the whole model, inside the
section box of a 3D view, or inside a scope box. Generates an HTML report with a 3D viewer,
including grids and levels for reference.

Shift+click: change the folder where reports are saved."""
__title__ = "Compare\nZone"
__author__ = "Luis Guerrero"

import os
import datetime
import json
import re

from pyrevit import forms, script, revit, DB, EXEC_PARAMS

import concrete_contact_diff as ccd

def _config_file(name):
    """config/<name> of this button's extension, or next to the engine's lib folder
    (the button may live in a different extension than the library)."""
    bases = [os.path.join(os.path.dirname(__file__), '..', '..', '..'),
             os.path.join(os.path.dirname(os.path.abspath(ccd.__file__)), '..')]
    for b in bases:
        p = os.path.abspath(os.path.join(b, 'config', name))
        if os.path.exists(p):
            return p
    forms.alert('Configuration file not found: config\\%s' % name, exitscript=True)


RULES = _config_file('concrete_contact_rules.json')
TEMPLATE = _config_file('report_template.html')

doc = revit.doc
app = doc.Application
cfg = script.get_config()
output = script.get_output()

# 1. Zone: whole model, the section box of a 3D view, or a scope box
WHOLE, SECTION_BOX, SCOPE_BOX = 'Whole model', 'Section box', 'Scope box'
zone_mode = forms.CommandSwitchWindow.show([WHOLE, SECTION_BOX, SCOPE_BOX], message='1/5 · What to review?')
if not zone_mode:
    script.exit()
view = doc.ActiveView
if zone_mode == WHOLE:
    if not forms.alert('The whole concrete model will be reviewed. On a large model this can take '
                       'several minutes (Cancel is available in the progress bar). Continue?',
                       yes=True, no=True):
        script.exit()
    with open(RULES) as f:
        skip_cats = json.load(f).get('concrete', {}).get('exclude_categories', [])
    zone = ccd.zone_from_model(doc, skip_cats)
    if zone is None:
        forms.alert('The concrete model has no model elements.', exitscript=True)
elif zone_mode == SECTION_BOX:
    # 3D views with an active section box; the active view first
    views3d = [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D)
               if not v.IsTemplate and v.IsSectionBoxActive]
    if not views3d:
        forms.alert('No 3D view has an active section box.', exitscript=True)
    views3d.sort(key=lambda v: (v.Id != view.Id, v.Name))
    by_name = dict((v.Name, v) for v in views3d)
    pick = views3d[0].Name if len(views3d) == 1 else forms.SelectFromList.show(
        [v.Name for v in views3d], title='1/5 · 3D view with the section box', button_name='Next', width=520)
    if not pick:
        script.exit()
    zone = ccd.zone_from_section_box(by_name[pick])
else:
    scope_boxes = sorted(ccd.ename(s) for s in DB.FilteredElementCollector(doc)
                         .OfCategory(DB.BuiltInCategory.OST_VolumeOfInterest)
                         .WhereElementIsNotElementType())
    if not scope_boxes:
        forms.alert('The concrete model has no scope boxes.', exitscript=True)
    pick = forms.SelectFromList.show(scope_boxes, title='1/5 · Scope box', button_name='Next', width=520)
    if not pick:
        script.exit()
    zone = ccd.zone_from_scope_box(doc, pick)
sb_name = zone['label']

# 2. Linked model to compare (gives the coordinate transform)
links = {}
for l in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
    if l.GetLinkDocument() is not None:
        links[l.Name] = l
if not links:
    forms.alert('There are no loaded linked models in the active model.', exitscript=True)
link_name = forms.SelectFromList.show(sorted(links), title='2/5 · Linked model to compare',
                                      button_name='Next', width=620)
if not link_name:
    script.exit()
link = links[link_name]
link_title = ccd.link_label(link)

# 3-4. Old and new versions, opened (detached) in this Revit session
open_docs = {}
for d in app.Documents:
    if not d.IsLinked and not d.Equals(doc) and not d.IsFamilyDocument:
        open_docs[d.Title] = d
if len(open_docs) < 2:
    forms.alert('Open both versions of "%s" (detached) in this Revit session '
                'and run the tool again.' % link_title, exitscript=True)
old_title = forms.SelectFromList.show(sorted(open_docs), title='3/5 · PREVIOUS version',
                                      button_name='Next', width=620)
if not old_title:
    script.exit()
new_title = forms.SelectFromList.show(sorted(t for t in open_docs if t != old_title),
                                      title='4/5 · NEW version', button_name='Next', width=620)
if not new_title:
    script.exit()

base = re.sub(r'\.rvt$', '', link_title, flags=re.I)
if base not in old_title or base not in new_title:
    if not forms.alert('The selected versions do not look like versions of the linked model "%s".\n\n'
                       'Previous: %s\nNew: %s\n\nContinue anyway?' % (base, old_title, new_title),
                       yes=True, no=True):
        script.exit()

# 5. Contact tolerance (defaults to the last value used, else the rules file)
with open(RULES) as f:
    default_tol = json.load(f).get('tolerance_mm', 25)
last_tol = cfg.get_option('tolerance_mm', default_tol)
tolerance = None
while tolerance is None:
    txt = forms.ask_for_string(default=str(last_tol), title='5/5 · Contact tolerance',
                               prompt='Maximum distance (mm) for an element to be considered\n'
                                      'in contact with the concrete.\n'
                                      '0 = only elements that penetrate the concrete.')
    if txt is None:
        script.exit()
    try:
        tolerance = float(txt.replace(',', '.'))
        if tolerance < 0:
            raise ValueError()
    except ValueError:
        forms.alert('Enter a number greater than or equal to 0 (mm).')
        tolerance = None
cfg.tolerance_mm = tolerance

# Output folder (asked once; Shift+click to change it)
out_root = cfg.get_option('out_root', '')
if EXEC_PARAMS.config_mode or not out_root or not os.path.isdir(out_root):
    out_root = forms.pick_folder(title='Folder to save the reports')
    if not out_root:
        script.exit()
    cfg.out_root = out_root
script.save_config()

stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M')
out_dir = os.path.join(out_root, '%s_%s' % (re.sub(r'[^A-Za-z0-9_-]+', '_', sb_name), stamp))

t0 = datetime.datetime.now()
try:
    with forms.ProgressBar(title='Compare Zone', cancellable=True) as pb:
        def progress(text, i, n):
            pb.title = text
            pb.update_progress(i if n else 0, n or 1)
            return not pb.cancelled
        s = ccd.run(doc, zone, open_docs[old_title], open_docs[new_title], link, RULES, out_dir, TEMPLATE,
                    tolerance_mm=tolerance, progress=progress)
except ccd.Cancelled:
    forms.alert('Comparison cancelled.', exitscript=True)
except Exception as ex:
    forms.alert('Error during the comparison:\n\n%s' % ex, exitscript=True)
secs = (datetime.datetime.now() - t0).total_seconds()

output.print_md('## Comparison · %s' % sb_name)
output.print_md('**%s** → **%s** · tolerance %s mm · %.0f s' % (s['old'], s['new'], s['tolerance_mm'], secs))
output.print_md('| New | Modified | Deleted | Concrete in zone | Near concrete (prev./new) | Changed near concrete |\n'
                '|---|---|---|---|---|---|\n| %d | %d | %d | %d | %d / %d | %d |'
                % (s['created'], s['modified'], s['deleted'], s['concrete_elements'],
                   s['near_old'], s['near_new'], s['changed_checked']))
output.print_md('Report: `%s`' % s['report'])

if s['created'] + s['modified'] + s['deleted'] == 0:
    forms.alert('No changes in contact with the concrete in "%s".' % sb_name)
os.startfile(s['report'])
