
from cuburn.code.util import *

class ColorClip(HunkOCode):
    def __init__(self, info):
        self.defs = self.defs_tmpl.substitute(info=info)

    defs_tmpl = Template('''
__global__
void colorclip(float4 *pixbuf, float gamma, float vibrancy, float highpow,
               float linrange, float lingam, float3 bkgd, int fbsize) {
    int i = gtid();
    if (i >= fbsize) return;

    float4 pix = pixbuf[i];

    if (pix.w <= 0) {
        pixbuf[i] = make_float4(bkgd.x, bkgd.y, bkgd.z, 0.0f);
        return;
    }

    float4 opix = pix;

    float alpha = powf(pix.w, gamma);
    if (pix.w < linrange) {
        float frac = pix.w / linrange;
        alpha = (1.0f - frac) * pix.w * lingam + frac * alpha;
    }

    float ls = vibrancy * alpha / pix.w;

    alpha = fminf(1.0f, fmaxf(0.0f, alpha));

    float maxc = fmaxf(pix.x, fmaxf(pix.y, pix.z));
    float maxa = maxc * ls;
    float newls = 1.0f / maxc;

    if (maxa > 1.0f && highpow >= 0.0f) {
        float lsratio = powf(newls / ls, highpow);
        pix.x *= newls;
        pix.y *= newls;
        pix.z *= newls;

        // Reduce saturation (according to the HSV model) by proportionally
        // increasing the values of the other colors.
        pix.x = maxc - (maxc - pix.x) * lsratio;
        pix.y = maxc - (maxc - pix.y) * lsratio;
        pix.z = maxc - (maxc - pix.z) * lsratio;
    } else {
        float adjhlp = -highpow;
        if (adjhlp > 1.0f || maxa <= 1.0f) adjhlp = 1.0f;
        if (maxc > 0.0f) {
            float adj = ((1.0f - adjhlp) * newls + adjhlp * ls);
            pix.x *= adj;
            pix.y *= adj;
            pix.z *= adj;
        }
    }

    pix.x += (1.0f - vibrancy) * powf(opix.x, gamma);
    pix.y += (1.0f - vibrancy) * powf(opix.y, gamma);
    pix.z += (1.0f - vibrancy) * powf(opix.z, gamma);

    {{if info.alpha_output_channel}}
    float 1_alpha = 1 / alpha;
    pix.x *= 1_alpha;
    pix.y *= 1_alpha;
    pix.z *= 1_alpha;
    {{else}}
    pix.x += (1.0f - alpha) * bkgd.x;
    pix.y += (1.0f - alpha) * bkgd.y;
    pix.z += (1.0f - alpha) * bkgd.z;
    {{endif}}
    pix.w = alpha;

    // Clamp values. I think this is superfluous, but I'm not certain.
    pix.x = fminf(1.0f, pix.x);
    pix.y = fminf(1.0f, pix.y);
    pix.z = fminf(1.0f, pix.z);

    pixbuf[i] = pix;
}
''')

class DensityEst(HunkOCode):
    """
    NOTE: for now, this *must* be invoked with a block size of (32,32,1), and
    a grid size of (W/32,1). At least 21 pixel gutters are required, and the
    stride and height probably need to be multiples of 32.
    """

    def __init__(self, info):
        self.info = info

    headers = "#include<math_constants.h>\n"
    @property
    def defs(self):
        return self.defs_tmpl.substitute(info=self.info)

    defs_tmpl = Template('''
#define W 21        // Filter width (regardless of standard deviation chosen)
#define W2 10       // Half of filter width, rounded down
#define FW 52       // Width of local result storage (NW+W2+W2)
#define FW2 (FW*FW)

__shared__ float de_r[FW2], de_g[FW2], de_b[FW2], de_a[FW2];

__device__ void de_add(int ibase, int ii, int jj, float4 scaled) {
    int idx = ibase + FW * ii + jj;
    atomicAdd(de_r+idx, scaled.x);
    atomicAdd(de_g+idx, scaled.y);
    atomicAdd(de_b+idx, scaled.z);
    atomicAdd(de_a+idx, scaled.w);
}

__global__
void logscale(float4 *pixbuf, float4 *outbuf, float k1, float k2) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    float den;
    float4 pix = pixbuf[i];
    read_pix(pix, den);

    float ls = fmaxf(0, k1 * logf(1.0f + pix.w * k2) / pix.w);
    pix.x *= ls;
    pix.y *= ls;
    pix.z *= ls;
    pix.w *= ls;

    outbuf[i] = pix;
}


// See helpers/filt_err.py for source of these values.
#define MIN_SD 0.23299530f
#define MAX_SD 4.33333333f

__global__
void density_est(float4 *pixbuf, float4 *outbuf,
                 float est_sd, float neg_est_curve, float est_min,
                 float k1, float k2) {
    for (int i = threadIdx.x + 32*threadIdx.y; i < FW2; i += 32)
        de_r[i] = de_g[i] = de_b[i] = de_a[i] = 0.0f;
    __syncthreads();

    for (int imrow = threadIdx.y + W2; imrow < ({{info.acc_height}} - W2); imrow += 32)
    {
        int idx = {{info.acc_stride}} * imrow +
                + blockIdx.x * 32 + threadIdx.x + W2;

        float4 in = pixbuf[idx];
        float den;
        read_pix(in, den);

        if (in.w > 0 && den > 0) {

            // Compute a fast and dirty approximation of a "gradient" using
            // a [[-1 0 0][0 0 0][0 0 1]]/4 matrix (and its reflection)
            // for angled edge detection, and limit blurring in those regions
            // to both provide a bit of smoothing and prevent irregular
            // bleed-out along gradients close to the image grid.
            //
            // For such a simple operator - particularly one whose entire
            // justification is "it feels right" - it gives very good results
            // over a wide range of images without any per-flame
            // parameter tuning. In rare cases, color clamping and extreme
            // palette changes can cause aliasing to reappear after the DE
            // step; the only way to fix that is through a color-buffer AA
            // like MLAA.
            float *dens = reinterpret_cast<float*>(pixbuf);
            int didx = idx * 4 + 3;
            float x = 0.25f * ( dens[didx+{{info.acc_stride*4}}+4]
                              - dens[didx-{{info.acc_stride*4}}-4] );
            float y = 0.25f * ( dens[didx+{{info.acc_stride*4}}-4]
                              - dens[didx-{{info.acc_stride*4}}+4] );
            float diag_mag = sqrtf(x*x + y*y);

            float ls = k1 * logf(1.0f + in.w * k2) / in.w;
            in.x *= ls;
            in.y *= ls;
            in.z *= ls;
            in.w *= ls;

            // Base index of destination for writes
            int si = (threadIdx.y + W2) * FW + threadIdx.x + W2;

            // Calculate standard deviation of Gaussian kernel. The base SD is
            // then scaled in inverse proportion to the density of the point
            // being scaled.
            float sd = est_sd * powf(den+1.0f, neg_est_curve);
            // And for the gradient...
            float diag_sd = est_sd * powf(diag_mag+1.0f, neg_est_curve);

            // If the gradient SD is smaller than the minimum SD, we're probably
            // on a strong edge; blur with a standard deviation around 1px.
            if (diag_sd < fmaxf(est_min, MIN_SD)) sd = 0.333333f;

            // Clamp the final standard deviation.
            sd = fminf(MAX_SD, fmaxf(sd, est_min));

            // Below a certain threshold, only one coeffecient would be
            // retained anyway; we hop right to it.
            if (sd <= MIN_SD) {
                de_add(si, 0,  0, in);
            } else {
                // These polynomials approximates the sum of the filters
                // with the clamping logic used here. See helpers/filt_err.py.
                float filtsum;
                if (sd < 0.75f) {
                    filtsum = -352.25061035f;
                    filtsum = filtsum * sd +    1117.09680176f;
                    filtsum = filtsum * sd +   -1372.48864746f;
                    filtsum = filtsum * sd +     779.15478516f;
                    filtsum = filtsum * sd +    -164.04229736f;
                    filtsum = filtsum * sd +     -12.04892635f;
                    filtsum = filtsum * sd +       9.04126644f;
                    filtsum = filtsum * sd +       0.10304667f;
                } else {
                    filtsum = 0.01162011f;
                    filtsum = filtsum * sd +      -0.21552004f;
                    filtsum = filtsum * sd +       1.66545594f;
                    filtsum = filtsum * sd +      -7.00809765f;
                    filtsum = filtsum * sd +      17.55487633f;
                    filtsum = filtsum * sd +     -26.80626106f;
                    filtsum = filtsum * sd +      30.61903954f;
                    filtsum = filtsum * sd +     -12.00870514f;
                    filtsum = filtsum * sd +       2.46708894f;
                }
                float filtscale = 1.0f / filtsum;

                // The reciprocal SD scaling coeffecient in the Gaussian
                // exponent: exp(-x^2/(2*sd^2)) = exp2f(x^2*rsd)
                float rsd = -0.5f * CUDART_L2E_F / (sd * sd);

                for (int jj = 0; jj <= W2; jj++) {
                    float jj2f = jj;
                    jj2f *= jj2f;

                    float iif = 0;
                    for (int ii = 0; ii <= jj; ii++) {

                        float coeff = exp2f((jj2f + iif * iif) * rsd)
                                    * filtscale;
                        if (coeff < 0.0001f) break;
                        iif += 1;

                        float4 scaled;
                        scaled.x = in.x * coeff;
                        scaled.y = in.y * coeff;
                        scaled.z = in.z * coeff;
                        scaled.w = in.w * coeff;

                        de_add(si,  ii,  jj, scaled);
                        if (jj == 0) continue;
                        de_add(si,  ii, -jj, scaled);
                        if (ii != 0) {
                            de_add(si, -ii,  jj, scaled);
                            de_add(si, -ii, -jj, scaled);
                            if (ii == jj) continue;
                        }
                        de_add(si,  jj,  ii, scaled);
                        de_add(si, -jj,  ii, scaled);

                        if (ii == 0) continue;
                        de_add(si, -jj, -ii, scaled);
                        de_add(si,  jj, -ii, scaled);

                    }
                }
            }
        }

        __syncthreads();
        // TODO: could coalesce this, but what a pain
        for (int i = threadIdx.x; i < FW; i += 32) {
            idx = {{info.acc_stride}} * imrow + blockIdx.x * 32 + i + W2;
            int si = threadIdx.y * FW + i;
            float *out = reinterpret_cast<float*>(&outbuf[idx]);
            atomicAdd(out,   de_r[si]);
            atomicAdd(out+1, de_g[si]);
            atomicAdd(out+2, de_b[si]);
            atomicAdd(out+3, de_a[si]);
        }

        __syncthreads();
        // TODO: shift instead of copying
        int tid = threadIdx.y * 32 + threadIdx.x;
        for (int i = tid; i < FW*(W2+W2); i += 512) {
            de_r[i] = de_r[i+FW*32];
            de_g[i] = de_g[i+FW*32];
            de_b[i] = de_b[i+FW*32];
            de_a[i] = de_a[i+FW*32];
        }

        __syncthreads();
        for (int i = tid + FW*(W2+W2); i < FW2; i += 512) {
            de_r[i] = 0.0f;
            de_g[i] = 0.0f;
            de_b[i] = 0.0f;
            de_a[i] = 0.0f;
        }
        __syncthreads();
    }
}

''')

    def invoke(self, mod, cp, abufd, obufd, stream=None):
        # TODO: add no-est version
        # TODO: come up with a general way to average these parameters

        k1 = np.float32(cp.color.brightness * 268 / 256)
        # Old definition of area is (w*h/(s*s)). Since new scale 'ns' is now
        # s/w, new definition is (w*h/(s*s*w*w)) = (h/(s*s*w))
        area = self.info.height / (cp.camera.scale ** 2 * self.info.width)
        k2 = np.float32(1 / (area * self.info.density ))

        if cp.de.radius == 0:
            nbins = self.info.acc_height * self.info.acc_stride
            fun = mod.get_function("logscale")
            t = fun(abufd, obufd, k1, k2,
                    block=(512, 1, 1), grid=(nbins/512, 1), stream=stream)
        else:
            # flam3_gaussian_filter() uses an implicit standard deviation of
            # 0.5, but the DE filters scale filter distance by the default
            # spatial support factor of 1.5, so the effective base SD is
            # (0.5/1.5)=1/3.
            est_sd = np.float32(cp.de.radius / 3.)
            neg_est_curve = np.float32(-cp.de.curve)
            est_min = np.float32(cp.de.minimum / 3.)
            fun = mod.get_function("density_est")
            fun(abufd, obufd, est_sd, neg_est_curve, est_min, k1, k2,
                block=(32, 32, 1), grid=(self.info.acc_width/32, 1),
                stream=stream)

