// Lawicel SLCAN line parsing / formatting. Pure C, no IDF calls (host-testable).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t id;
    uint8_t dlc;  // 0..8
    bool ext;
    bool rtr;
    uint8_t data[8];
} slcan_frame_t;

// Longest line we emit: 'T' + 8 id + 1 dlc + 16 data + 4 timestamp + '\r' = 31
#define SLCAN_MAX_LINE 32

/**
 * Parse a t/T/r/R command (without the trailing CR).
 * Strict: exact length for the DLC, hex only, id in range, DLC 0..8.
 * @return true if valid.
 */
bool slcan_parse_frame(const char *line, size_t len, slcan_frame_t *out);

/**
 * Format a received frame as an SLCAN line incl. trailing CR.
 * @param ts_ms  Lawicel timestamp (0..59999) or <0 for none.
 * @return number of bytes written (<= SLCAN_MAX_LINE).
 */
size_t slcan_format_frame(const slcan_frame_t *f, int32_t ts_ms, char *out);

/** S0..S8 -> bit/s, or 0 if invalid. */
uint32_t slcan_bitrate_from_code(char code);

#ifdef __cplusplus
}
#endif
