/*
 * goes_epaper_ee02.ino — XIAO ePaper DIY Kit EE02 (13.3" Spectra 6, 1600x1200)
 *
 * Wakes every 10 minutes, asks the host whether the frame actually changed,
 * and only burns a panel refresh if it did. Streams the pre-dithered 4bpp
 * buffer straight into PSRAM — no decoding, no colour maths on the MCU.
 *
 * Works against either backend:
 *   GitHub Pages   https://<user>.github.io/<repo>/     (TLS, free)
 *   your own box   http://192.168.1.50:8080/            (plain, LAN)
 * Set HOST and the sketch picks the right transport automatically.
 *
 * Board:  Tools -> Board  -> "XIAO ESP32S3 Plus"
 *         Tools -> PSRAM  -> enabled           (required: the buffer is 940 KB)
 *
 * Library: Seeed_GFX v2 (github.com/Seeed-Studio/Seeed_GFX2). The board and
 * panel are selected as template parameters to begin() below — no driver.h.
 */

#include <Seeed_GFX.h>
#include "board/boards/XIAO_EPaper_Boards.h"
#include "panel/configs/Seeed_Panel_Configs.h"
#include "driver/epaper/Driver_T133A01.h"
#include "panel/Panel_EPaper.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <esp_sleep.h>

// ---------------------------------------------------------------- config ---
// WiFi credentials live in secrets.h, which is gitignored so it never lands
// in the public repo. Copy secrets.h.example to secrets.h and fill in your
// network. secrets.h must sit next to this .ino for the compile to find it.
#include "secrets.h"   // defines WIFI_SSID and WIFI_PASS

// GitHub Pages (free path) — replace with your own:
static const char *HOST = "https://braytonmiles.github.io/goes-paper";
// Self-hosted alternative:
// static const char *HOST = "http://192.168.1.50:8080";

static const uint32_t REFRESH_SECONDS = 600;                  // 10 minutes
static const int      PANEL_W = 1600;
static const int      PANEL_H = 1200;
static const size_t   FRAME_BYTES = (size_t)PANEL_W * PANEL_H / 2;   // 4bpp

Seeed_GFX display;

// Survives deep sleep, so redundant refreshes are skipped across wakes.
RTC_DATA_ATTR char lastSha[24] = {0};
RTC_DATA_ATTR uint32_t wakeCount = 0;

static WiFiClient      plainClient;
static WiFiClientSecure tlsClient;

// --------------------------------------------------------------- transport ---
static bool hostIsTls() { return String(HOST).startsWith("https"); }

// setInsecure() skips certificate validation. For public, non-secret satellite
// imagery that is a deliberate trade: it avoids pinning a CA that will rotate
// out from under a device you may never reflash. Do NOT copy this pattern for
// anything carrying credentials.
static bool beginRequest(HTTPClient &http, const String &url) {
  bool ok;
  if (hostIsTls()) {
    tlsClient.setInsecure();
    ok = http.begin(tlsClient, url);
  } else {
    ok = http.begin(plainClient, url);
  }
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.setTimeout(30000);
  return ok;
}

// ------------------------------------------------------------------ wifi ---
static bool wifiUp(uint32_t timeoutMs = 20000) {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

static void sleepAgain() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.printf("sleeping %u s\n", REFRESH_SECONDS);
  Serial.flush();
  esp_sleep_enable_timer_wakeup((uint64_t)REFRESH_SECONDS * 1000000ULL);
  esp_deep_sleep_start();
}

// ------------------------------------------------------- change detection ---
// Comparing a digest first means a becalmed sky costs one small HTTP round
// trip instead of a 20-second panel refresh. Spectra 6 panels have a finite
// refresh budget; spending it only on frames that differ is the difference
// between years and months.
static bool frameChanged(String &shaOut) {
  HTTPClient http;
  if (!beginRequest(http, String(HOST) + "/latest.sha")) return false;
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("sha fetch failed: %d\n", code);
    http.end();
    return false;
  }
  shaOut = http.getString();
  shaOut.trim();
  http.end();

  if (shaOut.length() == 0) return false;
  if (strncmp(lastSha, shaOut.c_str(), sizeof(lastSha) - 1) == 0) {
    Serial.println("frame unchanged; skipping refresh");
    return false;
  }
  return true;
}

// ------------------------------------------------------------ frame fetch ---
static bool fetchFrame(uint8_t *buf) {
  HTTPClient http;
  if (!beginRequest(http, String(HOST) + "/latest.bin")) return false;
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("bin fetch failed: %d\n", code);
    http.end();
    return false;
  }

  int len = http.getSize();
  if (len > 0 && (size_t)len != FRAME_BYTES) {
    Serial.printf("unexpected length %d (want %u)\n", len, (unsigned)FRAME_BYTES);
    http.end();
    return false;
  }

  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  uint32_t idle = millis();
  while (got < FRAME_BYTES && millis() - idle < 30000) {
    size_t avail = stream->available();
    if (!avail) { delay(5); continue; }
    size_t want = min(avail, FRAME_BYTES - got);
    int n = stream->readBytes(buf + got, want);
    if (n > 0) { got += n; idle = millis(); }
  }
  http.end();

  Serial.printf("received %u / %u bytes\n", (unsigned)got, (unsigned)FRAME_BYTES);
  return got == FRAME_BYTES;
}

// ------------------------------------------------------------------ setup ---
void setup() {
  Serial.begin(115200);
  delay(200);
  wakeCount++;
  Serial.printf("\n=== wake #%u ===\n", wakeCount);

  if (!wifiUp()) { Serial.println("no wifi"); sleepAgain(); }
  Serial.printf("wifi ok, %s\n", WiFi.localIP().toString().c_str());

  String sha;
  if (!frameChanged(sha)) sleepAgain();

  uint8_t *buf = (uint8_t *)ps_malloc(FRAME_BYTES);
  if (!buf) { Serial.println("ps_malloc failed - is PSRAM enabled?"); sleepAgain(); }

  if (!fetchFrame(buf)) { free(buf); sleepAgain(); }

  if (!display.begin<Board_XIAO_ePaper_EE02,
                     Config_Seeed_ePaper_13inch3_Colorful_T133A01>()) {
    Serial.println(display.lastResult().message);
    free(buf);
    sleepAgain();
  }
  display.setRotation(1);                // landscape 1600x1200

  // Seeed_GFX v2 takes the packed 4bpp six-colour buffer exactly as the
  // renderer emits it (two pixels per byte). dataInProgmem=false because our
  // buffer lives in PSRAM, not memory-mapped flash.
  display.pushImage4BPP(0, 0, PANEL_W, PANEL_H, buf, false);
  display.refresh();                     // ~20 s full-colour refresh

  free(buf);
  strncpy(lastSha, sha.c_str(), sizeof(lastSha) - 1);
  lastSha[sizeof(lastSha) - 1] = '\0';
  Serial.printf("displayed %s\n", lastSha);

  sleepAgain();
}

void loop() { /* never reached; setup() ends in deep sleep */ }
