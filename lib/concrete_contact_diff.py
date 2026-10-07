# -*- coding: utf-8 -*-
"""Concrete contact diff.

Compares two versions of a linked model (old/new, opened as detached documents)
and reports only the elements that touch concrete elements of the host model
inside a scope box, within a tolerance. Output: created / modified / deleted,
as CSV, JSON and a self-contained HTML report.
"""
import json, re, codecs, os, datetime
from System import Int64
from System.Collections.Generic import List
import Autodesk.Revit.DB as DB

MM_TO_FT = 1.0 / 304.8
STATUS_ORDER = {'New': 0, 'Modified': 1, 'Deleted': 2}


def eid(i):
    try:
        return int(i.Value)
    except Exception:
        return int(i.IntegerValue)


def ename(e):
    try:
        return DB.Element.Name.GetValue(e)
    except Exception:
        return getattr(e, 'Name', '') or ''


def fam_type(d, e):
    tid = e.GetTypeId()
    t = d.GetElement(tid) if tid != DB.ElementId.InvalidElementId else None
    fam = ''
    if isinstance(e, DB.FamilyInstance):
        try:
            fam = e.Symbol.Family.Name
        except Exception:
            pass
    if not fam and t is not None:
        fam = getattr(t, 'FamilyName', '') or ''
    return fam, (ename(t) if t is not None else '')


def walk_geometry(e, on_solid, on_mesh=None):
    opts = DB.Options()
    opts.DetailLevel = DB.ViewDetailLevel.Fine

    def walk(geo):
        for g in geo:
            if isinstance(g, DB.Solid):
                if g.Volume > 1e-6:
                    on_solid(g)
            elif isinstance(g, DB.Mesh):
                if on_mesh:
                    on_mesh(g)
            elif isinstance(g, DB.GeometryInstance):
                walk(g.GetInstanceGeometry())
    geo = e.get_Geometry(opts)
    if geo:
        walk(geo)


def get_solids(e):
    out = []
    walk_geometry(e, out.append)
    return out


def mesh_edges(e):
    """Unique triangle edges of the mesh geometry of e (e.g. MEP Fabrication parts,
    IFC DirectShapes), or None when e has no meshes. ElementIntersectsSolidFilter
    ignores meshes, so these parts are tested edge by edge against the concrete."""
    meshes = []
    walk_geometry(e, lambda s: None, meshes.append)
    if not meshes:
        return None
    lines, seen = [], set()
    for m in meshes:
        for j in range(m.NumTriangles):
            tr = m.get_Triangle(j)
            for a, b in ((0, 1), (1, 2), (2, 0)):
                p, q = tr.get_Vertex(a), tr.get_Vertex(b)
                kp = (round(p.X, 4), round(p.Y, 4), round(p.Z, 4))
                kq = (round(q.X, 4), round(q.Y, 4), round(q.Z, 4))
                key = (kp, kq) if kp < kq else (kq, kp)
                if key in seen:
                    continue
                seen.add(key)
                if p.DistanceTo(q) > 0.003:
                    lines.append(DB.Line.CreateBound(p, q))
    return lines


def edges_hit_solid(lines, solid, opts):
    for l in lines:
        try:
            if solid.IntersectWithCurve(l, opts).SegmentCount > 0:
                return True
        except Exception:
            pass
    return False


def mesh_data(e, T, origin):
    """Triangulated geometry of e in host coordinates, in mm relative to origin."""
    verts, idx = [], []

    def add_mesh(m):
        base = len(verts) // 3
        for j in range(m.Vertices.Count):
            p = T.OfPoint(m.Vertices[j])
            verts.extend([int(round((p.X - origin.X) * 304.8)),
                          int(round((p.Y - origin.Y) * 304.8)),
                          int(round((p.Z - origin.Z) * 304.8))])
        for j in range(m.NumTriangles):
            tr = m.get_Triangle(j)
            idx.extend([int(base + tr.get_Index(0)), int(base + tr.get_Index(1)), int(base + tr.get_Index(2))])

    def add_solid(s):
        for f in s.Faces:
            try:
                add_mesh(f.Triangulate())
            except Exception:
                pass
    walk_geometry(e, add_solid, add_mesh)
    return {'p': verts, 'i': idx} if idx else None


def transform_outline(mn, mx, t, pad=0.0):
    xs, ys, zs = [], [], []
    for x in (mn.X - pad, mx.X + pad):
        for y in (mn.Y - pad, mx.Y + pad):
            for z in (mn.Z - pad, mx.Z + pad):
                p = t.OfPoint(DB.XYZ(x, y, z))
                xs.append(p.X); ys.append(p.Y); zs.append(p.Z)
    return DB.Outline(DB.XYZ(min(xs), min(ys), min(zs)), DB.XYZ(max(xs), max(ys), max(zs)))


def id_list(ids):
    return List[DB.ElementId]([DB.ElementId(Int64(i)) for i in ids])


def zone_from_scope_box(host, name):
    sb = [s for s in DB.FilteredElementCollector(host).OfCategory(DB.BuiltInCategory.OST_VolumeOfInterest)
          .WhereElementIsNotElementType() if ename(s) == name][0]
    bb = sb.get_BoundingBox(None)
    return {'label': name, 'outline': DB.Outline(bb.Min, bb.Max), 'solid': None}


def zone_from_model(host, exclude_categories=()):
    """Zone covering the whole host model: the bounding box of every element that
    concrete_elements would use (model elements with solid geometry), so sheets,
    sketches and other non-physical elements do not stretch the report frame."""
    big = 1e5
    everything = DB.Outline(DB.XYZ(-big, -big, -big), DB.XYZ(big, big, big))
    mn = [1e9, 1e9, 1e9]
    mx = [-1e9, -1e9, -1e9]
    for e, _, _, _ in concrete_elements(host, everything, {'exclude_categories': list(exclude_categories)}):
        bb = e.get_BoundingBox(None)
        if bb is None:
            continue
        mn = [min(mn[0], bb.Min.X), min(mn[1], bb.Min.Y), min(mn[2], bb.Min.Z)]
        mx = [max(mx[0], bb.Max.X), max(mx[1], bb.Max.Y), max(mx[2], bb.Max.Z)]
    if mn[0] > mx[0]:
        return None
    return {'label': 'Whole model', 'outline': DB.Outline(DB.XYZ(*mn), DB.XYZ(*mx)), 'solid': None}


def zone_from_section_box(view):
    """Zone from the section box of a 3D view. The box may be rotated: 'outline' is its
    axis-aligned envelope (fast prefilter), 'solid' its exact volume."""
    box = view.GetSectionBox()
    t = box.Transform
    mn, mx = box.Min, box.Max
    corners = [t.OfPoint(DB.XYZ(x, y, z)) for x in (mn.X, mx.X) for y in (mn.Y, mx.Y) for z in (mn.Z, mx.Z)]
    outline = DB.Outline(DB.XYZ(min(p.X for p in corners), min(p.Y for p in corners), min(p.Z for p in corners)),
                         DB.XYZ(max(p.X for p in corners), max(p.Y for p in corners), max(p.Z for p in corners)))
    pts = [DB.XYZ(mn.X, mn.Y, mn.Z), DB.XYZ(mx.X, mn.Y, mn.Z), DB.XYZ(mx.X, mx.Y, mn.Z), DB.XYZ(mn.X, mx.Y, mn.Z)]
    loop = DB.CurveLoop()
    for i in range(4):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % 4]))
    solid = DB.GeometryCreationUtilities.CreateExtrusionGeometry(
        List[DB.CurveLoop]([loop]), DB.XYZ.BasisZ, mx.Z - mn.Z)
    solid = DB.SolidUtils.CreateTransformed(solid, t)
    return {'label': 'Section box - %s' % view.Name, 'outline': outline, 'solid': solid}


def concrete_elements(host, outline, cfg, solid=None):
    """Every model element of the concrete (host) model inside the zone that has
    solid geometry. Returns [(element, family, type, solids)]."""
    skip = set(cfg.get('exclude_categories', []))
    res = []
    col = (DB.FilteredElementCollector(host).WhereElementIsNotElementType()
           .WherePasses(DB.BoundingBoxIntersectsFilter(outline)))
    if solid is not None:
        col = col.WherePasses(DB.ElementIntersectsSolidFilter(solid))
    for e in col:
        cat = e.Category
        if cat is None or cat.CategoryType != DB.CategoryType.Model:
            continue
        if isinstance(e, (DB.RevitLinkInstance, DB.ImportInstance)):
            continue
        try:
            if str(cat.BuiltInCategory) in skip:
                continue
        except Exception:
            pass
        solids = get_solids(e)
        if not solids:
            continue
        fam, typ = fam_type(host, e)
        res.append((e, fam, typ, solids))
    return res


def excluded(e, fam, typ, rules):
    if e.Category is None or e.Category.CategoryType != DB.CategoryType.Model:
        return True
    if e.Category.Name in rules.get('categories', []):
        return True
    for rx in rules.get('family_regex', []):
        if re.search(rx, fam or ''):
            return True
    for rx in rules.get('type_regex', []):
        if re.search(rx, typ or ''):
            return True
    return False


class Cancelled(Exception):
    pass


def _report(progress, text, i=0, n=0):
    """Calls the progress callback; it returns False to cancel."""
    if progress is not None and progress(text, i, n) is False:
        raise Cancelled('Comparison cancelled by the user.')


def near_concrete(d, concrete, host_to_link, zone_outline, rules, tol):
    """{id: element} of the model elements of d inside the zone whose bounding box is
    within tol of the bounding box of any concrete element. Native filters only, so it
    is fast even with tens of thousands of elements; any real contact passes this test."""
    flt = List[DB.ElementFilter]()
    for ce, _, _, _ in concrete:
        bb = ce.get_BoundingBox(None)
        if bb is not None:
            flt.Add(DB.BoundingBoxIntersectsFilter(transform_outline(bb.Min, bb.Max, host_to_link, tol)))
    if flt.Count == 0:
        return {}
    near = flt[0] if flt.Count == 1 else DB.LogicalOrFilter(flt)
    out = {}
    for e in (DB.FilteredElementCollector(d).WhereElementIsNotElementType()
              .WherePasses(DB.BoundingBoxIntersectsFilter(zone_outline)).WherePasses(near)):
        fam, typ = fam_type(d, e)
        if not excluded(e, fam, typ, rules):
            out[eid(e.Id)] = e
    return out


def contacts(d, concrete, host_to_link, sb_outline, rules, tol):
    """Return ({element_id: {'concrete': set(ids), 'pen': bool}}, candidate count) for
    every element of d inside sb_outline (used by the 'hide without contact' tool)."""
    cand = {}
    for e in (DB.FilteredElementCollector(d).WhereElementIsNotElementType()
              .WherePasses(DB.BoundingBoxIntersectsFilter(sb_outline))):
        fam, typ = fam_type(d, e)
        if not excluded(e, fam, typ, rules):
            cand[eid(e.Id)] = e
    return contacts_for(d, concrete, host_to_link, cand, tol), len(cand)


def contacts_for(d, concrete, host_to_link, cand, tol, progress=None, label=''):
    """Contact test for the given elements {id: element} of d.
    Returns {element_id: {'concrete': set(ids), 'pen': bool}}.

    'pen' is True when the element intersects the concrete itself (penetration),
    False when it is only within the tolerance (touching).
    """
    if not cand:
        return {}
    cand_ids = id_list(cand.keys())
    offsets = [DB.XYZ(0, 0, 0)]
    if tol > 0:
        offsets += [DB.XYZ(tol, 0, 0), DB.XYZ(-tol, 0, 0),
                    DB.XYZ(0, tol, 0), DB.XYZ(0, -tol, 0),
                    DB.XYZ(0, 0, tol), DB.XYZ(0, 0, -tol)]
    hits = {}
    mesh_cache = {}
    sci = DB.SolidCurveIntersectionOptions()
    for ci, (ce, _, _, solids) in enumerate(concrete):
        if ci % 25 == 0:
            _report(progress, label, ci, len(concrete))
        bb = ce.get_BoundingBox(None)
        if bb is None:
            continue
        near = (DB.FilteredElementCollector(d, cand_ids)
                .WherePasses(DB.BoundingBoxIntersectsFilter(transform_outline(bb.Min, bb.Max, host_to_link, tol)))
                .ToElementIds())
        remaining = set(eid(i) for i in near)
        if not remaining:
            continue
        for k in remaining:
            if k not in mesh_cache:
                mesh_cache[k] = mesh_edges(cand[k])
        for s in solids:
            s_link = DB.SolidUtils.CreateTransformed(s, host_to_link)
            for oi, off in enumerate(offsets):
                if not remaining:
                    break
                s_try = s_link if oi == 0 else DB.SolidUtils.CreateTransformed(s_link, DB.Transform.CreateTranslation(off))
                try:
                    found = [eid(i) for i in (DB.FilteredElementCollector(d, id_list(remaining))
                             .WherePasses(DB.ElementIntersectsSolidFilter(s_try)).ToElementIds())]
                except Exception:
                    found = []
                found += [k for k in remaining if k not in found and mesh_cache.get(k)
                          and edges_hit_solid(mesh_cache[k], s_try, sci)]
                for k in found:
                    h = hits.setdefault(k, {'concrete': set(), 'pen': False})
                    h['concrete'].add(eid(ce.Id))
                    if oi == 0:
                        h['pen'] = True
                    remaining.discard(k)
    return hits


def view_outline(host, view):
    """Volume shown by a view, in host coordinates, or None when unbounded.
    Plans: crop region + view range. 3D: section box. Sections/elevations: crop box."""
    def box_outline(bx):
        xs, ys, zs = [], [], []
        for x in (bx.Min.X, bx.Max.X):
            for y in (bx.Min.Y, bx.Max.Y):
                for z in (bx.Min.Z, bx.Max.Z):
                    p = bx.Transform.OfPoint(DB.XYZ(x, y, z))
                    xs.append(p.X); ys.append(p.Y); zs.append(p.Z)
        return (DB.XYZ(min(xs), min(ys), min(zs)), DB.XYZ(max(xs), max(ys), max(zs)))

    big = 1e5
    if isinstance(view, DB.ViewPlan):
        vr = view.GetViewRange()

        def z(plane, default):
            lid = vr.GetLevelId(plane)
            if lid == DB.ElementId.InvalidElementId:
                return default
            return host.GetElement(lid).ProjectElevation + vr.GetOffset(plane)
        zmin = z(DB.PlanViewPlane.ViewDepthPlane, -big)
        zmax = z(DB.PlanViewPlane.TopClipPlane, big)
        if view.CropBoxActive:
            mn, mx = box_outline(view.CropBox)
            return DB.Outline(DB.XYZ(mn.X, mn.Y, zmin), DB.XYZ(mx.X, mx.Y, zmax))
        return DB.Outline(DB.XYZ(-big, -big, zmin), DB.XYZ(big, big, zmax))
    if isinstance(view, DB.View3D):
        if view.IsSectionBoxActive:
            return DB.Outline(*box_outline(view.GetSectionBox()))
        return None
    if view.CropBoxActive:
        return DB.Outline(*box_outline(view.CropBox))
    return None


def visible_host_elements(host, view):
    """Model elements of the host visible in view that have solid geometry."""
    res = []
    for e in DB.FilteredElementCollector(host, view.Id).WhereElementIsNotElementType():
        cat = e.Category
        if cat is None or cat.CategoryType != DB.CategoryType.Model:
            continue
        if isinstance(e, (DB.RevitLinkInstance, DB.ImportInstance)):
            continue
        solids = get_solids(e)
        if solids:
            res.append((e, '', '', solids))
    return res


def view_contacts(host, view, links, tolerance_mm):
    """For each link: (link, contact ids, candidate elements inside the view volume).
    Contact = linked element within tolerance of an element of the host visible in view."""
    tol = tolerance_mm * MM_TO_FT
    concrete = visible_host_elements(host, view)
    vol = view_outline(host, view)
    out = []
    for link in links:
        d = link.GetLinkDocument()
        if d is None:
            continue
        h2l = link.GetTotalTransform().Inverse
        col = DB.FilteredElementCollector(d).WhereElementIsNotElementType()
        if vol is not None:
            outline = transform_outline(vol.MinimumPoint, vol.MaximumPoint, h2l)
            col = col.WherePasses(DB.BoundingBoxIntersectsFilter(outline))
        else:
            outline = None
        cand = [e for e in col if e.Category is not None and e.Category.CategoryType == DB.CategoryType.Model]
        if outline is None:
            bbs = [e.get_BoundingBox(None) for e, _, _, _ in concrete]
            bbs = [b for b in bbs if b is not None]
            if not bbs:
                out.append((link, set(), cand))
                continue
            outline = transform_outline(
                DB.XYZ(min(b.Min.X for b in bbs), min(b.Min.Y for b in bbs), min(b.Min.Z for b in bbs)),
                DB.XYZ(max(b.Max.X for b in bbs), max(b.Max.Y for b in bbs), max(b.Max.Z for b in bbs)), h2l, tol)
        hits, _ = contacts(d, concrete, h2l, outline, {}, tol)
        out.append((link, set(hits), cand))
    return out, len(concrete)


def pval(p):
    st = p.StorageType
    if st == DB.StorageType.String:
        return p.AsString()
    if st == DB.StorageType.ElementId:
        return p.AsValueString() or str(eid(p.AsElementId()))
    return p.AsValueString()


def snapshot(d, e, param_names):
    fam, typ = fam_type(d, e)
    bb = e.get_BoundingBox(None)
    snap = {
        'category': e.Category.Name, 'family': fam, 'type': typ,
        'type_id': eid(e.GetTypeId()),
        'bbox': [bb.Min.X, bb.Min.Y, bb.Min.Z, bb.Max.X, bb.Max.Y, bb.Max.Z] if bb else None,
        'params': {},
    }
    for n in param_names:
        p = e.LookupParameter(n)
        if p is not None and p.HasValue:
            snap['params'][n] = pval(p)
    return snap


def _clip_segment(p, q, xmin, ymin, xmax, ymax):
    """Liang-Barsky clip of segment p-q (XY tuples) to a rectangle. None if outside."""
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if abs(pk) < 1e-12:
            if qk < 0:
                return None
            continue
        r = qk / pk
        if pk < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def grids_levels(host, zone, origin):
    """Grids of the host clipped to the zone footprint (plus a margin) and the levels
    whose elevation falls inside the zone, in mm relative to origin (report coords)."""
    mn, mx = zone['outline'].MinimumPoint, zone['outline'].MaximumPoint
    padx = (mx.X - mn.X) * 0.08 + 3.0
    pady = (mx.Y - mn.Y) * 0.08 + 3.0
    xmin, ymin, xmax, ymax = mn.X - padx, mn.Y - pady, mx.X + padx, mx.Y + pady

    def mm_xy(x, y):
        return [int(round((x - origin.X) * 304.8)), int(round((y - origin.Y) * 304.8))]

    seg_name = {}
    for mg in DB.FilteredElementCollector(host).OfClass(DB.MultiSegmentGrid):
        for gid in mg.GetGridIds():
            seg_name[eid(gid)] = mg.Name
    grids = []
    for g in DB.FilteredElementCollector(host).OfClass(DB.Grid):
        name = seg_name.get(eid(g.Id), g.Name)
        c = g.Curve
        pts = [c.GetEndPoint(0), c.GetEndPoint(1)] if isinstance(c, DB.Line) else list(c.Tessellate())
        lines, cur = [], []
        for a, b in zip(pts[:-1], pts[1:]):
            seg = _clip_segment((a.X, a.Y), (b.X, b.Y), xmin, ymin, xmax, ymax)
            if seg is None:
                if len(cur) > 1:
                    lines.append(cur)
                cur = []
                continue
            s0, s1 = seg
            if cur and (abs(cur[-1][0] - s0[0]) > 1e-6 or abs(cur[-1][1] - s0[1]) > 1e-6):
                lines.append(cur)
                cur = []
            if not cur:
                cur = [s0]
            cur.append(s1)
        if len(cur) > 1:
            lines.append(cur)
        for pl in lines:
            grids.append({'name': name, 'p': [mm_xy(x, y) for x, y in pl]})

    levels = []
    for lv in DB.FilteredElementCollector(host).OfClass(DB.Level):
        z = lv.ProjectElevation
        if mn.Z - 0.5 <= z <= mx.Z + 0.5:
            p = lv.get_Parameter(DB.BuiltInParameter.LEVEL_ELEV)
            levels.append({'name': lv.Name, 'z': int(round((z - origin.Z) * 304.8)),
                           'elev': (p.AsValueString() if p else '') or ''})
    levels.sort(key=lambda l: l['z'])
    grid_z = levels[0]['z'] if levels else int(round((mn.Z - origin.Z) * 304.8))
    return {'grids': grids, 'grid_z': grid_z, 'levels': levels,
            'rect': mm_xy(mn.X, mn.Y) + mm_xy(mx.X, mx.Y)}


def link_label(link):
    ld = link.GetLinkDocument()
    return ld.Title if ld is not None else link.Name.split(' : ')[0]


def diff_snapshots(s_old, s_new, param_names):
    """(changes, move_mm) between two snapshots of the same element."""
    ch, move = [], 0.0
    if s_old['type_id'] != s_new['type_id']:
        ch.append('Type: %s -> %s' % (s_old['type'], s_new['type']))
    if s_old['bbox'] and s_new['bbox']:
        mv = max(abs(a - b) for a, b in zip(s_old['bbox'], s_new['bbox'])) * 304.8
        if mv > 1.0:
            move = round(mv, 1)
            ch.append('Geometry/position: %.0f mm' % mv)
    for n in param_names:
        a, b = s_old['params'].get(n), s_new['params'].get(n)
        if a != b:
            ch.append('%s: %s -> %s' % (n, a, b))
    return ch, move


def run(host, zone, old, new, link, rules_path, out_dir, template_path=None, tolerance_mm=None,
        progress=None, context_m=6.0):
    """host: concrete model; zone: scope box name or a dict from zone_from_section_box;
    old/new: open (detached) versions of the linked model;
    link: RevitLinkInstance of that model in host (gives the coordinate transform).
    tolerance_mm overrides the value in the rules file.
    progress(text, i, n) -> False cancels (raises Cancelled).
    context_m: concrete within this distance of the changes is added to the 3D report.

    Only elements that changed between versions can appear in the report, so the
    expensive contact test runs only on them: 1) native bbox filter keeps the elements
    near the concrete, 2) versions are compared on that set, 3) contact is tested only
    for the changed elements."""
    if not isinstance(zone, dict):
        zone = zone_from_scope_box(host, zone)
    sb_name = zone['label']
    with codecs.open(rules_path, 'r', 'utf-8') as f:
        cfg = json.load(f)
    if tolerance_mm is not None:
        cfg['tolerance_mm'] = float(tolerance_mm)
    tol = cfg['tolerance_mm'] * MM_TO_FT
    link_key = link_label(link)
    rules = {}
    for k, v in cfg['exclusions'].items():
        if k in link_key:
            rules = v
            break

    zmin, zmax = zone['outline'].MinimumPoint, zone['outline'].MaximumPoint

    link_to_host = link.GetTotalTransform()
    host_to_link = link_to_host.Inverse

    _report(progress, 'Collecting concrete in the zone...')
    concrete = concrete_elements(host, zone['outline'], cfg['concrete'], zone.get('solid'))
    conc_info = dict((eid(e.Id), '%s : %s [%d]' % (f or e.Category.Name, t, eid(e.Id))) for e, f, t, _ in concrete)
    sb_link_outline = transform_outline(zmin, zmax, host_to_link, tol)

    # 1) elements near the concrete (native filters)
    _report(progress, 'Finding elements near the concrete...')
    near_old = near_concrete(old, concrete, host_to_link, sb_link_outline, rules, tol)
    near_new = near_concrete(new, concrete, host_to_link, sb_link_outline, rules, tol)

    # 2) what changed between versions, among those elements
    params = cfg['compare_params']
    snaps = {}
    changed_old, changed_new = {}, {}
    keys = sorted(set(near_old) | set(near_new))
    for i, k in enumerate(keys):
        if i % 500 == 0:
            _report(progress, 'Comparing versions...', i, len(keys))
        e_old = near_old.get(k) or old.GetElement(DB.ElementId(Int64(k)))
        e_new = near_new.get(k) or new.GetElement(DB.ElementId(Int64(k)))
        s_old = snapshot(old, e_old, params) if e_old is not None else None
        s_new = snapshot(new, e_new, params) if e_new is not None else None
        if s_old is not None and s_new is not None and not diff_snapshots(s_old, s_new, params)[0]:
            continue
        snaps[k] = (e_old, e_new, s_old, s_new)
        if e_old is not None:
            changed_old[k] = e_old
        if e_new is not None:
            changed_new[k] = e_new

    # 3) contact test only for the changed elements
    h_old = contacts_for(old, concrete, host_to_link, changed_old, tol, progress, 'Testing contact (previous)...')
    h_new = contacts_for(new, concrete, host_to_link, changed_new, tol, progress, 'Testing contact (new)...')

    rows = []
    for k in sorted(set(h_old) | set(h_new)):
        e_old, e_new, s_old, s_new = snaps[k]
        ho, hn = h_old.get(k), h_new.get(k)
        touching = sorted((ho['concrete'] if ho else set()) | (hn['concrete'] if hn else set()))
        base = s_new or s_old
        row = {'id': k, 'category': base['category'], 'family': base['family'], 'type': base['type'],
               'concrete_ids': touching,
               'concrete': [conc_info.get(c, str(c)) for c in touching],
               'contact_old': ho is not None, 'contact_new': hn is not None,
               'penetrates': bool((hn or ho)['pen']),
               'changes': [], 'move_mm': 0.0}
        if s_old is None:
            row['status'] = 'New'
        elif s_new is None:
            row['status'] = 'Deleted'
        else:
            row['status'] = 'Modified'
            row['changes'], row['move_mm'] = diff_snapshots(s_old, s_new, params)
            if row['contact_old'] != row['contact_new']:
                row['changes'].insert(0, 'Now in contact' if row['contact_new'] else 'No longer touches concrete')
        row['contact'] = 'Penetrates' if row['penetrates'] else 'Touches'
        row['_old'], row['_new'] = e_old, e_new
        rows.append(row)
    rows.sort(key=lambda r: (STATUS_ORDER.get(r['status'], 9), r['family'], r['id']))

    summary = {
        'scope_box': sb_name, 'old': old.Title, 'new': new.Title, 'host': host.Title,
        'link': link_key, 'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'tolerance_mm': cfg['tolerance_mm'], 'concrete_elements': len(concrete),
        'near_old': len(near_old), 'near_new': len(near_new), 'changed_checked': len(snaps),
        'created': sum(1 for r in rows if r['status'] == 'New'),
        'modified': sum(1 for r in rows if r['status'] == 'Modified'),
        'deleted': sum(1 for r in rows if r['status'] == 'Deleted'),
    }

    # Geometry for the 3D report: changed elements (old/new states) + zone concrete.
    origin = (zmin + zmax) * 0.5
    meshes = []

    def add(m):
        if m is None:
            return -1
        meshes.append(m)
        return len(meshes) - 1

    ident = DB.Transform.Identity
    out_rows = []
    for r in rows:
        e_old, e_new = r.pop('_old'), r.pop('_new')
        r['mesh_new'] = add(mesh_data(e_new, link_to_host, origin)) if e_new is not None else -1
        r['mesh_old'] = add(mesh_data(e_old, link_to_host, origin)) if (e_old is not None and r['status'] != 'New') else -1
        out_rows.append(r)
    # Concrete for the report: the elements touched by a change plus those within
    # context_m of a change (the full zone can be hundreds of elements).
    _report(progress, 'Building the 3D report...')
    touched = set(c for r in rows for c in r['concrete_ids'])
    boxes = []
    pad = context_m / 0.3048
    for r in rows:
        for e in (snaps[r['id']][0], snaps[r['id']][1]):
            if e is None:
                continue
            bb = e.get_BoundingBox(None)
            if bb is not None:
                o = transform_outline(bb.Min, bb.Max, link_to_host, pad)
                boxes.append((o.MinimumPoint, o.MaximumPoint))
    conc_out = []
    for e, f, t, _ in concrete:
        k = eid(e.Id)
        if k not in touched:
            bb = e.get_BoundingBox(None)
            if bb is None or not any(bb.Min.X <= mx.X and bb.Max.X >= mn.X and bb.Min.Y <= mx.Y and bb.Max.Y >= mn.Y
                                     and bb.Min.Z <= mx.Z and bb.Max.Z >= mn.Z for mn, mx in boxes):
                continue
        conc_out.append({'id': k, 'name': conc_info[k], 'touched': k in touched,
                         'mesh': add(mesh_data(e, ident, origin))})

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    try:
        ref = grids_levels(host, zone, origin)
    except Exception:
        ref = {'grids': [], 'grid_z': 0, 'levels': [], 'rect': None}
    data = {'summary': summary, 'rows': out_rows, 'concrete': conc_out, 'meshes': meshes, 'ref': ref}
    with codecs.open(os.path.join(out_dir, 'contact_diff.json'), 'w', 'utf-8') as f:
        json.dump({'summary': summary, 'rows': out_rows}, f, indent=1, ensure_ascii=False)
    with codecs.open(os.path.join(out_dir, 'contact_diff.csv'), 'w', 'utf-8-sig') as f:
        f.write('Status,Element Id,Category,Family,Type,Contact,Movement (mm),Changes,In contact with\n')
        for r in out_rows:
            cells = [r['status'], str(r['id']), r['category'], r['family'], r['type'],
                     r['contact'], str(r['move_mm'] or ''), ' | '.join(r['changes']), ' | '.join(r['concrete'])]
            f.write(','.join('"%s"' % (c or '').replace('"', '""') for c in cells) + '\n')
    if template_path:
        tpl = codecs.open(template_path, 'r', 'utf-8').read()
        payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        html = tpl.replace('/*__DATA__*/null', payload)
        fname = 'Report_%s.html' % re.sub(r'[^A-Za-z0-9_-]+', '_', sb_name)
        summary['report'] = os.path.join(out_dir, fname)
        with codecs.open(summary['report'], 'w', 'utf-8') as f:
            f.write(html)
    return summary
