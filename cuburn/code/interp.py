from collections import OrderedDict
from itertools import cycle
import numpy as np

import tempita
from util import HunkOCode, Template, BaseCode, assemble_code

class GenomePackerName(str):
    """Class to indicate that a property is precalculated on the device"""
    pass

class GenomePackerView(object):
    """
    Obtain accessors in generated code.

    Call ``GenomePacker.view(ptr_name, wrapped_obj, prefix)`` to generate a
    view, where ``ptr_name`` is the name of the CUDA pointer which holds the
    interpolated values, ``wrapped_obj`` is the base Genome instance which
    will be uploaded to the device for interpolating from, and ``prefix`` is
    the prefix that will be assigned to property accessors from this object.

    Accessing a property on the object synthesizes an accessor for use in your
    code and an interpolator for use in generating that code. This conversion
    is done when the property is coerced into a string by the templating
    mechanism, so you can easily nest objects by saying, for instance,
    {{pcp.camera.rotation}} from within templated code. The accessed property
    must be a SplEval object, or a precalculated value (see
    ``GenomePackerPrecalc``).

    Index operations are converted to property accesses as well, so that you
    don't have to make a mess with 'getattr' in your code: {{pcp.xforms[x]}}
    works just fine. This means, however, that no arrays can be packed
    directly; they must be converted to have string-based keys first, and
    any loops must be unrolled in your code.
    """
    def __init__(self, packer, ptr_name, wrapped, prefix=()):
        self.packer = packer
        self.ptr_name = ptr_name
        self.wrapped = wrapped
        self.prefix = prefix
    def __getattr__(self, name):
        w = getattr(self.wrapped, name)
        return type(self)(self.packer, self.ptr_name, w, self.prefix+(name,))
    # As with the Genome class, we're all-dict, no-array here
    __getitem__ = lambda s, n: getattr(s, str(n))

    def __str__(self):
        """
        Returns the packed name in a format suitable for embedding directly
        into device code.
        """
        # So evil. When the template calls __str__ to format the output, we
        # allocate things. This makes for neater embedded code, which is where
        # the real complexity lies, but it also means printf() debugging when
        # templating will screw with the allocation tables!
        if not isinstance(self.wrapped, GenomePackerName):
            self.packer._require(self.prefix)
        # TODO: verify namespace stomping, etc
        return '%s.%s' % (self.ptr_name, '_'.join(self.prefix))

    def _precalc(self):
        """Create a GenomePackerPrecalc object. See that class for details."""
        return GenomePackerPrecalc(self.packer, self.ptr_name,
                                   self.wrapped, self.prefix)

class GenomePackerPrecalc(GenomePackerView):
    """
    Insert precalculated values into the packed genome.

    Create a Precalc object by calling a view object's _precalc() method. The
    returned object shares the same referent dict as the view object, but
    instead of returning view accessors for use in the body of your code,
    accessing a property synthesizes a call to an interpolation function for
    use within the precalculating function. Use this in your precalculated
    code to obtain values from a genome.

    Once you've obtained the needed parameters and performed the
    precalculation, write them to the genome object with a call to the '_set'
    method in your precalc template. This method generates a new accessor to
    which you can assign a value in your precalculation function. It also
    makes this accessor available for use on the original packer object from
    within your main function.

    Finally, call the '_code' method on the precalc object with your generated
    precalculation code to add it to the precalculation function. The code
    will be wrapped in a C block to prevent namespace leakage, so that
    multiple invocations of the precalculation code on different code blocks
    can be performed.

    Example:

        def do_precalc(px):
            pcam = px._precalc()
            pcam._code(Template('''
                {{pcam._set('prop_sin')}} = sin({{pcam.prop}});
                ''').substitute(pcam=pcam))

        def gen_code(px):
            return Template('''
                {{do_precalc(px)}}
                printf("The sin of %g is %g.", {{px.prop}}, {{px.prop_sin}});
                ''').substitute(px=px)
    """
    def __init__(self, packer, ptr_name, wrapped, prefix):
        super(GenomePackerPrecalc, self).__init__(packer, 'out', wrapped, prefix)
    def __str__(self):
        return self.packer._require_pre(self.prefix)
    def _set(self, name):
        fullname = self.prefix + (name,)
        self.packer._pre_alloc(fullname)
        # This just modifies the underlying object, because I'm too lazy right
        # now to ghost the namespace
        self.wrapped[name] = GenomePackerName('_'.join(fullname))
        return '%s->%s' % (self.ptr_name, self.wrapped[name])
    def _code(self, code):
        self.packer.precalc_code.append(code)

class GenomePacker(HunkOCode):
    """
    Packs a genome for use in iteration.
    """

    # 2^search_rounds is the maximum number of control points, including
    # endpoints, that can be used in a single genome. It should be okay to
    # change this number without touching other code, but 32 samples fits
    # nicely on a single cache line.
    search_rounds = 5

    def __init__(self, tname):
        """
        Create a new DataPacker.

        ``tname`` is the name of the structure typedef that will be emitted
        via this object's ``decls`` property.
        """
        self.tname = tname
        # We could do this in the order that things are requested, but we want
        # to be able to treat the direct stuff as a list so this function
        # doesn't unroll any more than it has to. So we separate things into
        # direct requests, and those that need precalculation.
        # Values of OrderedDict are unused; basically, it's just OrderedSet.
        self.packed_direct = OrderedDict()
        self.genome_precalc = OrderedDict()
        self.packed_precalc = OrderedDict()
        self.precalc_code = []

        self.ns = {}

        self._len = None
        self.decls = None
        self.defs = None

        self.packed = None
        self.genome = None

    def __len__(self):
        assert self._len is not None, 'len() called before finalize()'
        return self._len

    def view(self, ptr_name, wrapped_obj, prefix):
        """Create a DataPacker view. See DataPackerView class for details."""
        self.ns[prefix] = wrapped_obj
        return GenomePackerView(self, ptr_name, wrapped_obj, (prefix,))

    def _require(self, name):
        """
        Called to indicate that the named parameter from the original genome
        must be available during interpolation.
        """
        self.packed_direct[name] = None

    def _require_pre(self, name):
        i = len(self.genome_precalc) << self.search_rounds
        self.genome_precalc[name] = None
        return 'catmull_rom(&times[%d], &knots[%d], time)' % (i, i)

    def _pre_alloc(self, name):
        self.packed_precalc[name] = None

    def finalize(self):
        """
        Create the code to render this genome.
        """
        # At the risk of packing a few things more than once, we don't
        # uniquify the overall precalc order, sparing us the need to implement
        # recursive code generation
        self.packed = self.packed_direct.keys() + self.packed_precalc.keys()
        self.genome = self.packed_direct.keys() + self.genome_precalc.keys()

        self._len = len(self.packed)

        self.decls = self._decls.substitute(
                packed=self.packed, tname=self.tname,
                search_rounds=self.search_rounds)
        self.defs = self._defs.substitute(
                packed_direct=self.packed_direct, tname=self.tname,
                precalc_code=self.precalc_code,
                search_rounds=self.search_rounds)


    def pack(self):
        """
        Return a packed copy of the genome ready for uploading to the GPU as a
        3D NDArray, with the first element being the times and the second
        being the values.
        """
        width = 1 << self.search_rounds
        out = np.empty((2, len(self.genome), width), dtype=np.float32)
        # Ensure that unused values at the top are always big (must be >2.0)
        out[0].fill(1e9)

        for idx, gname in enumerate(self.genome):
            attr = self.ns[gname[0]]
            for g in gname[1:]:
                attr = getattr(attr, g)
            out[0][idx][:len(attr.knots[0])] = attr.knots[0]
            out[1][idx][:len(attr.knots[1])] = attr.knots[1]
        return out

    _defs = Template(r"""

__global__
void interp_{{tname}}(
        {{tname}}* out,
        const float *times, const float *knots,
        float tstart, float tstep, int maxid
) {
    int id = gtid();
    if (id >= maxid) return;
    out = &out[id];
    float time = tstart + id * tstep;

    float *outf = reinterpret_cast<float*>(out);

    // TODO: unroll pragma?
    for (int i = 0; i < {{len(packed_direct)}}; i++) {
        int j = i << {{search_rounds}};
        outf[i] = catmull_rom(&times[j], &knots[j], time);
    }

    // Advance 'times' and 'knots' to the purely generated sections, so that
    // the pregenerated statements emitted by _require_pre are correct.
    times = &times[{{len(packed_direct)<<search_rounds}}];
    knots = &knots[{{len(packed_direct)<<search_rounds}}];

    {{for hunk in precalc_code}}
    if (1) {
    {{hunk}}
    }
    {{endfor}}
}

""")

    _decls = Template(r"""
typedef struct {
{{for name in packed}}
    float   {{'_'.join(name)}};
{{endfor}}
} {{tname}};

/* Search through the fixed-size list 'hay' to find the rightmost index which
 * contains a value strictly smaller than the input 'needle'. The crazy
 * bitwise search is just for my own amusement.
 */
__device__
int bitwise_binsearch(const float *hay, float needle) {
    int lo = 0;

    // TODO: improve efficiency on 64-bit arches
    {{for i in range(search_rounds-1, -1, -1)}}
    if (needle > hay[lo + {{1 << i}}])
        lo += {{1 << i}};
    {{endfor}}
    return lo;
}

__device__ __noinline__
float catmull_rom(const float *times, const float *knots, float t) {
    int idx = bitwise_binsearch(times, t);

    // The left bias of the search means that we never have to worry about
    // overshooting unless the genome is corrupted
    idx = max(idx, 1);

    float t1 = times[idx], t2 = times[idx+1] - t1;
    float rt2 = 1.0f / t2;
    float t0 = (times[idx-1] - t1) * rt2, t3 = (times[idx+2] - t1) * rt2;
    t = (t - t1) * rt2;

    // Now t1 is effectively 0 and t2 is 1

    float k0 = knots[idx-1], k1 = knots[idx],
          k2 = knots[idx+1], k3 = knots[idx+2];

    float m1 = (k2 - k0) / (1.0f - t0),
          m2 = (k3 - k1) / (t3);

    float tt = t * t, ttt = tt * t;

    return m1 * (      ttt - 2.0f*tt + t)
         + k1 * ( 2.0f*ttt - 3.0f*tt + 1)
         + m2 * (      ttt -      tt)
         + k2 * (-2.0f*ttt + 3.0f*tt);
}

__global__
void test_cr(const float *times, const float *knots, const float *t, float *r) {
    int i = threadIdx.x + blockDim.x * blockIdx.x;
    r[i] = catmull_rom(times, knots, t[i]);
}
""")

class Palette(HunkOCode):
    # The JPEG YUV full-range matrix, without bias into the positve regime.
    # This assumes input color space is CIERGB D65, encoded with gamma 2.2.
    # Note that some interpolated colors may exceed the sRGB and YUV gamuts.
    YUV = np.matrix([[ 0.299,        0.587,      0.114],
                     [-0.168736,    -0.331264,   0.5],
                     [ 0.5,         -0.418688,  -0.081312]])

    def __init__(self, interp_mode):
        assert interp_mode in self.modes
        self.mode = interp_mode
        self.defs = self._defs.substitute(mode=interp_mode)

    def prepare(self, palettes):
        """
        Produce palettes suitable for uploading to the device. Returns an
        array of palettes in the same size and shape as the input.

        This function will never modify its argument, but may return it
        unmodified for certain interpolation modes.
        """
        if self.mode == 'yuvpolar':
            ys, uvrs, uvts, alphas = zip(*map(self.rgbtoyuvpolar, palettes))
            # Center all medians as closely to 0 as possible
            means = np.mean(uvts, axis=1)
            newmeans = (means + np.pi) % (2 * np.pi) - np.pi
            uvts = (newmeans - means).reshape((-1, 1)) + uvts
            zipped = zip(ys, uvrs, uvts, alphas)
            return np.array(zipped, dtype='f4').transpose((0, 2, 1))
        return palettes

    @classmethod
    def rgbtoyuvpolar(cls, pal):
        # TODO: premultiply alpha or some nonsense like that?
        y, u, v = np.array(cls.YUV * pal.T[:3])
        uvr = np.hypot(u, v)
        uvt = np.unwrap(np.arctan2(v, u))
        return y, uvr, uvt, pal.T[3]

    @classmethod
    def yuvpolartorgb(cls, y, uvr, uvt, a):
        u = uvr * np.cos(uvt)
        v = uvr * np.sin(uvt)
        r, g, b = np.array(cls.YUV.I * np.array([y, u, v]))
        # Ensure Fortran order so that the memory gets laid out correctly
        return np.array([r, g, b, a], order='F').T

    modes = ['hsv', 'yuv', 'yuvpolar']
    decls = "surface<void, cudaSurfaceType2D> flatpal;\n"

    _defs = Template(r"""
__device__
float4 interp_color(const float *times, const float4 *sources, float time) {
    int idx = fmaxf(bitwise_binsearch(times, time) + 1, 1);
    float lf = (times[idx] - time) / (times[idx] - times[idx-1]);
    float rf = 1.0f - lf;

    float4 left  = sources[blockDim.x * (idx - 1) + threadIdx.x];
    float4 right = sources[blockDim.x * (idx)     + threadIdx.x];
    float3 yuv;

    float3 l3 = make_float3(left.x, left.y, left.z);
    float3 r3 = make_float3(right.x, right.y, right.z);

{{if mode == 'hsv'}}
    float3 lhsv = rgb2hsv(l3);
    float3 rhsv = rgb2hsv(r3);

    if (fabs(lhsv.x - rhsv.x) > 3.0f)
        if (lhsv.x < rhsv.x)
            lhsv.x += 6.0f;
        else
            rhsv.x += 6.0f;

    float3 hsv;
    hsv.x = lhsv.x * lf + rhsv.x * rf;
    hsv.y = lhsv.y * lf + rhsv.y * rf;
    hsv.z = lhsv.z * lf + rhsv.z * rf;

    if (hsv.x > 6.0f)
        hsv.x -= 6.0f;
    if (hsv.x < 0.0f)
        hsv.x += 6.0f;

    yuv = rgb2yuv(hsv2rgb(hsv));
{{elif mode.startswith('yuv')}}
    {{if mode == 'yuv'}}
    float3 lyuv = rgb2yuv(l3);
    float3 ryuv = rgb2yuv(r3);
    yuv.x = lyuv.x * lf + ryuv.x * rf;
    yuv.y = lyuv.y * lf + ryuv.y * rf;
    yuv.z = lyuv.z * lf + ryuv.z * rf;
    {{elif mode == 'yuvpolar'}}
    yuv.x = l3.x * lf + r3.x * rf;
    float radius = l3.y * lf + r3.y * rf;
    float angle  = l3.z * lf + r3.z * rf;
    yuv.y = radius * cosf(angle);
    yuv.z = radius * sinf(angle);
    {{endif}}
{{endif}}

    yuv.y += 0.5f;
    yuv.z += 0.5f;

    return make_float4(yuv.x, yuv.y, yuv.z, left.w * lf + right.w * rf);
}

__global__
void interp_palette(uchar4 *outs,
        const float *times, const float4 *sources,
        float tstart, float tstep) {
    float time = tstart + blockIdx.x * tstep;
    float4 yuva = interp_color(times, sources, time);

    uchar4 out;
    out.x = yuva.x * 255.0f;
    out.y = yuva.y * 255.0f;
    out.z = yuva.z * 255.0f;
    out.w = yuva.w * 255.0f;
    outs[blockDim.x * blockIdx.x + threadIdx.x] = out;
}

__global__
void interp_palette_flat(mwc_st *rctxs,
        const float *times, const float4 *sources,
        float tstart, float tstep) {

    int gid = blockIdx.x * blockDim.x + threadIdx.x;
    mwc_st rctx = rctxs[gid];

    float time = tstart + blockIdx.x * tstep;
    float4 yuva = interp_color(times, sources, time);

    // TODO: pack Y at full precision, UV at quarter
    uint2 out;

    uint32_t y = min(255, (uint32_t) (yuva.x * 255.0f + 0.49f * mwc_next_11(rctx)));
    uint32_t u = min(255, (uint32_t) (yuva.y * 255.0f + 0.49f * mwc_next_11(rctx)));
    uint32_t v = min(255, (uint32_t) (yuva.z * 255.0f + 0.49f * mwc_next_11(rctx)));
    out.y = (1 << 22) | (y << 4);
    out.x = (u << 18) | v;
    surf2Dwrite(out, flatpal, 8 * threadIdx.x, blockIdx.x);
    rctxs[gid] = rctx;
}
""")

if __name__ == "__main__":
    import pycuda.driver as cuda
    from pycuda.compiler import SourceModule
    import pycuda.autoinit
    from cuburn.genome import SplEval

    gp = GenomePacker("unused")
    gp.finalize()
    mod = SourceModule(assemble_code(BaseCode, gp))
    times = np.sort(np.concatenate(([-2.0, 0.0, 1.0, 3.0], np.random.rand(12))))
    knots = np.random.randn(16)

    print times
    print knots

    evaltimes = np.float32(np.linspace(0, 1, 1024))
    sp = SplEval([x for k in zip(times, knots) for x in k])
    vals = np.array([sp(t) for t in evaltimes], dtype=np.float32)

    dtimes = np.empty((32,), dtype=np.float32)
    dtimes.fill(1e9)
    dtimes[:16] = times
    dknots = np.zeros_like(dtimes)
    dknots[:16] = knots

    dvals = np.empty_like(vals)
    mod.get_function("test_cr")(cuda.In(dtimes), cuda.In(dknots),
            cuda.In(evaltimes), cuda.Out(dvals), block=(1024, 1, 1))
    for t, v, d in zip(evaltimes, vals, dvals):
        print '%6f %8g %8g' % (t, v, d)
