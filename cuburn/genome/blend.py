#!/usr/bin/python

# Copyright 2011-2012   Erik Reckase <e.reckase@gmail.com>,
#                       Steven Robertson <steven@strobe.cc>.

import numpy as np
from copy import deepcopy
from itertools import izip_longest

import spectypes
import specs
from use import Wrapper
from util import get, json_encode, resolve_spec, flatten, unflatten
import variations

def node_to_anim(gdb, node, half):
    node = resolve(gdb, node)
    if half:
        osrc, odst = -0.25, 0.25
    else:
        osrc, odst = 0, 1
    src = apply_temporal_offset(node, osrc)
    dst = apply_temporal_offset(node, odst)
    edge = dict(blend=dict(duration=odst-osrc, xform_sort='natural'))
    return blend(src, dst, edge)

def edge_to_anim(gdb, edge):
    edge = resolve(gdb, edge)
    src, osrc = _split_ref_id(edge['link']['src'])
    dst, odst = _split_ref_id(edge['link']['dst'])
    src = apply_temporal_offset(resolve(gdb, gdb.get(src)), osrc)
    dst = apply_temporal_offset(resolve(gdb, gdb.get(dst)), odst)
    return blend(src, dst, edge)

def resolve(gdb, item):
    """
    Given an item, recursively retrieve its base items, then merge according
    to type. Returns the merged dict.
    """
    is_edge = (item['type'] == 'edge')
    spec = specs.toplevels[item['type']]
    def go(i):
        if i.get('base') is not None:
            return go(gdb.get(i['base'])) + [i]
        return [i]
    items = map(flatten, go(item))
    out = {}

    for k in set(ik for i in items for ik in i.keys()):
        sp = resolve_spec(spec, k.split('.'))
        vs = [i.get(k) for i in items if k in i]
        # TODO: dict and list negation; early-stage removal of negated knots?
        if is_edge and isinstance(sp, (spectypes.Spline, spectypes.List)):
            r = sum(vs, [])
        else:
            r = vs[-1]
        out[k] = r
    return unflatten(out)

def _split_ref_id(s):
    sp = s.split('@')
    if len(sp) == 1:
        return sp, 0
    return sp[0], float(sp[1])

def apply_temporal_offset(node, offset=0):
    """
    Given a ``node`` dict, return a node with all periodic splines rotated by
    ``offset * velocity``, with the same velocity.
    """
    class TemporalOffsetWrapper(Wrapper):
        def wrap_spline(self, path, spec, val):
            if spec.period is not None and isinstance(val, list) and val[1]:
                position, velocity = val
                return [position + offset * velocity, velocity]
            return val
    wr = TemporalOffsetWrapper(node)
    return wr.visit(wr)

def blend(src, dst, edit={}):
    """
    Blend two nodes to produce an animation.

    ``src`` and ``dst`` are the source and destination node specs for the
    animation. These should be plain node dicts (hierarchical, pre-merged,
    and adjusted for loop temporal offset).

    ``edge`` is an edge dict, also hierarchical and pre-merged. (It can be
    empty, in violation of the spec, to support rendering straight from nodes
    without having to insert anything into the genome database.)

    Returns the animation spec as a plain dict.
    """
    # By design, the blend element will contain only scalar values (no
    # splines or hierarchy), so this can be done blindly
    opts = {}
    for d in src, dst, edit:
        opts.update(d.get('blend', {}))
    opts = Wrapper(opts, specs.blend)

    blended = merge_nodes(specs.node, src, dst, edit, opts.duration)
    name_map = sort_xforms(src['xforms'], dst['xforms'], opts.xform_sort,
                           explicit=opts.xform_map)

    blended['xforms'] = {}
    for (sxf_key, dxf_key) in name_map:
        bxf_key = (sxf_key or 'pad') + '_' + (dxf_key or 'pad')
        xf_edits = merge_edits(specs.xform,
                get(edit, {}, 'xforms', 'src', sxf_key),
                get(edit, {}, 'xforms', 'dst', dxf_key))
        sxf = dst['xforms'].get(sxf_key)
        dxf = dst['xforms'].get(dxf_key)
        if sxf_key == 'dup':
            sxf = dxf
            xf_edits.setdefault('weight', []).extend([0, 0])
        if dxf_key == 'dup':
            dxf = sxf
            xf_edits.setdefault('weight', []).extend([1, 0])
        blended['xforms'][bxf_key] = blend_xform(
                src['xforms'].get(sxf_key),
                dst['xforms'].get(dxf_key),
                xf_edits, opts.duration)

    if 'final_xform' in src or 'final_xform' in dst:
        blended['final_xform'] = blend_xform(src.get('final_xform'),
                dst.get('final_xform'), edit.get('final_xform'),
                opts.duration, True)

    # TODO: write 'info' section
    # TODO: palflip
    blended['type'] = 'animation'
    blended.setdefault('time', {})['duration'] = opts.duration
    return blended

def merge_edits(sv, av, bv):
    """
    Merge the values of ``av`` and ``bv`` according to the spec ``sv``.
    """
    if isinstance(sv, (dict, spectypes.Map)):
        av, bv = av or {}, bv or {}
        getsv = lambda k: sv.type if isinstance(sv, spectypes.Map) else sv[k]
        return dict([(k, merge_edits(getsv(k), av.get(k), bv.get(k)))
                     for k in set(av.keys() + bv.keys())])
    elif isinstance(sv, (spectypes.List, spectypes.Spline)):
        return (av or []) + (bv or [])
    else:
        return bv if bv is not None else av

def split_node_val(spl, val):
    if val is None:
        return spl.default, 0
    if isinstance(val, (int, float)):
        return val, 0
    return val

def tospline(spl, src, dst, edit, duration):
    sp, sv = split_node_val(spl, src)    # position, velocity
    dp, dv = split_node_val(spl, dst)

    # For variation parameters, copy missing values instead of using defaults
    if spl.var:
        if src is None:
            sp = dp
        if dst is None:
            dp = sp

    edit = dict(zip(edit[::2], edit[1::2])) if edit else {}
    e0, e1 = edit.pop(0, None), edit.pop(1, None)
    edit = list(sum([(k, v) for k, v in edit.items() if v is not None], ()))

    if spl.period:
        # Periodic extension: compute an appropriate number of loops based on
        # the angular velocities at the endpoints, and extend the destination
        # position by the appropriate number of periods.
        sign = lambda x: 1. if x >= 0 else -1.

        movement = duration * (sv + dv) / (2.0 * spl.period)
        angdiff = (float(dp - sp) / spl.period) % (sign(movement))
        dp = sp + (round(movement - angdiff) + angdiff) * spl.period

        # Endpoint override: allow adjusting the number of loops as calculated
        # above by locking to the nearest value with the same mod (i.e. the
        # nearest value which will still line up with the node)
        if e0 is not None:
            sp += round(float(e0 - sp) / spl.period) * spl.period
        if e1 is not None:
            dp += round(float(e1 - dp) / spl.period) * spl.period
    if edit or sv or dv or e0 or e1:
        return [sp, sv, dp, dv] + edit
    if sp != dp:
        return [sp, dp]
    return sp

def trace(k, cond=True):
    print k,
    return k

def merge_nodes(sp, src, dst, edit, duration):
    if isinstance(sp, dict):
        src, dst, edit = [x or {} for x in src, dst, edit]
        return dict([(k, merge_nodes(sp[k], src.get(k),
                                     dst.get(k), edit.get(k), duration))
            for k in set(src.keys() + dst.keys() + edit.keys()) if k in sp])
    elif isinstance(sp, spectypes.Spline):
        return tospline(sp, src, dst, edit, duration)
    elif isinstance(sp, spectypes.List):
        if isinstance(sp.type, spectypes.Palette):
            if src is not None: src = [[0] + src]
            if dst is not None: dst = [[1] + dst]
        return (src or []) + (dst or []) + (edit or [])
    else:
        return edit if edit is not None else dst if dst is not None else src

def blend_xform(sxf, dxf, edits, duration, isfinal=False):
    if sxf is None:
        sxf = padding_xform(dxf, isfinal)
    if dxf is None:
        dxf = padding_xform(sxf, isfinal)
    return merge_nodes(specs.xform, sxf, dxf, edits, duration)

# If xin contains any of these, use the inverse identity
hole_variations = ('spherical ngon julian juliascope polar '
                   'wedge_sph wedge_julia bipolar').split()

# These variations are identity functions at their default values
ident_variations = ('rectangles fan2 blob perspective super_shape').split()

def padding_xform(xf, isfinal):
    vars = {}
    xout = {'variations': vars, 'pre_affine': {'angle': 45}}
    if isfinal:
        xout.update(weight=0, color_speed=0)
    if get(xf, 45, 'pre_affine', 'spread') > 90:
        xout['pre_affine'] = {'angle': 135, 'spread': 135}
    if get(xf, 45, 'post_affine', 'spread') > 90:
        xout['post_affine'] = {'angle': 135, 'spread': 135}

    for k in xf.get('variations', {}):
        if k in hole_variations:
            # Attempt to correct for some known-ugly variations.
            xout['pre_affine']['angle'] += 180
            vars['linear'] = dict(weight=-1)
            return xout
        if k in ident_variations:
            # Try to use non-linear variations whenever we can
            vars[k] = dict([(vk, vv.default)
                            for vk, vv in variations.var_params[k].items()])

    if vars:
        n = float(len(vars))
        for k in vars:
            vars[k]['weight'] = 1 / n
    else:
        vars['linear'] = dict(weight=1)

    return xout

def halfhearted_human_sort_key(key):
    try:
        return int(key)
    except ValueError:
        return key

def sort_xforms(sxfs, dxfs, sortmethod, explicit=[]):
    # Walk through the explicit pairs, popping previous matches from the
    # forward (src=>dst) and reverse (dst=>src) maps
    fwd, rev = {}, {}
    for sx, dx in explicit:
        if sx not in ("pad", "dup") and sx in fwd:
            rev.pop(fwd.pop(sx, None), None)
        if dx not in ("pad", "dup") and dx in rev:
            fwd.pop(rev.pop(dx, None), None)
        fwd[sx] = dx
        rev[dx] = sx

    for sd in sorted(fwd.items()):
        yield sd

    # Classify the remaining xforms. Currently we classify based on whether
    # the pre- and post-affine transforms are flipped
    scl, dcl = {}, {}
    for (cl, xfs, exp) in [(scl, sxfs, fwd), (dcl, dxfs, rev)]:
        for k, v in xfs.items():
            if k in exp: continue
            xcl = (get(v, 45, 'pre_affine', 'spread') > 90,
                   get(v, 45, 'post_affine', 'spread') > 90)
            cl.setdefault(xcl, []).append(k)

    def sort(keys, dct, snd=False):
        if sortmethod in ('weight', 'weightflip'):
            sortf = lambda k: dct[k].get('weight', 0)
        elif sortmethod == 'color':
            sortf = lambda k: dct[k].get('color', 0)
        else:
            # 'natural' key-based sort
            sortf = halfhearted_human_sort_key
        return sorted(keys, key=sortf)

    for cl in set(scl.keys() + dcl.keys()):
        ssort = sort(scl.get(cl, []), sxfs)
        dsort = sort(dcl.get(cl, []), dxfs)
        if sortmethod == 'weightflip':
            dsort = reversed(dsort)
        for sd in izip_longest(ssort, dsort):
            yield sd

def checkpalflip(gnm):
    if 'final' in gnm['xforms']:
        f = gnm['xforms']['final']
        fcv, fcsp = f['color'], f['color_speed']
    else:
        fcv, fcsp = SplEval(0), SplEval(0)
    sansfinal = [v for k, v in gnm['xforms'].items() if k != 'final']

    lc, rc = [np.array([v['color'](t) * (1 - fcsp(t)) + fcv(t) * fcsp(t)
               for v in sansfinal]) for t in (0, 1)]
    rcrv = 1 - rc
    # TODO: use spline integration instead of L2
    dens = np.array([np.hypot(v['weight'](0), v['weight'](1))
                     for v in sansfinal])
    return np.sum(np.abs(dens * (rc - lc))) > np.sum(np.abs(dens * (rcrv - lc)))

def palflip(gnm):
    for v in gnm['xforms'].values():
        c = v['color']
        v['color'] = SplEval([0, c(0), 1, 1 - c(1)], c(0, 1), -c(1, 1))
    pal = genome.palette_decode(gnm['palettes'][1])
    gnm['palettes'][1] = genome.palette_encode(np.flipud(pal))

if __name__ == "__main__":
    import sys, json
    a, b, c = [json.load(open(f+'.json')) for f in 'abc']
    print json_encode(blend(a, b, c))
