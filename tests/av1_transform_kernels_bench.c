#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include "av1_transform_kernels_bench.h"
#include "config/av1_rtcd.h"
#include "av1/common/scan.h"
#include "aom_ports/mem.h"

static int64_t now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

static int64_t native_bench(const int *k, int64_t iterations, int depth) {
  static int32_t coefficients[4096];
  static uint8_t samples[4096];
  static uint16_t high_samples[4096];
  const int sizes[][2] = {
    {4,4},{8,8},{16,16},{32,32},{64,64},{4,8},{8,4},{8,16},{16,8},
    {16,32},{32,16},{32,64},{64,32},{4,16},{16,4},{8,32},{32,8},{16,64},{64,16}
  };
  const int w = k[0], h = k[1], ch = h < 32 ? h : 32, cw = w < 32 ? w : 32;
  int tx = 0;
  while (tx < 19 && (sizes[tx][0] != w || sizes[tx][1] != h)) ++tx;
  if (tx == 19) abort();
  uint32_t seed = 12345;
  for (int i = 0; i < cw * ch; ++i) coefficients[i] = 0;
  for (int i = 0; i < w * h; ++i) {
    seed = seed * 1664525 + 1013904223;
    int r = i / w, c = i % w;
    if (r < k[3] && c < k[4]) coefficients[c * ch + r] = (int32_t)((seed >> 8) % 401) - 200;
    samples[i] = (uint8_t)((seed >> 4) & 255);
    high_samples[i] = (uint16_t)((seed >> 4) & 1023);
  }
  coefficients[0] = 700;
  const int16_t *scan = av1_scan_orders[tx][k[2]].scan;
  int eob = 0;
  for (int i = 0; i < cw * ch; ++i) if (coefficients[scan[i]]) eob = i + 1;
  TxfmParam param = {0};
  param.tx_size = (TX_SIZE)tx;
  param.tx_type = (TX_TYPE)k[2];
  param.bd = depth;
  param.is_hbd = depth > 8;
  param.tx_set_type = EXT_TX_SET_ALL16;
  param.eob = eob;
  for (int64_t i = 0; i < iterations; ++i) {
    if (depth == 8) av1_inv_txfm_add(coefficients, samples, w, &param);
    else av1_highbd_inv_txfm_add(coefficients, CONVERT_TO_BYTEPTR(high_samples), w, &param);
  }
  uint64_t sum = 0;
  for (int i = 0; i < w * h; ++i) sum = sum * 31 + (depth == 8 ? samples[i] : high_samples[i]);
  return (int64_t)sum;
}

static int compare_double(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return (x > y) - (x < y);
}

int main(int argc, char **argv) {
  int depth = argc > 1 ? atoi(argv[1]) : 8;
  if (depth != 8 && depth != 10) return 3;
  av1_rtcd();
  static const int cases[][5] = {
    {16,16,0,16,16},{16,16,0,8,8},{16,16,0,4,4},{8,8,0,8,8},{8,8,0,4,4},
    {8,8,3,8,8},{8,8,1,8,8},{8,8,2,8,8},{16,16,3,8,8},{16,16,1,8,8},
    {4,4,0,4,4},{4,4,3,4,4},{4,4,1,4,4},{4,4,2,4,4},{16,8,0,8,8},
    {8,16,0,8,8},{4,8,0,8,4},{8,4,3,4,8},{32,32,0,8,8},{32,32,0,32,32},
    {64,64,0,8,8},{16,16,9,8,8}
  };
  for (unsigned c = 0; c < sizeof cases / sizeof cases[0]; ++c) {
    const int *k = cases[c];
    int64_t expected = native_bench(k, 1, depth);
    for (int p = 0; p < 2; ++p) {
      if (av1_transform_kernels_bench_KernelBench(k[0], k[1], k[2], k[3], k[4], p, depth, 1) != expected) {
        fprintf(stderr, "Transform benchmark mismatch: %dx%d type %d path %d\n", k[0], k[1], k[2], p);
        return 1;
      }
    }
    double ns[3];
    for (int p = 0; p < 3; ++p) {
      double times[5];
      int64_t n = 200000 / (k[0] * k[1] / 16 + 1) + 1000;
      for (int rep = 0; rep < 5; ++rep) {
        int64_t t0 = now();
        int64_t sum = p == 2 ? native_bench(k, n, depth) :
          av1_transform_kernels_bench_KernelBench(k[0], k[1], k[2], k[3], k[4], p, depth, n);
        times[rep] = (double)(now() - t0) / n;
        if (sum != native_bench(k, n, depth)) return 2;
      }
      qsort(times, 5, sizeof times[0], compare_double);
      ns[p] = times[2];
    }
    printf("%d-bit %2dx%-2d type %2d box %2dx%-2d kernel %.1f ns general %.1f ns libaom %.1f ns ratio %.2f\n",
      depth,k[0],k[1],k[2],k[3],k[4],ns[0],ns[1],ns[2],ns[0] / ns[2]);
  }
  return 0;
}
