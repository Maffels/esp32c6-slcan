#include "slcan_proto.h"

static const char HEX[16] = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B', 'C', 'D', 'E', 'F'};

static int hexval(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    return -1;
}

static bool parse_hex(const char *s, size_t n, uint32_t *out)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++) {
        int h = hexval(s[i]);
        if (h < 0) {
            return false;
        }
        v = (v << 4) | (uint32_t)h;
    }
    *out = v;
    return true;
}

bool slcan_parse_frame(const char *line, size_t len, slcan_frame_t *out)
{
    if (len < 1) {
        return false;
    }
    char c = line[0];
    bool ext = (c == 'T' || c == 'R');
    bool rtr = (c == 'r' || c == 'R');
    if (c != 't' && c != 'T' && c != 'r' && c != 'R') {
        return false;
    }
    size_t id_len = ext ? 8 : 3;
    if (len < 1 + id_len + 1) {
        return false;
    }
    uint32_t id, dlc;
    if (!parse_hex(line + 1, id_len, &id) || !parse_hex(line + 1 + id_len, 1, &dlc)) {
        return false;
    }
    if (dlc > 8 || id > (ext ? 0x1FFFFFFFu : 0x7FFu)) {
        return false;
    }
    size_t data_off = 1 + id_len + 1;
    size_t want = data_off + (rtr ? 0 : 2 * dlc);
    if (len != want) {
        return false;
    }
    out->id = id;
    out->dlc = (uint8_t)dlc;
    out->ext = ext;
    out->rtr = rtr;
    for (uint32_t i = 0; i < 8; i++) {
        out->data[i] = 0;
    }
    if (!rtr) {
        for (uint32_t i = 0; i < dlc; i++) {
            uint32_t b;
            if (!parse_hex(line + data_off + 2 * i, 2, &b)) {
                return false;
            }
            out->data[i] = (uint8_t)b;
        }
    }
    return true;
}

size_t slcan_format_frame(const slcan_frame_t *f, int32_t ts_ms, char *out)
{
    size_t n = 0;
    uint8_t dlc = f->dlc > 8 ? 8 : f->dlc;
    if (f->ext) {
        out[n++] = f->rtr ? 'R' : 'T';
        for (int s = 28; s >= 0; s -= 4) {
            out[n++] = HEX[(f->id >> s) & 0xF];
        }
    } else {
        out[n++] = f->rtr ? 'r' : 't';
        for (int s = 8; s >= 0; s -= 4) {
            out[n++] = HEX[(f->id >> s) & 0xF];
        }
    }
    out[n++] = HEX[dlc];
    if (!f->rtr) {
        for (uint8_t i = 0; i < dlc; i++) {
            out[n++] = HEX[f->data[i] >> 4];
            out[n++] = HEX[f->data[i] & 0xF];
        }
    }
    if (ts_ms >= 0) {
        uint32_t t = (uint32_t)ts_ms;
        for (int s = 12; s >= 0; s -= 4) {
            out[n++] = HEX[(t >> s) & 0xF];
        }
    }
    out[n++] = '\r';
    return n;
}

uint32_t slcan_bitrate_from_code(char code)
{
    switch (code) {
    case '0': return 10000;
    case '1': return 20000;
    case '2': return 50000;
    case '3': return 100000;
    case '4': return 125000;
    case '5': return 250000;
    case '6': return 500000;
    case '7': return 800000;
    case '8': return 1000000;
    default: return 0;
    }
}
