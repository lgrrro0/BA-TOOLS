# -*- coding: utf-8 -*-
"""Exports the existing structural types (columns, walls, grade beams,
piers, slabs) to one CSV per profile, with lengths in FEET-INCHES
(e.g. 1'-6"). Fill in the files (or upload them to Google Sheets) and use
"Structural Types from CSV" to create the new types."""
__title__ = "Export Types\n(Feet-Inches)"
__author__ = "Luis Guerrero"

import os

from pyrevit import revit, forms, script

import structural_types as st

FMT = 'ft-in'

doc = revit.doc
output = script.get_output()

items = [forms.TemplateListItem(p, checked=True) for p in sorted(st.PROFILES)]
chosen = forms.SelectFromList.show(items, title='Profiles to export', multiselect=True,
                                   button_name='Next', width=450)
if not chosen:
    script.exit()
folder = forms.pick_folder(title='Folder to save the CSV files')
if not folder:
    script.exit()

rows = []
for name in chosen:
    path, n, note = st.export_profile(doc, name, folder, FMT)
    rows.append((name, n, os.path.basename(path), note))

output.print_md('## Export types · %s' % ('decimal inches' if FMT == 'in' else 'feet-inches'))
output.print_table(rows, columns=['Profile', 'Types', 'File', 'Note'])
output.print_md('Folder: `%s`' % folder)
os.startfile(folder)
