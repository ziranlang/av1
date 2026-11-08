#include <inttypes.h>
#include <stdio.h>
#include "av1_transform_kernels_highbit.h"

int main(void) {
  int64_t accepted = 0, rejected = 0, last_case = 0;
  int32_t status = av1_transform_kernels_highbit_HighTestCheck(&accepted, &rejected, false, &last_case);
  if (status) {
    fprintf(stderr, "AV1 kernel failure %d: width %" PRId64 " height %" PRId64
      " type %" PRId64 " trial %" PRId64 "\n", status, last_case / 1000000,
      last_case / 10000 % 100, last_case / 100 % 100, last_case % 100);
    return status;
  }
  if (accepted < 3000 || rejected < 100) return 20;
  printf("AV1 transform kernels: %" PRId64 " accepted, %" PRId64 " rejected\n", accepted, rejected);
  return 0;
}
