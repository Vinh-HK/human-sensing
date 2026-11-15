// Décodeurs à flux des trames HLK-LD2450 et HLK-LD2410C.
// C++ pur (aucune dépendance Arduino) : testable sur PC (voir test/).
//
// Principe : une machine à états qui reçoit les octets un par un, comme ils
// arrivent de l'UART, se resynchronise sur l'en-tête après une erreur, et
// signale une trame complète et valide.
#pragma once
#include <stdint.h>
#include <string.h>

struct Ld2450Target {
  int16_t x_mm;      // + à droite du radar
  int16_t y_mm;      // + devant le radar
  int16_t speed_cms; // + approche (convention Hi-Link)
  uint16_t res_mm;
  bool valid;
};

// Format signe/valeur du LD2450 : bit 15 = 1 -> positif.
static inline int16_t ld2450_s15(uint8_t lo, uint8_t hi) {
  uint16_t raw = (uint16_t)lo | ((uint16_t)hi << 8);
  int16_t v = (int16_t)(raw & 0x7FFF);
  return (raw & 0x8000) ? v : (int16_t)-v;
}

class Ld2450Parser {
 public:
  Ld2450Target targets[3];
  uint32_t frames_ok = 0, frames_bad = 0;

  // Retourne true quand une trame complète et valide vient d'être décodée.
  bool feed(uint8_t b) {
    static const uint8_t HEAD[4] = {0xAA, 0xFF, 0x03, 0x00};
    if (n_ < 4) {
      if (b == HEAD[n_]) { buf_[n_++] = b; }
      else { n_ = (b == HEAD[0]) ? 1 : 0; if (n_) buf_[0] = b; }
      return false;
    }
    buf_[n_++] = b;
    if (n_ < 30) return false;
    n_ = 0;
    if (buf_[28] != 0x55 || buf_[29] != 0xCC) { frames_bad++; return false; }
    for (int k = 0; k < 3; k++) {
      const uint8_t* c = &buf_[4 + 8 * k];
      bool empty = true;
      for (int i = 0; i < 8; i++) if (c[i]) { empty = false; break; }
      targets[k].valid = !empty;
      targets[k].x_mm = ld2450_s15(c[0], c[1]);
      targets[k].y_mm = ld2450_s15(c[2], c[3]);
      targets[k].speed_cms = ld2450_s15(c[4], c[5]);
      targets[k].res_mm = (uint16_t)c[6] | ((uint16_t)c[7] << 8);
    }
    frames_ok++;
    return true;
  }

 private:
  uint8_t buf_[30];
  int n_ = 0;
};

struct Ld2410Report {
  uint8_t state;        // 0 personne, 1 mouvement, 2 immobile, 3 les deux
  uint16_t moving_cm;
  uint8_t moving_energy;
  uint16_t static_cm;
  uint8_t static_energy;
  uint16_t detect_cm;
};

class Ld2410Parser {
 public:
  Ld2410Report report{};
  uint32_t frames_ok = 0, frames_bad = 0;

  bool feed(uint8_t b) {
    static const uint8_t HEAD[4] = {0xF4, 0xF3, 0xF2, 0xF1};
    static const uint8_t TAIL[4] = {0xF8, 0xF7, 0xF6, 0xF5};
    if (n_ < 4) {
      if (b == HEAD[n_]) { buf_[n_++] = b; }
      else { n_ = (b == HEAD[0]) ? 1 : 0; if (n_) buf_[0] = b; }
      return false;
    }
    buf_[n_++] = b;
    if (n_ == 6) {
      len_ = (uint16_t)buf_[4] | ((uint16_t)buf_[5] << 8);
      if (len_ < 13 || len_ > sizeof(buf_) - 10) { frames_bad++; n_ = 0; }
      return false;
    }
    if (n_ < 6 || n_ < (int)(6 + len_ + 4)) return false;
    n_ = 0;
    const uint8_t* d = &buf_[6];
    if (memcmp(&buf_[6 + len_], TAIL, 4) != 0 || d[1] != 0xAA ||
        (d[0] != 0x01 && d[0] != 0x02)) {
      frames_bad++;
      return false;
    }
    report.state = d[2];
    report.moving_cm = (uint16_t)d[3] | ((uint16_t)d[4] << 8);
    report.moving_energy = d[5];
    report.static_cm = (uint16_t)d[6] | ((uint16_t)d[7] << 8);
    report.static_energy = d[8];
    report.detect_cm = (uint16_t)d[9] | ((uint16_t)d[10] << 8);
    frames_ok++;
    return true;
  }

 private:
  uint8_t buf_[64];
  int n_ = 0;
  uint16_t len_ = 0;
};
