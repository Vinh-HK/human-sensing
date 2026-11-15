// Nœud radar : ESP32 + HLK-LD2450 (suivi) + HLK-LD2410C (présence statique).
//
// Rôle : lire les deux radars, publier une trame JSON par trame LD2450 (≈10 Hz)
// sur MQTT. Aucun traitement lourd ici : le suivi multi-pièces se fait sur le
// hub, qui seul connaît le plan de l'appartement.
//
// Topic  : radar/<NODE_ID>/frame
// Charge : {"seq":123,"t":45678,"tg":[[x_mm,y_mm,v_cms],...],"st":{"state":2,"mov":0,"sta":230,"e":45}}
// Statut : radar/<NODE_ID>/status  "online" / "offline" (LWT, retenu)

#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "config.h"
#include "radar_parsers.h"

HardwareSerial& LD2450 = Serial2;   // UART2
HardwareSerial& LD2410 = Serial1;   // UART1 (broches remappées)

WiFiClient net;
PubSubClient mqtt(net);
Ld2450Parser p2450;
Ld2410Parser p2410;

String topicFrame, topicStatus;
uint32_t seq = 0;
uint32_t lastFrameMs = 0, lastStaticMs = 0, lastHealthMs = 0;

static void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // latence stable (le modem-sleep ajoute des dizaines de ms)
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(200);
}

static void connectMqtt() {
  if (mqtt.connected()) return;
  String cid = String("radar-") + NODE_ID;
  if (mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASS, topicStatus.c_str(), 1, true, "offline")) {
    mqtt.publish(topicStatus.c_str(), "online", true);
  }
}

#if LD2450_FORCE_MULTI_TARGET
// Passage en mode multi-cibles. Commandes du protocole Hi-Link (à vérifier
// dans la version du document correspondant à votre firmware radar).
static void ld2450Cmd(const uint8_t* payload, size_t n) {
  const uint8_t head[4] = {0xFD, 0xFC, 0xFB, 0xFA}, tail[4] = {0x04, 0x03, 0x02, 0x01};
  uint8_t len[2] = {(uint8_t)n, 0};
  LD2450.write(head, 4); LD2450.write(len, 2); LD2450.write(payload, n); LD2450.write(tail, 4);
  delay(100);
}
static void ld2450MultiTarget() {
  const uint8_t enableCfg[] = {0xFF, 0x00, 0x01, 0x00};
  const uint8_t multi[] = {0x90, 0x00};
  const uint8_t endCfg[] = {0xFE, 0x00};
  ld2450Cmd(enableCfg, sizeof enableCfg);
  ld2450Cmd(multi, sizeof multi);
  ld2450Cmd(endCfg, sizeof endCfg);
}
#endif

static void publishFrame() {
  JsonDocument doc;
  doc["seq"] = seq++;
  doc["t"] = millis();
  JsonArray tg = doc["tg"].to<JsonArray>();
  for (auto& t : p2450.targets) {
    if (!t.valid) continue;
    JsonArray a = tg.add<JsonArray>();
    a.add(t.x_mm); a.add(t.y_mm); a.add(t.speed_cms);
  }
  if (HAS_LD2410 && millis() - lastStaticMs < 2000) {
    JsonObject st = doc["st"].to<JsonObject>();
    st["state"] = p2410.report.state;
    st["mov"] = p2410.report.moving_cm;
    st["sta"] = p2410.report.static_cm;
    st["e"] = p2410.report.static_energy;
  }
  char out[384];
  size_t n = serializeJson(doc, out, sizeof out);
  mqtt.publish(topicFrame.c_str(), (const uint8_t*)out, n, false);
}

void setup() {
  Serial.begin(115200);
  LD2450.begin(256000, SERIAL_8N1, PIN_LD2450_RX, PIN_LD2450_TX);
  if (HAS_LD2410) LD2410.begin(256000, SERIAL_8N1, PIN_LD2410_RX, PIN_LD2410_TX);
  LD2450.setRxBufferSize(1024);
#if LD2450_FORCE_MULTI_TARGET
  ld2450MultiTarget();
#endif
  topicFrame = String("radar/") + NODE_ID + "/frame";
  topicStatus = String("radar/") + NODE_ID + "/status";
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);  // PubSubClient : 256 octets par défaut, trop juste
  connectWifi();
  connectMqtt();
}

void loop() {
  // 1. Vider les UART (ne jamais bloquer : 256000 bauds = 25 octets/ms)
  while (LD2450.available()) {
    if (p2450.feed((uint8_t)LD2450.read())) {
      lastFrameMs = millis();
      if (mqtt.connected()) publishFrame();
    }
  }
  if (HAS_LD2410) {
    while (LD2410.available()) {
      if (p2410.feed((uint8_t)LD2410.read())) lastStaticMs = millis();
    }
  }

  // 2. Réseau
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) {
    static uint32_t lastTry = 0;
    if (millis() - lastTry > 3000) { lastTry = millis(); connectMqtt(); }
  }
  mqtt.loop();

  // 3. Santé : un radar muet est signalé (câble, alimentation, mauvais baud)
  if (millis() - lastHealthMs > 10000) {
    lastHealthMs = millis();
    char h[160];
    snprintf(h, sizeof h, "{\"ld2450_ok\":%lu,\"ld2450_bad\":%lu,\"ld2410_ok\":%lu,\"rssi\":%d,\"silent_ms\":%lu}",
             (unsigned long)p2450.frames_ok, (unsigned long)p2450.frames_bad,
             (unsigned long)p2410.frames_ok, WiFi.RSSI(), (unsigned long)(millis() - lastFrameMs));
    mqtt.publish((String("radar/") + NODE_ID + "/health").c_str(), h);
    Serial.println(h);
  }
}
