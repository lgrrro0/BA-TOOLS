# -*- coding: utf-8 -*-
"""Lists the model's cloud links with their ACC version and date, compares
against the last time the tool was run on this model, and creates or
updates a legend with LINK NAME | VERSION | LAST MODIFIED."""
__title__ = "Link\nVersions"
__author__ = "Luis Guerrero"

import datetime

from pyrevit import forms, script, revit, DB

import link_versions as lv

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
doc_key = doc.PathName or doc.Title

with forms.ProgressBar(title='Reading link versions...', indeterminate=True):
    items = lv.collect(doc)
if not items:
    forms.alert('The model has no cloud links.', exitscript=True)

# 1. What changed since the last run
prev = lv.last_run(doc_key)
changes = lv.compare(prev, items)
stale = [i for i in items if i['note']]

if prev is None:
    msg = u'First run on this model.\nThe current state is saved to compare against next time.'
else:
    last = datetime.datetime.strptime(prev['run'], '%Y-%m-%d %H:%M:%S').strftime('%m/%d/%Y %H:%M')
    if changes:
        msg = u'Since the last run (%s), %d link(s) changed:\n\n' % (last, len(changes))
        msg += u'\n'.join(u'• %s — %s%s' % (n, what, (u': %s  →  %s' % (a, b)) if a or b else u'')
                          for n, what, a, b in changes)
    else:
        msg = u'No changes since the last run (%s).' % last
if stale:
    msg += u'\n\nAttention:\n' + u'\n'.join(u'• %s: %s' % (i['name'], i['note']) for i in stale)
forms.alert(msg, title='Link Versions')

output.print_md('## Link Versions · %s' % doc.Title)
output.print_table([(i['name'], lv.fmt_version(i['version']), lv.fmt_date(i['date']), i['note']) for i in items],
                   columns=['Link', 'Version', 'Date', 'Note'])

prev_sel = set(prev.get('selected', [])) if prev else None
lv.save_run(doc_key, items, prev_sel or [])

# 2. Links for the legend
labels = {}
opts = []
for i in items:
    label = u'%s   ·   %s   ·   %s' % (i['name'], lv.fmt_version(i['version']), lv.fmt_date(i['date']))
    labels[label] = i
    opts.append(forms.TemplateListItem(label, checked=(not prev_sel or i['name'] in prev_sel)))
chosen = forms.SelectFromList.show(opts, title='Links for the legend', multiselect=True,
                                   button_name='Next', width=760)
if not chosen:
    script.exit()
selected = [labels[c] for c in chosen]

# 3. Target legend (existing or new)
legends = lv.legends(doc)
if not legends:
    forms.alert('The model has no legends. Create an empty legend '
                '(View › Legends › Legend) and run the tool again.', exitscript=True)
NEW = u'<New legend>'
target_name = forms.SelectFromList.show([NEW] + [v.Name for v in legends], title='Legend to create or update',
                                        button_name='OK', width=500)
if not target_name:
    script.exit()
if target_name == NEW:
    new_name = forms.ask_for_string(default='LINK VERSIONS', title='New legend', prompt='Legend name:')
    if not new_name:
        script.exit()
    if any(v.Name == new_name for v in legends):
        forms.alert(u'A legend named "%s" already exists.' % new_name, exitscript=True)
    target = None
else:
    target = [v for v in legends if v.Name == target_name][0]
    n = lv.sheets_showing(doc, target)
    if not forms.alert(u'The text and lines of legend "%s"%s will be replaced.\nContinue?'
                       % (target_name, (u', which appears on %d sheet(s),' % n) if n else u''),
                       yes=True, no=True):
        script.exit()

with revit.Transaction('Link versions legend'):
    if target is None:
        target = doc.GetElement(legends[0].Duplicate(DB.ViewDuplicateOption.Duplicate))
        target.Name = new_name
    lv.write_legend(doc, target, selected)

lv.save_run(doc_key, items, [i['name'] for i in selected])
uidoc.ActiveView = target
output.print_md(u'Legend **%s** updated with %d link(s).' % (target.Name, len(selected)))
