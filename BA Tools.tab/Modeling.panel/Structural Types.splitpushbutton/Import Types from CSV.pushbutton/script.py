# -*- coding: utf-8 -*-
"""Creates structural element types (columns, walls, grade beams, piers,
slabs...) from a FOLDER with one or more CSV files, one per element type.
Each file must match one of the profiles in lib/structural_types.py. Either
of these two file names is accepted:

    "<SPREADSHEET_NAME> - <ProfileName>.csv"   (default name exported by
                                                 Google Sheets)
    "<ProfileName>.csv"                        (plain name)

Example: for the "Round Columns" profile, either of these works:
    "Structural Family Types List - Round Columns.csv"
    "Round Columns.csv"

Length columns can be in inches (plain header "Width" or "Width (in)",
e.g. 18.5), decimal feet ("Width (ft)") or feet-inches ("Width (ft-in)",
e.g. 1'-6"). The Export Types buttons in this menu write inches and
feet-inches files.

In Google Sheets: one tab per element type; download each tab separately
as CSV (File -> Download -> Comma-separated values) and save them all
together in the same folder.
"""
__title__ = "Structural\nTypes from CSV"
__author__ = "Luis Guerrero"

from pyrevit import revit, DB, forms, script

import structural_types as st

output = script.get_output()
doc = revit.doc

# 1. Pick the folder with the CSV files ---------------------------------------
folder = forms.pick_folder(title="Select the folder with the element CSV files")
if not folder:
    script.exit()

matched_files = {}
for profile_name in st.PROFILES:
    found = st.find_csv_for_profile(folder, profile_name)
    if found:
        matched_files[profile_name] = found

if not matched_files:
    forms.alert(
        "No recognizable CSV file was found in that folder.\n\n"
        "Expected file names (either one per profile):\n{}".format(
            "\n".join(
                "- {0} - {1}.csv  or  {1}.csv".format(st.SPREADSHEET_NAME, p)
                for p in st.PROFILES
            )
        ),
        exitscript=True,
    )

summary = {}

# 2. Process each CSV that matches a profile ----------------------------------
with revit.Transaction("Create types from CSV"):
    for profile_name, csv_path in matched_files.items():
        profile = st.PROFILES[profile_name]
        kind = profile["kind"]
        rows, units = st.read_csv_rows(csv_path)
        created, skipped, errors = [], [], []

        if kind == "family_symbol":
            base = st.find_base_symbol(doc, profile)
            base_ref = profile.get("base_type_name") or profile.get("base_family_name")
            existing_names = st.get_existing_type_names(doc, base.Family) if base else None
        else:
            base = st.find_base_system_type(doc, profile)
            base_ref = profile["base_type_name"]
            existing_names = st.get_existing_system_type_names(doc, kind) if base else None
        if not base:
            msg = "Base type/family '{}' not found in the project.".format(base_ref)
            similar = st.find_similar_names(doc, base_ref, kind)
            if similar:
                msg += " Similar names found: {}".format(", ".join(similar))
            summary[profile_name] = (created, skipped, [msg])
            continue

        for i, raw in enumerate(rows, start=2):  # row 1 = header
            if not any((v or '').strip() for v in raw.values()):
                continue  # blank row
            if (raw.get('Current Type Name') or '').strip() in existing_names:
                skipped.append(raw['Current Type Name'].strip())
                continue  # row written by Export Types for a type that already exists
            row, err = st.normalize_row(raw, profile, units)
            if err:
                errors.append("Row {0}: {1}".format(i, err))
                continue
            try:
                new_name = profile["name_fn"](row)
            except Exception as e:
                errors.append("Row {0}: could not build the name ({1})".format(i, e))
                continue

            if new_name in existing_names:
                skipped.append(new_name)
                continue

            new_type = base.Duplicate(new_name)
            existing_names.add(new_name)

            if kind == "family_symbol":
                row_ok = True
                for csv_col, revit_param in profile["param_map"].items():
                    param = new_type.LookupParameter(revit_param)
                    if param is None:
                        errors.append("{0}: parameter '{1}' not found.".format(new_name, revit_param))
                        row_ok = False
                        continue
                    param.Set(DB.UnitUtils.ConvertToInternalUnits(float(row[csv_col]), profile["units"]))
                if row_ok:
                    created.append(new_name)
            else:
                value = DB.UnitUtils.ConvertToInternalUnits(float(row[profile["thickness_column"]]),
                                                            profile["units"])
                ok, err = st.set_structural_layer_width(new_type, value)
                if ok:
                    created.append(new_name)
                else:
                    errors.append("{0}: {1}".format(new_name, err))

        summary[profile_name] = (created, skipped, errors)

# 3. Report --------------------------------------------------------------------
for profile_name, (created, skipped, errors) in summary.items():
    output.print_md("## {}".format(profile_name))
    output.print_md("**Created:** {}".format(len(created)))
    for n in created:
        output.print_md("- {}".format(n))
    if skipped:
        output.print_md("**Skipped (already existed):** {}".format(len(skipped)))
        for n in skipped:
            output.print_md("- {}".format(n))
    if errors:
        output.print_md("**Errors:**")
        for e in errors:
            output.print_md("- {}".format(e))

ignored_profiles = [p for p in st.PROFILES if p not in matched_files]
if ignored_profiles:
    output.print_md("## Profiles with no CSV in the folder (not processed)")
    for p in ignored_profiles:
        output.print_md("- {}".format(p))
