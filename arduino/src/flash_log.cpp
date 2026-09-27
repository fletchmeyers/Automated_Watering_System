#include "board_config.h"
#include "flash_log.h"

#if LOG_BACKEND == LOG_FLASH_SAMD

/*
 * SAMD21 internal flash backend.
 *
 * The log area is the top LOG_FLASH_BYTES of flash. Firmware uploads only
 * write as far as the firmware image, so the log survives reflashing.
 *
 * Flash is erased a 256-byte row at a time and written a 64-byte page at a
 * time, so each record is exactly one page and a page is only ever written
 * once between erases. The first row holds a header (magic + format ID);
 * every other row holds 4 records. Records are placed round-robin by record
 * number, and a row is erased just before its first record is written —
 * which is what overwrites the oldest records once the log is full.
 *
 * Each record stores its own number, so after a reset the newest and oldest
 * records are found by scanning. Which records the Pi has already confirmed
 * is only kept in RAM: after a reset the log resumes from the oldest record
 * still present, and the Pi's cursor (if the Pi hasn't restarted too) moves
 * it forward again on the next sync.
 */

extern uint32_t __etext, __data_start__, __data_end__;

static const uint32_t FLASH_BYTES = 0x40000;
static const uint32_t ROW_BYTES   = 256;
static const uint32_t PAGE_BYTES  = 64;
static const uint32_t LOG_START   = FLASH_BYTES - LOG_FLASH_BYTES;
static const uint32_t SLOTS_START = LOG_START + ROW_BYTES;   // first row is the header
static const uint32_t SLOT_COUNT  = (LOG_FLASH_BYTES - ROW_BYTES) / PAGE_BYTES;
static const uint32_t LOG_MAGIC   = 0x31474F4C;               // "LOG1"
static const uint32_t EMPTY       = 0xFFFFFFFF;               // erased flash

static_assert(LOG_FLASH_BYTES % ROW_BYTES == 0, "LOG_FLASH_BYTES must be a whole number of 256-byte rows");

struct Header {
  uint32_t magic;
  uint32_t format_id;
  uint32_t unused[14];
};

struct Record {
  uint32_t index;
  uint32_t epoch;
  uint8_t  len;
  char     json[LOG_JSON_MAX];
};

static_assert(sizeof(Header) == PAGE_BYTES, "header must fill one flash page");
static_assert(sizeof(Record) == PAGE_BYTES, "record must fill one flash page");

static bool     ready = false;
static uint32_t head = 0, tail = 0;
static uint16_t format_id = 0;

static const Record *slot(uint32_t index) {
  return (const Record *)(SLOTS_START + (index % SLOT_COUNT) * PAGE_BYTES);
}

static void nvm_command(uint32_t cmd) {
  NVMCTRL->CTRLA.reg = NVMCTRL_CTRLA_CMDEX_KEY | cmd;
  while (!NVMCTRL->INTFLAG.bit.READY) {}
}

static void erase_row(uint32_t addr) {
  NVMCTRL->ADDR.reg = addr / 2;   // ADDR is in 16-bit words
  nvm_command(NVMCTRL_CTRLA_CMD_ER);
}

static void write_page(uint32_t addr, const void *data) {
  NVMCTRL->CTRLB.bit.MANW = 1;     // commit only on the explicit WP below
  nvm_command(NVMCTRL_CTRLA_CMD_PBC);
  volatile uint32_t *dst = (volatile uint32_t *)addr;
  const uint32_t *src = (const uint32_t *)data;
  for (uint32_t i = 0; i < PAGE_BYTES / 4; i++) dst[i] = src[i];
  nvm_command(NVMCTRL_CTRLA_CMD_WP);
}

static bool record_valid(const Record *r) {
  return r->index != EMPTY && r->len > 0 && r->len <= LOG_JSON_MAX && r->json[0] == '{';
}

static void format() {
  for (uint32_t addr = LOG_START; addr < FLASH_BYTES; addr += ROW_BYTES) erase_row(addr);
  Header h;
  memset(&h, 0xFF, sizeof(h));
  h.magic = LOG_MAGIC;
  // Only needs to differ from the previous format so the Pi drops any cursor
  // it held for the old log — ADC noise plus boot timing is plenty for that.
  h.format_id = ((micros() ^ ((uint32_t)analogRead(A0) << 5) ^ 0x5A5A) & 0xFFFF) | 1;
  write_page(LOG_START, &h);
  Serial.print(F("[LOG] Formatted log area, id "));
  Serial.println(h.format_id);
}

bool log_init() {
  uint32_t image_end = (uint32_t)&__etext + ((uint32_t)&__data_end__ - (uint32_t)&__data_start__);
  if (image_end > LOG_START) {
    Serial.println(F("[LOG] Firmware overlaps the log area — shrink LOG_FLASH_BYTES. Logging disabled."));
    return false;
  }

  const Header *h = (const Header *)LOG_START;
  if (h->magic != LOG_MAGIC) format();
  format_id = h->format_id;

  bool any = false;
  uint32_t lo = 0, hi = 0;
  for (uint32_t s = 0; s < SLOT_COUNT; s++) {
    const Record *r = (const Record *)(SLOTS_START + s * PAGE_BYTES);
    if (!record_valid(r)) continue;
    if (!any || r->index < lo) lo = r->index;
    if (!any || r->index > hi) hi = r->index;
    any = true;
  }
  tail = any ? lo : 0;
  head = any ? hi + 1 : 0;
  ready = true;

  Serial.print(F("[LOG] "));
  Serial.print(head - tail);
  Serial.print(F(" of "));
  Serial.print(SLOT_COUNT);
  Serial.println(F(" records in use."));
  return true;
}

bool log_append(uint32_t epoch, const char *json, uint8_t len) {
  if (!ready || len == 0 || len > LOG_JSON_MAX) return false;

  uint32_t addr = (uint32_t)slot(head);
  // Erase the row on entering it. A non-blank slot mid-row only happens if
  // a write was interrupted — erase then too, losing at most 3 records.
  if ((head % 4) == 0 || slot(head)->index != EMPTY) {
    erase_row(addr - (addr - SLOTS_START) % ROW_BYTES);
  }

  Record r;
  memset(&r, 0xFF, sizeof(r));
  r.index = head;
  r.epoch = epoch;
  r.len   = len;
  memcpy(r.json, json, len);
  write_page(addr, &r);
  head++;

  // Anything older than the row just erased is gone.
  uint32_t oldest = head > SLOT_COUNT ? head - SLOT_COUNT + (4 - head % 4) % 4 : 0;
  if (tail < oldest) tail = oldest;
  return true;
}

bool log_read(uint32_t index, uint32_t *epoch, char *json) {
  if (!ready || index >= head) return false;
  const Record *r = slot(index);
  if (!record_valid(r) || r->index != index) return false;
  *epoch = r->epoch;
  memcpy(json, r->json, r->len);
  json[r->len] = '\0';
  return true;
}

void log_confirm(uint32_t index) {
  if (index > head) index = head;
  if (index > tail) tail = index;
}

uint32_t log_tail()         { return tail; }
uint32_t log_head()         { return head; }
uint32_t log_capacity()     { return ready ? SLOT_COUNT : 0; }
uint32_t log_record_bytes() { return PAGE_BYTES; }
uint16_t log_format_id()    { return format_id; }

#else  // LOG_NONE

bool log_init()                                   { return false; }
bool log_append(uint32_t, const char *, uint8_t)  { return false; }
bool log_read(uint32_t, uint32_t *, char *)       { return false; }
void log_confirm(uint32_t)                        {}
uint32_t log_tail()                               { return 0; }
uint32_t log_head()                               { return 0; }
uint32_t log_capacity()                           { return 0; }
uint32_t log_record_bytes()                       { return 0; }
uint16_t log_format_id()                          { return 0; }

#endif
