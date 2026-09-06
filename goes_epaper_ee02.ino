/*
 * goes_epaper_ee02.ino — XIAO ePaper DIY Kit EE02 (13.3" Spectra 6, 1600x1200)
 *
 * Wakes every 30 minutes, asks the host whether the frame actually changed,
 * and only burns a panel refresh if it did. Streams the pre-dithered 4bpp
 * buffer straight into PSRAM — no decoding, no colour maths on the MCU.
 *
 * Day/night cadence: refreshes every 30 min from 1 h before sunrise to 3 h
 * after sunset, then slows to every 2 h through the deep-night hours to save
 * battery (sun times computed on-device from the scene location via NTP).
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
#include <time.h>
#include <math.h>

// ---------------------------------------------------------------- config ---
// WiFi credentials live in secrets.h, which is gitignored so it never lands
// in the public repo. Copy secrets.h.example to secrets.h and fill in your
// network. secrets.h must sit next to this .ino for the compile to find it.
#include "secrets.h"   // defines WIFI_SSID and WIFI_PASS

// GitHub Pages (free path) — replace with your own:
static const char *HOST = "https://braytonmiles.github.io/goes-paper";
// Self-hosted alternative:
// static const char *HOST = "http://192.168.1.50:8080";

static const uint32_t REFRESH_SECONDS = 1800;                 // 30 minutes
// Battery note: a skip-wake (SHA unchanged) is cheap - tiny GET, no 960 KB
// download, no panel refresh. A real refresh is the expensive one. Once the
// dispatch pinger makes renders actually land every 10 min, most wakes become
// real refreshes, so 600 here would mean ~144 panel refreshes/day instead of
// ~48. Freshness is bounded by the source anyway (SLIDER full_disk: new frame
// every 10 min, ~24 min behind real time), so 1800 costs almost nothing.
static const int      PANEL_W = 1600;
static const int      PANEL_H = 1200;
static const size_t   FRAME_BYTES = (size_t)PANEL_W * PANEL_H / 2;   // 4bpp

// Two refresh cadences by time of day. In the daytime window - from
// PRE_SUNRISE_S before sunrise to POST_SUNSET_S after sunset - the board runs
// the normal REFRESH_SECONDS cadence. Through the deep-night hours it slows to
// NIGHT_SECONDS to save battery (GeoColor at night is the least useful frame).
// Sun times are computed on-device from the scene location below (keep in sync
// with LAT/LON in goes_epaper.py).
static const double   SUN_LAT       = 34.0522;    // scene centre (Los Angeles)
static const double   SUN_LON       = -118.2437;  // signed longitude (E+), = LON in goes_epaper.py
static const uint32_t PRE_SUNRISE_S = 1UL * 3600; // daytime window: 1 h before sunrise
static const uint32_t POST_SUNSET_S = 3UL * 3600; //                 to 3 h after sunset
static const uint32_t NIGHT_SECONDS = 2UL * 3600; // deep-night refresh cadence (2 h)

Seeed_GFX display;

// Survives deep sleep, so redundant refreshes are skipped across wakes.
RTC_DATA_ATTR char lastSha[24] = {0};
RTC_DATA_ATTR uint32_t wakeCount = 0;

// Sleep interval chosen each wake: day cadence vs deep-night cadence. Set after
// the NTP sync in setup(); every sleepAgain() at end/error paths uses it.
static uint32_t g_sleepSecs = REFRESH_SECONDS;

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

static void sleepFor(uint32_t secs) {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.printf("sleeping %u s\n", secs);
  Serial.flush();
  esp_sleep_enable_timer_wakeup((uint64_t)secs * 1000000ULL);
  esp_deep_sleep_start();
}

static void sleepAgain() { sleepFor(g_sleepSecs); }

// ------------------------------------------------------------------ clock ---
static bool timeSync(uint32_t timeoutMs = 8000) {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");   // UTC, no tz offset
  uint32_t t0 = millis();
  time_t now = 0;
  while ((now = time(nullptr)) < 1700000000 && millis() - t0 < timeoutMs)
    delay(200);
  return now >= 1700000000;   // sanity: clock actually set (~2023-11 or later)
}

// Sunrise equation (en.wikipedia.org/wiki/Sunrise_equation). n is the integer
// solar day count since 2000-01-01; lonDeg is signed longitude (east positive).
// Fills the UTC epoch seconds of that solar day's sunrise and sunset.
static void sunForN(long n, double latDeg, double lonDeg,
                    time_t &riseOut, time_t &setOut) {
  const double DEG = M_PI / 180.0;
  double Jstar    = (double)n - lonDeg / 360.0;
  double M        = fmod(357.5291 + 0.98560028 * Jstar, 360.0);
  double Mr       = M * DEG;
  double C        = 1.9148 * sin(Mr) + 0.0200 * sin(2*Mr) + 0.0003 * sin(3*Mr);
  double lambda   = fmod(M + C + 180.0 + 102.9372, 360.0);
  double lr       = lambda * DEG;
  double Jtransit = 2451545.0 + Jstar + 0.0053 * sin(Mr) - 0.0069 * sin(2*lr);
  double delta    = asin(sin(lr) * sin(23.44 * DEG));
  double phi      = latDeg * DEG;
  double cosO     = (sin(-0.833 * DEG) - sin(phi) * sin(delta)) /
                    (cos(phi) * cos(delta));
  if (cosO >  1.0) cosO =  1.0;    // polar day:   sun never sets
  if (cosO < -1.0) cosO = -1.0;    // polar night: sun never rises
  double omega    = acos(cosO) / (2.0 * M_PI);   // fraction of a day
  riseOut = (time_t)llround(((Jtransit - omega) - 2440587.5) * 86400.0);
  setOut  = (time_t)llround(((Jtransit + omega) - 2440587.5) * 86400.0);
}

// Deep-sleep interval for this wake: REFRESH_SECONDS while inside the daytime
// window [sunrise - PRE, sunset + POST], else the slower NIGHT_SECONDS. During
// deep night we still never sleep past the window's opening, so the fast daytime
// cadence resumes right on time. Checks the solar days around now so the window
// that straddles UTC midnight is handled correctly.
static uint32_t planSleep() {
  time_t now  = time(nullptr);
  double Jnow = (double)now / 86400.0 + 2440587.5;
  long   n0   = (long)lround(Jnow - 2451545.0 - 0.0009);

  time_t nextStart = 0;
  bool   haveNext  = false;
  for (long dn = -1; dn <= 1; dn++) {
    time_t rise, set;
    sunForN(n0 + dn, SUN_LAT, SUN_LON, rise, set);
    time_t start = rise - (time_t)PRE_SUNRISE_S;
    time_t end   = set  + (time_t)POST_SUNSET_S;
    if (now >= start && now <= end) return REFRESH_SECONDS;   // daytime cadence
    if (start > now && (!haveNext || start < nextStart)) {    // track next open
      nextStart = start;
      haveNext  = true;
    }
  }
  // Deep night: slow cadence, but wake exactly when the window next opens if
  // that comes sooner than NIGHT_SECONDS.
  uint32_t secs = NIGHT_SECONDS;
  if (haveNext) {
    long untilOpen = (long)(nextStart - now);
    if (untilOpen > 60 && (uint32_t)untilOpen < secs) secs = (uint32_t)untilOpen;
  }
  return secs;
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

  // Pick this wake's cadence from the sun position. If NTP fails we keep the
  // default 30-min cadence, so a clock glitch never strands the panel.
  if (timeSync()) {
    g_sleepSecs = planSleep();
    Serial.printf("cadence: %u s (~%.1f h) after this refresh\n",
                  g_sleepSecs, g_sleepSecs / 3600.0);
  } else {
    Serial.println("NTP failed; using default 30-min cadence");
  }

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
