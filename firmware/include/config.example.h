// Copier en config.h (non versionné) et adapter.
#pragma once

#define NODE_ID "n-salon"          // doit correspondre à un id de hub/config.yaml

#define WIFI_SSID "mon-wifi"
#define WIFI_PASS "mot-de-passe"

#define MQTT_HOST "192.168.1.10"   // le hub (Raspberry Pi ou PC)
#define MQTT_PORT 1883
#define MQTT_USER nullptr
#define MQTT_PASS nullptr

// Câblage ESP32 DevKit (ESP32-WROOM-32). Croiser TX/RX :
//   radar TX -> broche RX de l'ESP32, radar RX -> broche TX de l'ESP32.
#define PIN_LD2450_RX 16
#define PIN_LD2450_TX 17
#define PIN_LD2410_RX 18
#define PIN_LD2410_TX 19

#define HAS_LD2410 1               // 0 si le nœud n'a que le LD2450

// 1 = envoie au démarrage la commande « multi-cibles » au LD2450.
// Laisser à 0 si votre module est déjà en multi-cibles (cas courant).
#define LD2450_FORCE_MULTI_TARGET 0
