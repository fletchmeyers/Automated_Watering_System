#include "packet_protocol.h"
#include "node_log.h"

// Build one reading as JSON with "t" first, optionally with "ts" last —
// the same shape as the Pico's SD lines. Returns the length, or 0 if it
// doesn't fit in cap bytes.
__attribute__((unused))
static size_t reading_json(size_t i, const char *ts, char *buf, size_t cap) {
  JsonDocument rec;
  rec["t"] = latest_tags[i];
  for (JsonPairConst kv : latest_readings[i].as<JsonObjectConst>()) {
    rec[kv.key()] = kv.value();
  }
  if (ts) rec["ts"] = ts;
  if (measureJson(rec) >= cap) {
    Serial.print(F("[LOG] Reading too long to log: "));
    Serial.println(latest_tags[i]);
    return 0;
  }
  return serializeJson(rec, buf, cap);
}

// Add "j":<place> to a JSON log line, so the Pi knows where in the chunk it
// goes and can ask again for just the lines it missed. Returns the length.
__attribute__((unused))
static size_t tag_line(const char *line, size_t len, uint32_t place, char *out, size_t cap) {
  if (len < 3 || line[0] != '{') {
    size_t n = len < cap ? len : cap;
    memcpy(out, line, n);
    return n;
  }
  int n = snprintf(out, cap, "{\"j\":%lu,%.*s", (unsigned long)place, (int)(len - 1), line + 1);
  if (n < 0) return 0;
  return (size_t)n < cap ? (size_t)n : cap - 1;
}

// A sync request's optional "j" is a bitmask of the chunk's lines it wants
// (bit i = line i); without one, the whole chunk is sent.
__attribute__((unused))
static bool line_wanted(JsonDocument &command, uint32_t place) {
  if (!command["j"].is<uint32_t>()) return true;
  return place < 32 && (command["j"].as<uint32_t>() >> place & 1);
}

static void send_se(PacketSender &sender, uint32_t gen, uint32_t offset, uint32_t count, bool more) {
  JsonDocument doc;
  doc["g"] = gen;
  doc["o"] = offset;
  doc["c"] = count;
  doc["m"] = more ? 1 : 0;
  sender.send(doc, "se");
}

static void send_info(PacketSender &sender, uint64_t used, uint64_t free_bytes, uint64_t total) {
  JsonDocument doc;
  doc["ub"] = used;
  doc["fb"] = free_bytes;
  doc["tb"] = total;
  sender.send(doc, "info");
}


#if LOG_BACKEND == LOG_SD
// ── microSD ──────────────────────────────────────────────────────────────
// Same files and rules as the Pico (communication_garden.py): readings are
// appended to data.txt as JSON lines; a sync renames it to sending.txt and
// serves it by byte offset; sync_cursor.txt holds "<generation> <offset>"
// the Pi has confirmed. See send_sync_chunk() there for the full protocol.

#include <SdFat.h>

static const char *DATA_FILE    = "data.txt";
static const char *SENDING_FILE = "sending.txt";
static const char *CURSOR_FILE  = "sync_cursor.txt";

static SdFat sd;
static bool sd_ok = false;
static uint64_t free_bytes = 0;   // counted once at boot, then kept up to date

static uint64_t file_size(const char *path) {
  File32 f;
  if (!f.open(path, O_RDONLY)) return 0;
  uint64_t size = f.fileSize();
  f.close();
  return size;
}

static void read_cursor(uint32_t *gen, uint32_t *offset) {
  *gen = 0;
  *offset = 0;
  File32 f;
  if (!f.open(CURSOR_FILE, O_RDONLY)) return;
  char buf[24] = {0};
  f.read(buf, sizeof(buf) - 1);
  f.close();
  unsigned long g, o;
  if (sscanf(buf, "%lu %lu", &g, &o) == 2) {
    *gen = g;
    *offset = o;
  }
}

static void write_cursor(uint32_t gen, uint32_t offset) {
  File32 f;
  if (!f.open(CURSOR_FILE, O_WRONLY | O_CREAT | O_TRUNC)) {
    Serial.println(F("[SYNC] Could not save cursor."));
    return;
  }
  f.print(gen);
  f.print(' ');
  f.print(offset);
  f.close();
}

bool node_log_init() {
  // SHARED_SPI: the radio is on the same bus. Both use SPI transactions,
  // and RadioHead registers its interrupt, so the radio can't cut in on an
  // SD transfer.
#ifdef SD_SPI_SCK
  // A card on its own SPI bus (the Adalogger's built-in slot): the second bus.
  SPI1.setSCK(SD_SPI_SCK);
  SPI1.setTX(SD_SPI_MOSI);
  SPI1.setRX(SD_SPI_MISO);
  sd_ok = sd.begin(SdSpiConfig(SD_CS, DEDICATED_SPI, SD_SCK_MHZ(12), &SPI1));
#else
  sd_ok = sd.begin(SdSpiConfig(SD_CS, SHARED_SPI, SD_SCK_MHZ(12)));
#endif
  if (!sd_ok) {
    Serial.println(F("[LOG] SD card not found (FAT16/FAT32 only). Logging disabled."));
    return false;
  }
  free_bytes = (uint64_t)sd.freeClusterCount() * sd.bytesPerCluster();
  Serial.print(F("[LOG] SD card ready, "));
  Serial.print((uint32_t)((file_size(DATA_FILE) + file_size(SENDING_FILE)) / 1024));
  Serial.print(F(" KB waiting to sync, "));
  Serial.print((uint32_t)(free_bytes / (1024UL * 1024)));
  Serial.println(F(" MB free."));
  return true;
}

void node_log_snapshot(uint32_t epoch) {
  if (!sd_ok || latest_count == 0) return;
  char ts[20], line[128];
  format_iso(epoch, ts);

  File32 f;
  if (!f.open(DATA_FILE, O_WRONLY | O_CREAT | O_APPEND)) {
    Serial.println(F("[LOG] Could not open data.txt."));
    return;
  }
  size_t logged = 0;
  for (size_t i = 0; i < latest_count; i++) {
    size_t len = reading_json(i, ts, line, sizeof(line) - 1);
    if (!len) continue;
    line[len++] = '\n';
    if (f.write(line, len) != len) {
      Serial.println(F("[LOG] SD write failed (card full?)."));
      break;
    }
    free_bytes = free_bytes > len ? free_bytes - len : 0;
    logged++;
  }
  f.close();

  Serial.print(F("[LOG] Logged "));
  Serial.print(logged);
  Serial.println(F(" readings to SD."));
}

void node_log_sync(JsonDocument &command, PacketSender &sender) {
  uint32_t gen, offset;
  read_cursor(&gen, &offset);
  if (!sd_ok) {
    send_se(sender, gen, 0, 0, false);
    return;
  }
  // Only ever move forward: a request carrying an older offset (e.g. the
  // radio side catching up after a USB sync went further) resumes from the
  // saved cursor instead of resending what's already been stored.
  if (command["g"].as<long>() == (long)gen && command["o"].is<uint32_t>()
      && command["o"].as<uint32_t>() >= offset) {
    offset = command["o"].as<uint32_t>();
    write_cursor(gen, offset);
  }
  uint32_t k = command["k"] | 8;

  // Previous file fully confirmed by the Pi — delete it and move on.
  bool sending = sd.exists(SENDING_FILE);
  uint64_t size = sending ? file_size(SENDING_FILE) : 0;
  if (sending && offset >= size) {
    sd.remove(SENDING_FILE);
    free_bytes += size;
    sending = false;
    Serial.print(F("[SYNC] Generation "));
    Serial.print(gen);
    Serial.println(F(" fully synced."));
  }

  // Start a new generation from whatever has been logged since.
  if (!sending) {
    size = file_size(DATA_FILE);
    if (size == 0) {
      send_se(sender, gen, 0, 0, false);
      return;
    }
    if (!sd.rename(DATA_FILE, SENDING_FILE)) {
      Serial.println(F("[SYNC] Could not rename data.txt."));
      return;
    }
    gen++;
    offset = 0;
    write_cursor(gen, offset);
  }

  File32 f;
  if (!f.open(SENDING_FILE, O_RDONLY) || !f.seekSet(offset)) {
    Serial.println(F("[SYNC] Could not read sending.txt."));
    return;
  }
  char line[128], tagged[144];
  uint32_t sent = 0;   // lines in this chunk, whether or not they were asked for again
  while (sent < k) {
    int n = f.fgets(line, sizeof(line));
    if (n <= 0) break;
    offset += n;
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = '\0';
    if (n == 0) continue;
    if (line_wanted(command, sent)) {
      size_t len = tag_line(line, n, sent, tagged, sizeof(tagged));
      sender.send_raw((const uint8_t *)tagged, len);
      if (!sender.over_usb()) delay(SYNC_LINE_GAP_MS);
    }
    sent++;
  }
  f.close();

  bool more = offset < size || file_size(DATA_FILE) > 0;
  send_se(sender, gen, offset, sent, more);

  Serial.print(F("[SYNC] Sent "));
  Serial.print(sent);
  Serial.print(F(" lines (gen "));
  Serial.print(gen);
  Serial.print(F(", offset "));
  Serial.print(offset);
  Serial.print(F("/"));
  Serial.print((uint32_t)size);
  Serial.println(F(")."));
}

void node_log_info(PacketSender &sender) {
  if (!sd_ok) {
    send_info(sender, 0, 0, 0);
    return;
  }
  uint64_t total = (uint64_t)sd.clusterCount() * sd.bytesPerCluster();
  send_info(sender, file_size(DATA_FILE) + file_size(SENDING_FILE), free_bytes, total);
}


#elif LOG_BACKEND == LOG_FLASH_SAMD
// ── Internal flash ───────────────────────────────────────────────────────
// Record numbers stand in for byte offsets, and the log's format ID for the
// generation (see flash_log.h). Records hold the reading without "ts"; the
// time is stored separately and added back when the record is sent.

#include "flash_log.h"

bool node_log_init() { return log_init(); }

void node_log_snapshot(uint32_t epoch) {
  if (latest_count == 0 || log_capacity() == 0) return;
  size_t logged = 0;
  for (size_t i = 0; i < latest_count; i++) {
    char buf[LOG_JSON_MAX + 1];
    size_t len = reading_json(i, nullptr, buf, sizeof(buf));
    if (len && log_append(epoch, buf, len)) logged++;
  }
  Serial.print(F("[LOG] Logged "));
  Serial.print(logged);
  Serial.print(F(" readings ("));
  Serial.print(log_head() - log_tail());
  Serial.print(F("/"));
  Serial.print(log_capacity());
  Serial.println(F(" records waiting)."));
}

void node_log_sync(JsonDocument &command, PacketSender &sender) {
  uint16_t g = log_format_id();
  if (command["g"].as<long>() == g && command["o"].is<uint32_t>()) {
    log_confirm(command["o"].as<uint32_t>());
  }
  uint32_t k = command["k"] | 8;

  uint32_t i = log_tail(), sent = 0, epoch;
  char json[LOG_JSON_MAX + 1], ts[20], line[LOG_JSON_MAX + 32], tagged[LOG_JSON_MAX + 48];
  while (sent < k && i < log_head()) {
    if (log_read(i, &epoch, json)) {
      if (line_wanted(command, sent)) {
        format_iso(epoch, ts);
        int len = snprintf(line, sizeof(line), "%.*s,\"ts\":\"%s\"}",
                           (int)strlen(json) - 1, json, ts);
        len = tag_line(line, len, sent, tagged, sizeof(tagged));
        sender.send_raw((const uint8_t *)tagged, len);
        if (!sender.over_usb()) delay(SYNC_LINE_GAP_MS);
      }
      sent++;
    }
    i++;
  }
  send_se(sender, g, i, sent, i < log_head());

  Serial.print(F("[SYNC] Sent "));
  Serial.print(sent);
  Serial.print(F(" records ("));
  Serial.print(log_head() - i);
  Serial.println(F(" left)."));
}

void node_log_info(PacketSender &sender) {
  uint32_t rb = log_record_bytes();
  uint32_t used = (log_head() - log_tail()) * rb;
  uint32_t total = log_capacity() * rb;
  send_info(sender, used, total - used, total);
}


#else
// ── No storage ───────────────────────────────────────────────────────────

bool node_log_init() { return false; }
void node_log_snapshot(uint32_t) {}
void node_log_sync(JsonDocument &, PacketSender &sender) { send_se(sender, 0, 0, 0, false); }
void node_log_info(PacketSender &sender) { send_info(sender, 0, 0, 0); }

#endif
