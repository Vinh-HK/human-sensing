// Test des décodeurs sur PC :  g++ -std=c++17 -Iinclude test/test_parsers.cpp -o t && ./t
// (ou : pio test -e native)
#include <assert.h>
#include <stdio.h>
#include "radar_parsers.h"

int main() {
  // Exemple du manuel LD2450 : x=-782 mm, y=1713 mm, v=-16 cm/s, rés.=320 mm
  const uint8_t f[30] = {0xAA, 0xFF, 0x03, 0x00, 0x0E, 0x03, 0xB1, 0x86, 0x10, 0x00,
                         0x40, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x55, 0xCC};
  Ld2450Parser p;
  // octets parasites puis un faux début d'en-tête, puis la trame
  const uint8_t junk[] = {0x12, 0xAA, 0xFF, 0x07};
  for (uint8_t b : junk) assert(!p.feed(b));
  bool got = false;
  for (uint8_t b : f) got = p.feed(b);
  assert(got);
  assert(p.targets[0].valid && p.targets[0].x_mm == -782 && p.targets[0].y_mm == 1713);
  assert(p.targets[0].speed_cms == -16 && p.targets[0].res_mm == 320);
  assert(!p.targets[1].valid && !p.targets[2].valid);

  // LD2410C, rapport « immobile à 230 cm, énergie 45 »
  const uint8_t r[] = {0xF4, 0xF3, 0xF2, 0xF1, 0x0D, 0x00, 0x02, 0xAA, 0x02, 0x00, 0x00, 0x00,
                       0xE6, 0x00, 0x2D, 0xE6, 0x00, 0x55, 0x00, 0xF8, 0xF7, 0xF6, 0xF5};
  Ld2410Parser q;
  got = false;
  for (uint8_t b : r) got = q.feed(b);
  assert(got && q.report.state == 2 && q.report.static_cm == 230 && q.report.static_energy == 45);
  printf("parsers OK\n");
  return 0;
}
