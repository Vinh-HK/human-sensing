"""Décodeurs des trames série HLK-LD2450 et HLK-LD2410C.

Le firmware ESP32 fait le même travail en C++. Cette version Python sert à :
  - tester le protocole sans ESP32 (radar branché sur un adaptateur USB-UART) ;
  - écrire des tests unitaires sur des trames réelles enregistrées.

LD2450 (256000 bauds, 8N1), trame de 30 octets :
  AA FF 03 00 | cible1 (8) | cible2 (8) | cible3 (8) | 55 CC
  cible : x(2) y(2) vitesse(2) résolution(2), petit-boutiste.
  x, y, vitesse : bit 15 = signe (1 = positif, 0 = négatif), bits 0-14 = valeur.
  Unités : mm, mm, cm/s, mm. Une cible vide vaut 8 octets à zéro.

LD2410C (256000 bauds, 8N1), trame de rapport :
  F4 F3 F2 F1 | longueur (2, LE) | données | F8 F7 F6 F5
  données (mode normal) : 02 AA état dist_mob(2) énergie_mob dist_stat(2)
                          énergie_stat dist_détection(2) 55 00
  état : 0 = personne, 1 = en mouvement, 2 = immobile, 3 = les deux.
"""

from __future__ import annotations

from dataclasses import dataclass

LD2450_HEAD = b"\xAA\xFF\x03\x00"
LD2450_TAIL = b"\x55\xCC"
LD2450_LEN = 30

LD2410_HEAD = b"\xF4\xF3\xF2\xF1"
LD2410_TAIL = b"\xF8\xF7\xF6\xF5"


def _signed15(lo: int, hi: int) -> int:
    raw = lo | (hi << 8)
    val = raw & 0x7FFF
    return val if raw & 0x8000 else -val


@dataclass
class LD2450Target:
    x_mm: int
    y_mm: int
    speed_cms: int
    res_mm: int


@dataclass
class LD2410Report:
    state: int
    moving_cm: int
    moving_energy: int
    static_cm: int
    static_energy: int
    detect_cm: int

    @property
    def present(self) -> bool:
        return self.state != 0


def decode_ld2450_frame(frame: bytes) -> list[LD2450Target]:
    if len(frame) != LD2450_LEN or not frame.startswith(LD2450_HEAD) or not frame.endswith(LD2450_TAIL):
        raise ValueError("trame LD2450 invalide")
    out = []
    for k in range(3):
        o = 4 + 8 * k
        chunk = frame[o:o + 8]
        if not any(chunk):
            continue
        out.append(LD2450Target(
            x_mm=_signed15(chunk[0], chunk[1]),
            y_mm=_signed15(chunk[2], chunk[3]),
            speed_cms=_signed15(chunk[4], chunk[5]),
            res_mm=chunk[6] | (chunk[7] << 8),
        ))
    return out


class LD2450Parser:
    """Parser à flux : on lui donne des octets dans n'importe quel découpage."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_frames = 0

    def feed(self, data: bytes) -> list[list[LD2450Target]]:
        self.buf += data
        frames = []
        while True:
            i = self.buf.find(LD2450_HEAD)
            if i < 0:
                # garder les 3 derniers octets : un en-tête peut être coupé
                del self.buf[:-3]
                return frames
            if len(self.buf) - i < LD2450_LEN:
                del self.buf[:i]
                return frames
            cand = bytes(self.buf[i:i + LD2450_LEN])
            if cand.endswith(LD2450_TAIL):
                frames.append(decode_ld2450_frame(cand))
                del self.buf[:i + LD2450_LEN]
            else:
                self.bad_frames += 1
                del self.buf[:i + 1]


def decode_ld2410_payload(data: bytes) -> LD2410Report | None:
    # data = octets entre la longueur et le pied de trame
    if len(data) < 13 or data[1] != 0xAA or data[0] not in (0x01, 0x02):
        return None
    return LD2410Report(
        state=data[2],
        moving_cm=data[3] | (data[4] << 8),
        moving_energy=data[5],
        static_cm=data[6] | (data[7] << 8),
        static_energy=data[8],
        detect_cm=data[9] | (data[10] << 8),
    )


class LD2410Parser:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_frames = 0

    def feed(self, data: bytes) -> list[LD2410Report]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(LD2410_HEAD)
            if i < 0:
                del self.buf[:-3]
                return out
            if len(self.buf) - i < 6:
                del self.buf[:i]
                return out
            n = self.buf[i + 4] | (self.buf[i + 5] << 8)
            if n > 64:  # longueur absurde : en-tête parasite
                self.bad_frames += 1
                del self.buf[:i + 1]
                continue
            end = i + 6 + n + 4
            if len(self.buf) < end:
                del self.buf[:i]
                return out
            if bytes(self.buf[end - 4:end]) == LD2410_TAIL:
                rep = decode_ld2410_payload(bytes(self.buf[i + 6:i + 6 + n]))
                if rep is not None:
                    out.append(rep)
                else:
                    self.bad_frames += 1
                del self.buf[:end]
            else:
                self.bad_frames += 1
                del self.buf[:i + 1]


# --- encodeurs (pour les tests et le simulateur) -----------------------------

def _enc15(v: int) -> bytes:
    raw = (abs(v) & 0x7FFF) | (0x8000 if v >= 0 else 0)
    return bytes((raw & 0xFF, raw >> 8))


def encode_ld2450_frame(targets: list[tuple[int, int, int, int]]) -> bytes:
    body = b""
    for k in range(3):
        if k < len(targets):
            x, y, v, r = targets[k]
            body += _enc15(x) + _enc15(y) + _enc15(v) + bytes((r & 0xFF, r >> 8))
        else:
            body += bytes(8)
    return LD2450_HEAD + body + LD2450_TAIL


def encode_ld2410_report(state: int, mov_cm=0, mov_e=0, sta_cm=0, sta_e=0, det_cm=0) -> bytes:
    d = bytes((0x02, 0xAA, state, mov_cm & 0xFF, mov_cm >> 8, mov_e,
               sta_cm & 0xFF, sta_cm >> 8, sta_e, det_cm & 0xFF, det_cm >> 8, 0x55, 0x00))
    return LD2410_HEAD + bytes((len(d), 0)) + d + LD2410_TAIL
