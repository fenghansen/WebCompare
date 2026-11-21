/* Exact AX little-endian RAW10 unpacking. No SDK or Python headers required.
 * Each five-byte group stores four consecutive ten-bit samples. Stride is
 * measured in pixels, includes row padding, and must be divisible by four.
 * Integer-only arithmetic preserves all ten bits; no fast-math is needed.
 */
#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#define WC_EXPORT __declspec(dllexport)
#else
#define WC_EXPORT
#endif

WC_EXPORT int wc_raw10_abi(void) { return 1; }

WC_EXPORT int wc_unpack_ax10(const uint8_t *src, size_t bytes, uint16_t *dst,
                            size_t count, unsigned width, unsigned height,
                            unsigned stride) {
    size_t row_bytes;
    if (!src || !dst || !width || !height || stride < width || (stride & 3))
        return -1;
    if ((size_t)(stride / 4) > SIZE_MAX / 5) return -1;
    row_bytes = (size_t)(stride / 4) * 5;
    if ((size_t)height > SIZE_MAX / row_bytes ||
        (size_t)height > SIZE_MAX / width || bytes != row_bytes * height ||
        count != (size_t)width * height) return -1;
    for (unsigned y = 0; y < height; ++y) {
        const uint8_t *p = src + (size_t)y * row_bytes;
        uint16_t *q = dst + (size_t)y * width;
        unsigned groups = width / 4;
        for (unsigned g = 0; g < groups; ++g, p += 5, q += 4) {
            q[0] = (uint16_t)(p[0] | ((p[1] & 3u) << 8));
            q[1] = (uint16_t)((p[1] >> 2) | ((p[2] & 15u) << 6));
            q[2] = (uint16_t)((p[2] >> 4) | ((p[3] & 63u) << 4));
            q[3] = (uint16_t)((p[3] >> 6) | ((uint16_t)p[4] << 2));
        }
        unsigned tail = width & 3;
        if (tail) q[0] = (uint16_t)(p[0] | ((p[1] & 3u) << 8));
        if (tail > 1) q[1] = (uint16_t)((p[1] >> 2) | ((p[2] & 15u) << 6));
        if (tail > 2) q[2] = (uint16_t)((p[2] >> 4) | ((p[3] & 63u) << 4));
    }
    return 0;
}

/* Quantization thresholds are derived and checked against the NumPy float32
 * transfer by the caller. Narrow the binary search with 256 linear bins, so
 * no per-pixel pow() or approximate gamma lookup is needed. */
static uint8_t quantize(float value, const float *threshold, const uint16_t *bins) {
    if (value <= 0) return 0;
    if (value >= 1) return 255;
    unsigned bucket = (unsigned)(value * 256.0f);
    unsigned lo = bins[bucket], hi = bins[bucket + 1];
    while (lo < hi) {
        unsigned mid = (lo + hi) / 2;
        if (value >= threshold[mid]) lo = mid + 1;
        else hi = mid;
    }
    return (uint8_t)lo;
}

WC_EXPORT int wc_demosaic_rgb8(const float *m, uint8_t *out, unsigned width,
                             unsigned height, const int *cfa, const float *wb4,
                             const float *threshold, const uint16_t *bins) {
    if (!m || !out || !cfa || !wb4 || !threshold || !bins || width < 2 || height < 2)
        return -1;
    for (unsigned y = 0; y < height; ++y) {
        unsigned yn = y ? y - 1 : 1, ys = y + 1 < height ? y + 1 : height - 2;
        const float *north = m + (size_t)yn * width;
        const float *row = m + (size_t)y * width;
        const float *south = m + (size_t)ys * width;
        for (unsigned x = 0; x < width; ++x) {
            unsigned xw = x ? x - 1 : 1, xe = x + 1 < width ? x + 1 : width - 2;
            unsigned phase = ((y & 1) << 1) | (x & 1);
            int color = cfa[phase];
            float channels[3];
            channels[color] = row[x] * 4.0f;
            if (color == 1) {
                int horizontal = cfa[phase ^ 1];
                int vertical = cfa[phase ^ 2];
                channels[horizontal] = row[xw] * 2.0f + row[xe] * 2.0f;
                channels[vertical] = north[x] * 2.0f + south[x] * 2.0f;
            } else {
                channels[1] = ((north[x] + south[x]) + row[xw]) + row[xe];
                channels[2 - color] = ((north[xw] + north[xe]) + south[xw]) + south[xe];
            }
            size_t offset = ((size_t)y * width + x) * 3;
            for (unsigned c = 0; c < 3; ++c)
                out[offset + c] = quantize(channels[c] * wb4[c], threshold, bins);
        }
    }
    return 0;
}
