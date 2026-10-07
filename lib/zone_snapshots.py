# -*- coding: utf-8 -*-
"""Zone snapshots: compares a linked model against a saved state of itself
instead of opening old/new versions as detached documents.

Each run saves a snapshot of the linked model as it is loaded right now,
limited to the elements near the concrete of a zone (same prefilter as
concrete_contact_diff), plus the list of every element id in the link (to
tell "deleted" apart from "moved away"). The next run compares the loaded
link against that snapshot, so the previous version never has to be
downloaded.

Limitations of the saved (previous) side, because only data is kept, not
the model: its geometry in the report is drawn as the element's bounding
box, and its contact with the concrete is the stored result when the
element was tested before, otherwise a bounding-box test.
"""
import os
import re
import json
import codecs
import datetime

from System import Int64
from System.Collections.Generic import List
import Autodesk.Revit.DB as DB

import concrete_contact_diff as ccd

try:
    import zlib
except ImportError:
    zlib = None

STORE = os.path.join(os.environ.get('APPDATA', ''), 'pyRevit', 'BATools', 'zone_snapshots')
CURRENT = None   # marker for "the link as loaded now" in run()


# ---------------------------------------------------------------- storage
def _safe(s):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', s or '').strip('_') or 'unnamed'


def folder(link_name, zone_label):
    return os.path.join(STORE, _safe(link_name), _safe(zone_label))


def list_snapshots(link_name, zone_label):
    """Saved snapshots for this link and zone, newest first:
    [{'path', 'taken' (datetime), 'version' (int or None), 'label'}]."""
    f = folder(link_name, zone_label)
    out = []
    if not os.path.isdir(f):
        return out
    for fn in os.listdir(f):
        m = re.match(r'^(\d{8}_\d{6})__V(\d+|-)\.json(\.z)?$', fn)
        if not m:
            continue
        taken = datetime.datetime.strptime(m.group(1), '%Y%m%d_%H%M%S')
        ver = int(m.group(2)) if m.group(2) != '-' else None
        out.append({'path': os.path.join(f, fn), 'taken': taken, 'version': ver,
                    'label': u'%s  ·  saved %s' % (('V%d' % ver) if ver else u'Version ?',
                                                     taken.strftime('%m/%d/%Y %H:%M'))})
    out.sort(key=lambda s: s['taken'], reverse=True)
    return out


def save_snapshot(link_name, zone_label, snap):
    f = folder(link_name, zone_label)
    if not os.path.isdir(f):
        os.makedirs(f)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    ver = snap.get('version')
    base = os.path.join(f, '%s__V%s.json' % (stamp, ver if ver else '-'))
    txt = json.dumps(snap, separators=(',', ':'))
    if zlib is not None:
        try:
            with open(base + '.z', 'wb') as fh:
                fh.write(zlib.compress(txt, 6))
            return base + '.z'
        except Exception:
            pass
    with codecs.open(base, 'w', 'utf-8') as fh:
        fh.write(txt)
    return base


def load_snapshot(path):
    if path.endswith('.z'):
        with open(path, 'rb') as fh:
            snap = json.loads(zlib.decompress(fh.read()))
    else:
        with codecs.open(path, 'r', 'utf-8') as fh:
            snap = json.load(fh)
    snap['elements'] = dict((int(k), v) for k, v in snap['elements'].items())
    snap['contact'] = dict((int(k), v) for k, v in snap.get('contact', {}).items())
    snap['all_ids'] = set(snap['all_ids'])
    return snap


# ---------------------------------------------------------------- geometry
def _box_solid(bbox, pad, to_host):
    """Solid of a link-coordinates bounding box [x0,y0,z0,x1,y1,z1], grown by pad,
    in host coordinates."""
    eps = 0.003
    x0, y0, z0 = bbox[0] - pad - eps, bbox[1] - pad - eps, bbox[2] - pad - eps
    x1, y1, z1 = bbox[3] + pad + eps, bbox[4] + pad + eps, bbox[5] + pad + eps
    pts = [DB.XYZ(x0, y0, z0), DB.XYZ(x1, y0, z0), DB.XYZ(x1, y1, z0), DB.XYZ(x0, y1, z0)]
    loop = DB.CurveLoop()
    for i in range(4):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % 4]))
    s = DB.GeometryCreationUtilities.CreateExtrusionGeometry(List[DB.CurveLoop]([loop]), DB.XYZ.BasisZ, z1 - z0)
    return DB.SolidUtils.CreateTransformed(s, to_host)


def box_contacts(host, concrete, to_host, bboxes, tol):
    """Contact test from bounding boxes only: {id: {'concrete': [...], 'pen': bool}}."""
    conc_ids = List[DB.ElementId]([e.Id for e, _, _, _ in concrete])
    out = {}
    if conc_ids.Count == 0:
        return out
    for k, bb in bboxes.items():
        if not bb:
            continue
        try:
            grown = [ccd.eid(i) for i in DB.FilteredElementCollector(host, conc_ids)
                     .WherePasses(DB.ElementIntersectsSolidFilter(_box_solid(bb, tol, to_host))).ToElementIds()]
            if not grown:
                continue
            pen = DB.FilteredElementCollector(host, conc_ids).WherePasses(
                DB.ElementIntersectsSolidFilter(_box_solid(bb, 0.0, to_host))).GetElementCount() > 0
        except Exception:
            continue
        out[k] = {'concrete': grown, 'pen': pen}
    return out


def box_mesh(bbox, to_host, origin):
    """Report mesh (mm, relative to origin) of a link-coordinates bounding box."""
    if not bbox:
        return None
    verts = []
    for x, y, z in ((0, 1, 2), (3, 1, 2), (3, 4, 2), (0, 4, 2), (0, 1, 5), (3, 1, 5), (3, 4, 5), (0, 4, 5)):
        p = to_host.OfPoint(DB.XYZ(bbox[x], bbox[y], bbox[z]))
        verts.extend([int(round((p.X - origin.X) * 304.8)), int(round((p.Y - origin.Y) * 304.8)),
                      int(round((p.Z - origin.Z) * 304.8))])
    idx = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
           1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7]
    return {'p': verts, 'i': idx}


def _host_outline(bbox, to_host, pad=0.0):
    return ccd.transform_outline(DB.XYZ(bbox[0], bbox[1], bbox[2]), DB.XYZ(bbox[3], bbox[4], bbox[5]), to_host, pad)


# ---------------------------------------------------------------- comparison
def run(host, zone, link, link_name, old, new, rules_path, out_dir, template_path=None,
        tolerance_mm=None, progress=None, context_m=6.0, version=None, labels=('', '')):
    """old: snapshot dict (load_snapshot). new: snapshot dict, or CURRENT to use the
    link as loaded now (then a new snapshot is built and returned for saving).
    Returns (summary, new_snapshot or None)."""
    with codecs.open(rules_path, 'r', 'utf-8') as f:
        cfg = json.load(f)
    if tolerance_mm is not None:
        cfg['tolerance_mm'] = float(tolerance_mm)
    tol = cfg['tolerance_mm'] * ccd.MM_TO_FT
    link_key = ccd.link_label(link)
    rules = {}
    for k, v in cfg['exclusions'].items():
        if k in link_key:
            rules = v
            break
    params = cfg['compare_params']
    zmin, zmax = zone['outline'].MinimumPoint, zone['outline'].MaximumPoint
    link_to_host = link.GetTotalTransform()
    host_to_link = link_to_host.Inverse

    ccd._report(progress, 'Collecting concrete in the zone...')
    concrete = ccd.concrete_elements(host, zone['outline'], cfg['concrete'], zone.get('solid'))
    conc_info = dict((ccd.eid(e.Id), '%s : %s [%d]' % (f or e.Category.Name, t, ccd.eid(e.Id)))
                     for e, f, t, _ in concrete)
    sb_link_outline = ccd.transform_outline(zmin, zmax, host_to_link, tol)

    # New side: the loaded link (live) or a saved snapshot
    live = new is CURRENT
    d = link.GetLinkDocument() if live else None
    if live:
        ccd._report(progress, 'Finding elements near the concrete...')
        near_new = ccd.near_concrete(d, concrete, host_to_link, sb_link_outline, rules, tol)
        new_ids = set(ccd.eid(i) for i in DB.FilteredElementCollector(d).WhereElementIsNotElementType().ToElementIds())
        new_els = {}
        for i, (k, e) in enumerate(near_new.items()):
            if i % 500 == 0:
                ccd._report(progress, 'Reading the current version...', i, len(near_new))
            new_els[k] = ccd.snapshot(d, e, params)
    else:
        near_new = {}
        new_ids, new_els = new['all_ids'], new['elements']

    old_ids, old_els = old['all_ids'], old['elements']

    # What changed. None = element does not exist in that version;
    # 'far' = it exists but was not near the concrete (no data kept for it).
    rows_src = {}
    keys = sorted(set(old_els) | set(new_els))
    for i, k in enumerate(keys):
        if i % 500 == 0:
            ccd._report(progress, 'Comparing versions...', i, len(keys))
        s_old = old_els.get(k) or ('far' if k in old_ids else None)
        s_new = new_els.get(k)
        e_new = None
        if s_new is None and k in new_ids:
            if live:
                e_new = d.GetElement(DB.ElementId(Int64(k)))
                s_new = ccd.snapshot(d, e_new, params) if e_new is not None else None
            else:
                s_new = 'far'
        elif live:
            e_new = near_new.get(k)
        if isinstance(s_old, dict) and isinstance(s_new, dict) and not ccd.diff_snapshots(s_old, s_new, params)[0]:
            continue
        rows_src[k] = (s_old, s_new, e_new)

    # Contact. Live side: real geometry. Saved side: stored result, else bounding box.
    def saved_contacts(snap, ids, label):
        known = dict((k, snap['contact'][k]) for k in ids if k in snap['contact'])
        ccd._report(progress, label)
        boxes = dict((k, snap['elements'][k]['bbox']) for k in ids
                     if k not in known and k in snap['elements'])
        found = box_contacts(host, concrete, link_to_host, boxes, tol)
        known.update(found)
        return dict((k, v) for k, v in known.items() if v and v.get('concrete'))

    old_side = [k for k, (so, sn, en) in rows_src.items() if isinstance(so, dict)]
    h_old = saved_contacts(old, old_side, 'Testing contact (previous)...')
    new_side = [k for k, (so, sn, en) in rows_src.items() if isinstance(sn, dict)]
    if live:
        cand = dict((k, rows_src[k][2] or d.GetElement(DB.ElementId(Int64(k)))) for k in new_side)
        cand = dict((k, e) for k, e in cand.items() if e is not None)
        h_new = ccd.contacts_for(d, concrete, host_to_link, cand, tol, progress, 'Testing contact (new)...')
        h_new = dict((k, {'concrete': sorted(v['concrete']), 'pen': v['pen']}) for k, v in h_new.items())
    else:
        h_new = saved_contacts(new, new_side, 'Testing contact (new)...')

    rows = []
    for k in sorted(set(h_old) | set(h_new)):
        s_old, s_new, e_new = rows_src[k]
        ho, hn = h_old.get(k), h_new.get(k)
        touching = sorted(set(ho['concrete'] if ho else []) | set(hn['concrete'] if hn else []))
        base = s_new if isinstance(s_new, dict) else s_old
        row = {'id': k, 'category': base['category'], 'family': base['family'], 'type': base['type'],
               'concrete_ids': touching, 'concrete': [conc_info.get(c, str(c)) for c in touching],
               'contact_old': ho is not None, 'contact_new': hn is not None,
               'penetrates': bool((hn or ho)['pen']), 'changes': [], 'move_mm': 0.0}
        if s_old is None:
            row['status'] = 'New'
        elif s_new is None:
            row['status'] = 'Deleted'
        else:
            row['status'] = 'Modified'
            if s_old == 'far':
                row['changes'] = ['Moved near the concrete (previous state not saved)']
            elif s_new == 'far':
                row['changes'] = ['Moved away from the concrete']
            else:
                row['changes'], row['move_mm'] = ccd.diff_snapshots(s_old, s_new, params)
            if row['contact_old'] != row['contact_new']:
                row['changes'].insert(0, 'Now in contact' if row['contact_new'] else 'No longer touches concrete')
        row['contact'] = 'Penetrates' if row['penetrates'] else 'Touches'
        rows.append(row)
    rows.sort(key=lambda r: (ccd.STATUS_ORDER.get(r['status'], 9), r['family'], r['id']))

    summary = {
        'scope_box': zone['label'], 'old': labels[0], 'new': labels[1], 'host': host.Title,
        'link': link_key, 'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'tolerance_mm': cfg['tolerance_mm'], 'concrete_elements': len(concrete),
        'near_old': len(old_els), 'near_new': len(new_els), 'changed_checked': len(rows_src),
        'created': sum(1 for r in rows if r['status'] == 'New'),
        'modified': sum(1 for r in rows if r['status'] == 'Modified'),
        'deleted': sum(1 for r in rows if r['status'] == 'Deleted'),
    }

    # Report geometry: real mesh for the live side, bounding box for saved data
    ccd._report(progress, 'Building the 3D report...')
    origin = (zmin + zmax) * 0.5
    meshes = []

    def add(m):
        if m is None:
            return -1
        meshes.append(m)
        return len(meshes) - 1

    boxes = []
    pad = context_m / 0.3048
    for r in rows:
        s_old, s_new, e_new = rows_src[r['id']]
        if r['status'] != 'Deleted':
            if live and e_new is None:
                e_new = d.GetElement(DB.ElementId(Int64(r['id'])))
            m = ccd.mesh_data(e_new, link_to_host, origin) if (live and e_new is not None) else None
            if m is None and isinstance(s_new, dict):
                m = box_mesh(s_new['bbox'], link_to_host, origin)
            r['mesh_new'] = add(m)
        else:
            r['mesh_new'] = -1
        r['mesh_old'] = add(box_mesh(s_old['bbox'], link_to_host, origin)) \
            if (isinstance(s_old, dict) and r['status'] != 'New') else -1
        for s in (s_old, s_new):
            if isinstance(s, dict) and s['bbox']:
                o = _host_outline(s['bbox'], link_to_host, pad)
                boxes.append((o.MinimumPoint, o.MaximumPoint))
    touched = set(c for r in rows for c in r['concrete_ids'])
    conc_out = []
    ident = DB.Transform.Identity
    for e, f, t, _ in concrete:
        k = ccd.eid(e.Id)
        if k not in touched:
            bb = e.get_BoundingBox(None)
            if bb is None or not any(bb.Min.X <= mx.X and bb.Max.X >= mn.X and bb.Min.Y <= mx.Y and bb.Max.Y >= mn.Y
                                     and bb.Min.Z <= mx.Z and bb.Max.Z >= mn.Z for mn, mx in boxes):
                continue
        conc_out.append({'id': k, 'name': conc_info[k], 'touched': k in touched,
                         'mesh': add(ccd.mesh_data(e, ident, origin))})

    _write_outputs(host, zone, origin, summary, rows, conc_out, meshes, out_dir, template_path)

    new_snap = None
    if live:
        # carry over stored contacts of unchanged elements, add the ones tested now
        contact = dict((k, v) for k, v in old['contact'].items() if k in new_els and k not in rows_src)
        for k in cand:
            contact[k] = h_new.get(k, {'concrete': [], 'pen': False})
        new_snap = {'link': link_name, 'zone': zone['label'], 'version': version,
                    'taken': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'source': d.Title, 'host': host.Title, 'tolerance_mm': cfg['tolerance_mm'],
                    'elements': new_els, 'all_ids': sorted(new_ids), 'contact': contact}
    return summary, new_snap


def baseline(host, zone, link, link_name, rules_path, tolerance_mm=None, progress=None, version=None):
    """Snapshot of the link as loaded now, without comparing (first run)."""
    with codecs.open(rules_path, 'r', 'utf-8') as f:
        cfg = json.load(f)
    if tolerance_mm is not None:
        cfg['tolerance_mm'] = float(tolerance_mm)
    tol = cfg['tolerance_mm'] * ccd.MM_TO_FT
    link_key = ccd.link_label(link)
    rules = {}
    for k, v in cfg['exclusions'].items():
        if k in link_key:
            rules = v
            break
    host_to_link = link.GetTotalTransform().Inverse
    d = link.GetLinkDocument()
    ccd._report(progress, 'Collecting concrete in the zone...')
    concrete = ccd.concrete_elements(host, zone['outline'], cfg['concrete'], zone.get('solid'))
    ccd._report(progress, 'Finding elements near the concrete...')
    near = ccd.near_concrete(d, concrete, host_to_link,
                             ccd.transform_outline(zone['outline'].MinimumPoint, zone['outline'].MaximumPoint,
                                                   host_to_link, tol), rules, tol)
    els = {}
    for i, (k, e) in enumerate(near.items()):
        if i % 500 == 0:
            ccd._report(progress, 'Saving the current version...', i, len(near))
        els[k] = ccd.snapshot(d, e, cfg['compare_params'])
    ids = sorted(ccd.eid(i) for i in DB.FilteredElementCollector(d).WhereElementIsNotElementType().ToElementIds())
    return {'link': link_name, 'zone': zone['label'], 'version': version,
            'taken': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': d.Title, 'host': host.Title, 'tolerance_mm': cfg['tolerance_mm'],
            'elements': els, 'all_ids': ids, 'contact': {}}


def _write_outputs(host, zone, origin, summary, rows, conc_out, meshes, out_dir, template_path):
    """Same files as concrete_contact_diff.run: JSON, CSV and the HTML report."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    try:
        ref = ccd.grids_levels(host, zone, origin)
    except Exception:
        ref = {'grids': [], 'grid_z': 0, 'levels': [], 'rect': None}
    data = {'summary': summary, 'rows': rows, 'concrete': conc_out, 'meshes': meshes, 'ref': ref}
    with codecs.open(os.path.join(out_dir, 'contact_diff.json'), 'w', 'utf-8') as f:
        json.dump({'summary': summary, 'rows': rows}, f, indent=1, ensure_ascii=False)
    with codecs.open(os.path.join(out_dir, 'contact_diff.csv'), 'w', 'utf-8-sig') as f:
        f.write('Status,Element Id,Category,Family,Type,Contact,Movement (mm),Changes,In contact with\n')
        for r in rows:
            cells = [r['status'], str(r['id']), r['category'], r['family'], r['type'],
                     r['contact'], str(r['move_mm'] or ''), ' | '.join(r['changes']), ' | '.join(r['concrete'])]
            f.write(','.join('"%s"' % (c or '').replace('"', '""') for c in cells) + '\n')
    if template_path:
        tpl = codecs.open(template_path, 'r', 'utf-8').read()
        payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        html = tpl.replace('/*__DATA__*/null', payload)
        fname = 'Report_%s.html' % re.sub(r'[^A-Za-z0-9_-]+', '_', zone['label'])
        summary['report'] = os.path.join(out_dir, fname)
        with codecs.open(summary['report'], 'w', 'utf-8') as f:
            f.write(html)
