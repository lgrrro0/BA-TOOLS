# -*- coding: utf-8 -*-
"""Grid dimension strings for the active view.

Grids visible in the view are grouped by direction (parallel grids, any
angle). At each end of a group the grid heads are located and two dimension
lines are placed inward from them, at fixed model offsets: the overall
dimension (first to last grid) and, further in, the chain dimension through
every grid. Only the grids that actually reach each dimension line are
dimensioned, so grids trimmed short in the view are left out (e.g. with C
and D trimmed, the chain goes B -> E). Works in view coordinates, so plans,
sections and elevations are handled the same way. Curved grids and
dimensions that already exist in the view are skipped.
"""
import math

import Autodesk.Revit.DB as DB

TOP_LEFT, BOTTOM_RIGHT, BOTH = 'top_left', 'bottom_right', 'both'
HEAD_TOL = 0.5   # ft: grid heads closer than this are considered aligned


def _eid(i):
    try:
        return int(i.Value)
    except Exception:
        return int(i.IntegerValue)


def _grid_line(grid, view):
    try:
        for c in grid.GetCurvesInView(DB.DatumExtentType.ViewSpecific, view):
            if isinstance(c, DB.Line):
                return c
    except Exception:
        pass
    c = grid.Curve
    return c if isinstance(c, DB.Line) else None


def _existing_sets(doc, view):
    sets = set()
    for d in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Dimension):
        try:
            ids = [_eid(r.ElementId) for r in d.References]
        except Exception:
            continue
        sets.add(frozenset(ids))
    return sets


def groups(doc, view):
    """[(direction (dx, dy), [(grid, s, t_lo, t_hi, depth)])] sorted by s, plus skipped [(grid, reason)].
    s: position across the grids; t_lo / t_hi: extents of the grid along its direction (view)."""
    O, u, v, w = view.Origin, view.RightDirection, view.UpDirection, view.ViewDirection
    items, skipped = [], []
    multi = set()
    for mg in DB.FilteredElementCollector(doc).OfClass(DB.MultiSegmentGrid):
        multi.update(_eid(i) for i in mg.GetGridIds())
    for g in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid):
        if _eid(g.Id) in multi:
            skipped.append((g, 'segment of a multi-segment grid'))
            continue
        if g.IsCurved:
            skipped.append((g, 'curved grid'))
            continue
        ln = _grid_line(g, view)
        if ln is None:
            skipped.append((g, 'no line in this view'))
            continue
        a, b = ln.GetEndPoint(0) - O, ln.GetEndPoint(1) - O
        p0, p1 = (a.DotProduct(u), a.DotProduct(v)), (b.DotProduct(u), b.DotProduct(v))
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 1e-3:
            skipped.append((g, 'seen end-on in this view'))
            continue
        dx, dy = dx / L, dy / L
        # canonical direction: "up" for vertical-ish grids, "left" for horizontal-ish
        if (abs(dy) >= abs(dx) and dy < 0) or (abs(dy) < abs(dx) and dx > 0):
            dx, dy = -dx, -dy
        items.append((g, p0, p1, (dx, dy), (a.DotProduct(w) + b.DotProduct(w)) / 2.0))
    out = []
    for g, p0, p1, d, depth in items:
        for gd, members in out:
            if abs(gd[0] * d[1] - gd[1] * d[0]) < 1e-3:
                members.append((g, p0, p1, depth))
                break
        else:
            out.append((d, [(g, p0, p1, depth)]))
    result = []
    for d, members in out:
        n = (-d[1], d[0])
        rows = []
        for g, p0, p1, depth in members:
            s = p0[0] * n[0] + p0[1] * n[1]
            t0, t1 = p0[0] * d[0] + p0[1] * d[1], p1[0] * d[0] + p1[1] * d[1]
            rows.append((g, s, min(t0, t1), max(t0, t1), depth))
        rows.sort(key=lambda r: r[1])
        result.append((d, rows))
    return result, skipped


def _head_position(heads, sign):
    """Position of the grid heads at one end: the one shared by most grids (within
    HEAD_TOL); on a tie, the outermost. sign -1: heads at the max end, +1: at the min end."""
    best = None
    for h in heads:
        n = sum(1 for x in heads if abs(x - h) <= HEAD_TOL)
        key = (n, -sign * h)
        if best is None or key > best[0]:
            best = (key, h)
    return best[1]


def _crossing(rows, t):
    """Grids that reach the line at t, without collinear duplicates."""
    out = []
    for r in rows:
        if r[2] - 1e-6 <= t <= r[3] + 1e-6 and (not out or abs(r[1] - out[-1][1]) >= 1e-4):
            out.append(r)
    return out


def create(doc, view, side=BOTH, overall_ft=2.0, chain_ft=4.0):
    """Creates the dimensions (inside an open transaction).
    overall_ft / chain_ft: model distance from the grid heads, inward, to the overall
    and chain dimension lines. Returns (created, skipped) as lists of
    (description, grids, dimension or None / reason)."""
    O, u, v, w = view.Origin, view.RightDirection, view.UpDirection, view.ViewDirection
    existing = _existing_sets(doc, view)
    grps, skipped_grids = groups(doc, view)
    created, skipped = [], [(g.Name, [g], why) for g, why in skipped_grids]

    def to3d(x, y, depth):
        return O + u.Multiply(x) + v.Multiply(y) + w.Multiply(depth)

    for d, rows in grps:
        if len(rows) < 2:
            skipped.append((rows[0][0].Name, [rows[0][0]], 'no parallel grid to dimension to'))
            continue
        n = (-d[1], d[0])
        ends = []
        if side in (TOP_LEFT, BOTH):
            ends.append((_head_position([r[3] for r in rows], -1), -1))
        if side in (BOTTOM_RIGHT, BOTH):
            ends.append((_head_position([r[2] for r in rows], 1), 1))
        for t_head, sign in ends:
            for kind, off in (('overall', overall_ft), ('chain', chain_ft)):
                t = t_head + sign * off
                on_line = _crossing(rows, t)
                if kind == 'overall':
                    if len(on_line) < 3:
                        continue   # with 2 grids the chain already is the overall
                    on_line = [on_line[0], on_line[-1]]
                grids = [r[0] for r in on_line]
                label = u'%s to %s' % (grids[0].Name, grids[-1].Name) if grids else u'-'
                desc = u'%s · %s' % (label, kind)
                if len(grids) < 2:
                    skipped.append((desc, grids, 'fewer than 2 grids reach the dimension line'))
                    continue
                key = frozenset(_eid(g.Id) for g in grids)
                if key in existing:
                    skipped.append((desc, grids, 'already dimensioned'))
                    continue
                depth = sum(r[4] for r in on_line) / len(on_line)
                s0, s1 = on_line[0][1], on_line[-1][1]
                a = (n[0] * s0 + d[0] * t, n[1] * s0 + d[1] * t)
                b = (n[0] * s1 + d[0] * t, n[1] * s1 + d[1] * t)
                line = DB.Line.CreateBound(to3d(a[0], a[1], depth), to3d(b[0], b[1], depth))
                refs = DB.ReferenceArray()
                for g in grids:
                    refs.Append(DB.Reference(g))
                try:
                    dim = doc.Create.NewDimension(view, line, refs)
                    created.append((desc, grids, dim))
                except Exception as ex:
                    skipped.append((desc, grids, 'Revit error: %s' % ex))
    return created, skipped
