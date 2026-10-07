# -*- coding: utf-8 -*-
"""Checks every Revit link and CAD link/import (DWG, DXF...) of the model:
load status, ACC version and whether the loaded copy is out of date,
imported instead of linked, not pinned, not placed or placed more than once,
not placed by shared coordinates (Revit links), current-view-only CAD,
nested/attachment links, and worksets (default workset, or shared with
model elements)."""
__title__ = "Link Health\nCheck"
__author__ = "Luis Guerrero"

from pyrevit import revit, forms, script

import link_health as lh

doc = revit.doc
output = script.get_output()

with forms.ProgressBar(title='Checking links...', indeterminate=True):
    results = lh.check(doc)
if not results:
    forms.alert('The model has no Revit or CAD links.', exitscript=True)

ICON = {lh.ERROR: u'❌', lh.WARNING: u'⚠️', 'ok': u'✅'}
n_err = sum(1 for r in results if lh.worst(r['findings']) == lh.ERROR)
n_warn = sum(1 for r in results if lh.worst(r['findings']) == lh.WARNING)

output.print_md(u'## Link health check · %s' % doc.Title)
output.print_md(u'%d links · %s %d with errors · %s %d with warnings · %s %d OK'
                % (len(results), ICON[lh.ERROR], n_err, ICON[lh.WARNING], n_warn, ICON['ok'],
                   len(results) - n_err - n_warn))
for sev, text in lh.host_notes(doc):
    output.print_md(u'%s %s' % (ICON[sev], text))
rows = []
for r in results:
    issues = [t for s, t in r['findings'] if s in (lh.ERROR, lh.WARNING)]
    info = [t for s, t in r['findings'] if s == lh.INFO]
    rows.append((ICON[lh.worst(r['findings'])], r['kind'], r['name'], r['status'],
                 u'<br>'.join(issues) or u'-', u'<br>'.join(info), output.linkify(r['ids'])))
output.print_table(rows, columns=['', 'Type', 'Link', 'Status', 'Issues', 'Info', 'Select'])
