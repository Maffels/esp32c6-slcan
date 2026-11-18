// ESP32-C6 SLCAN adapter: Lawicel over USB-Serial/JTAG <-> TWAI (MCP2562, TX=GPIO1, RX=GPIO2).
// ESP-IDF 6.1.1, on-chip TWAI node driver.
//
// Data paths (no task blocks the other direction):
//   CAN RX ISR -> 1024-frame ring -> writer task -> batched lines -> USB TX ring (16 KB)
//   USB RX ring (16 KB) -> reader task -> strict parse -> 1024-slot TX pool -> TWAI TX queue
// Every t/T/r/R gets z / Z on success or BEL on failure (closed, bad line, TX pool full).
// Losses are counted, never silent: '?' prints counters, 'F' the Lawicel status flags.
// Bus-off: the node is closed and reopened automatically.

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_twai.h"
#include "esp_twai_onchip.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "slcan_proto.h"

static const char *TAG = "slcan";

#define CAN_TX_GPIO GPIO_NUM_1
#define CAN_RX_GPIO GPIO_NUM_2

#define FW_VERSION_REPLY "V0200\r"  // Lawicel Vhhss: hw 02, sw 00 (IDF port)
#define FW_SERIAL_REPLY "NC6SL\r"

#define RX_RING_LEN 1024u  // power of two
#define RX_RING_MASK (RX_RING_LEN - 1u)
// TX: the reader must never block. The USB-Serial/JTAG driver drops whole USB packets (one
// pyserial write each, so whole commands) when its RX ring is full, without counting them.
// A deep TX queue absorbs bursts (1024 frames = ~280 ms at 500 kbit/s, 8-byte extended);
// beyond that a command is refused at once with BEL and counted in tx_nack.
#define TX_POOL_LEN 1024
#define USB_TX_BUF 16384
#define USB_RX_BUF 16384
#define USB_WRITE_WAIT_MS 2
#define BUS_OFF_REOPEN_MS 100

// Lawicel F flags
#define F_RX_FULL 0x01
#define F_TX_FULL 0x02
#define F_ERR_WARN 0x04
#define F_OVERRUN 0x08
#define F_ERR_PASSIVE 0x20
#define F_BUS_ERR 0x80

typedef struct {
    uint64_t ts_us;
    uint32_t id;
    uint8_t dlc;
    uint8_t ext;
    uint8_t rtr;
    uint8_t data[8];
} rx_item_t;

typedef struct {
    twai_frame_t frame;  // first member: done_tx_frame casts back to the slot
    uint8_t data[8];
} tx_slot_t;

static rx_item_t s_rx[RX_RING_LEN];
static atomic_uint s_rx_head;  // written by the RX ISR
static atomic_uint s_rx_tail;  // written by the writer task

static tx_slot_t s_tx[TX_POOL_LEN];
static QueueHandle_t s_tx_free;  // tx_slot_t *

static twai_node_handle_t s_node;
static SemaphoreHandle_t s_lock;  // node lifecycle + transmit
static volatile bool s_open;
static volatile bool s_listen;
static volatile bool s_timestamps;
static uint32_t s_bitrate = 500000;
static TaskHandle_t s_writer;
static TaskHandle_t s_house;

static struct {
    atomic_uint rx;          // frames sent to the host
    atomic_uint rx_overrun;  // CAN frames lost: RX ring full
    atomic_uint usb_drop;    // CAN frames lost: USB TX ring full (host not reading)
    atomic_uint tx_ok;
    atomic_uint tx_fail;  // TWAI reported a failed transmission
    atomic_uint tx_nack;  // command refused: closed / pool full / driver error
    atomic_uint cmd_err;  // malformed line
    atomic_uint bus_err;
    atomic_uint bus_off;
    atomic_uint flags;  // Lawicel F, cleared on read
    atomic_int state;   // twai_error_state_t
} st;

// ------------------------------------------------------------------ TWAI callbacks (ISR)

static bool IRAM_ATTR on_rx_done(twai_node_handle_t h, const twai_rx_done_event_data_t *e, void *u)
{
    (void)e;
    (void)u;
    static uint8_t discard[8];
    uint32_t head = atomic_load_explicit(&s_rx_head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&s_rx_tail, memory_order_acquire);
    twai_frame_t f = {0};
    if (head - tail >= RX_RING_LEN) {
        // The driver needs the frame taken; drop it into a scratch buffer and count it.
        f.buffer = discard;
        f.buffer_len = sizeof(discard);
        twai_node_receive_from_isr(h, &f);
        atomic_fetch_add(&st.rx_overrun, 1);
        atomic_fetch_or(&st.flags, F_RX_FULL | F_OVERRUN);
        return false;
    }
    rx_item_t *it = &s_rx[head & RX_RING_MASK];
    f.buffer = it->data;
    f.buffer_len = sizeof(it->data);
    if (twai_node_receive_from_isr(h, &f) != ESP_OK) {
        return false;
    }
    it->ts_us = f.header.timestamp;
    it->id = f.header.id;
    it->dlc = f.header.dlc > 8 ? 8 : (uint8_t)f.header.dlc;
    it->ext = f.header.ide;
    it->rtr = f.header.rtr;
    atomic_store_explicit(&s_rx_head, head + 1, memory_order_release);
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_writer, &woken);
    return woken == pdTRUE;
}

static bool IRAM_ATTR on_tx_done(twai_node_handle_t h, const twai_tx_done_event_data_t *e, void *u)
{
    (void)h;
    (void)u;
    atomic_fetch_add(e->is_tx_success ? &st.tx_ok : &st.tx_fail, 1);
    tx_slot_t *slot = (tx_slot_t *)e->done_tx_frame;
    BaseType_t woken = pdFALSE;
    xQueueSendFromISR(s_tx_free, &slot, &woken);
    return woken == pdTRUE;
}

static bool IRAM_ATTR on_error(twai_node_handle_t h, const twai_error_event_data_t *e, void *u)
{
    (void)h;
    (void)u;
    // 0x1 arbitration lost (normal CSMA), 0x2 bit, 0x4 form, 0x8 stuff, 0x10 ack
    if (e && (e->err_flags.val & 0x1Eu)) {
        atomic_fetch_add(&st.bus_err, 1);
        atomic_fetch_or(&st.flags, F_BUS_ERR);
    }
    return false;
}

static bool IRAM_ATTR on_state_change(twai_node_handle_t h, const twai_state_change_event_data_t *e, void *u)
{
    (void)h;
    (void)u;
    BaseType_t woken = pdFALSE;
    atomic_store(&st.state, (int)e->new_sta);
    if (e->new_sta == TWAI_ERROR_WARNING) {
        atomic_fetch_or(&st.flags, F_ERR_WARN);
    } else if (e->new_sta == TWAI_ERROR_PASSIVE) {
        atomic_fetch_or(&st.flags, F_ERR_PASSIVE);
    } else if (e->new_sta == TWAI_ERROR_BUS_OFF) {
        atomic_fetch_add(&st.bus_off, 1);
        atomic_fetch_or(&st.flags, F_BUS_ERR);
        vTaskNotifyGiveFromISR(s_house, &woken);
    }
    return woken == pdTRUE;
}

// ------------------------------------------------------------------ node lifecycle (hold s_lock)

static void tx_pool_reset(void)
{
    xQueueReset(s_tx_free);
    for (int i = 0; i < TX_POOL_LEN; i++) {
        tx_slot_t *slot = &s_tx[i];
        slot->frame.buffer = slot->data;
        xQueueSend(s_tx_free, &slot, 0);
    }
}

static esp_err_t node_open(bool listen)
{
    twai_onchip_node_config_t cfg = {
        .io_cfg = {
            .tx = CAN_TX_GPIO,
            .rx = CAN_RX_GPIO,
            .quanta_clk_out = GPIO_NUM_NC,
            .bus_off_indicator = GPIO_NUM_NC,
        },
        .bit_timing = {
            .bitrate = s_bitrate,
            .sp_permill = s_bitrate > 500000 ? 800 : 875,  // J1939-14: 87.5 %
        },
        .timestamp_resolution_hz = 1000000,
        .fail_retry_cnt = -1,  // standard CAN: retransmit until acked (bus-off handled below)
        .tx_queue_depth = TX_POOL_LEN,
        .flags.enable_listen_only = listen,
    };
    esp_err_t err = twai_new_node_onchip(&cfg, &s_node);
    if (err != ESP_OK) {
        s_node = NULL;
        return err;
    }
    twai_event_callbacks_t cbs = {
        .on_rx_done = on_rx_done,
        .on_tx_done = on_tx_done,
        .on_error = on_error,
        .on_state_change = on_state_change,
    };
    err = twai_node_register_event_callbacks(s_node, &cbs, NULL);
    if (err == ESP_OK) {
        tx_pool_reset();
        atomic_store(&st.state, TWAI_ERROR_ACTIVE);
        err = twai_node_enable(s_node);
    }
    if (err != ESP_OK) {
        twai_node_delete(s_node);
        s_node = NULL;
        return err;
    }
    s_listen = listen;
    s_open = true;
    return ESP_OK;
}

static void node_close(void)
{
    s_open = false;
    if (s_node) {
        twai_node_disable(s_node);
        twai_node_delete(s_node);
        s_node = NULL;
    }
}

static bool node_transmit(const slcan_frame_t *f)
{
    if (!s_open || s_listen || !s_node) {
        return false;
    }
    tx_slot_t *slot;
    if (xQueueReceive(s_tx_free, &slot, 0) != pdTRUE) {
        atomic_fetch_or(&st.flags, F_TX_FULL);
        return false;
    }
    memset(&slot->frame.header, 0, sizeof(slot->frame.header));
    slot->frame.header.id = f->id;
    slot->frame.header.dlc = f->dlc;
    slot->frame.header.ide = f->ext;
    slot->frame.header.rtr = f->rtr;
    memcpy(slot->data, f->data, sizeof(slot->data));
    slot->frame.buffer = slot->data;
    slot->frame.buffer_len = f->rtr ? 0 : f->dlc;
    slot->frame.tx_queue_priority = 0;  // FIFO
    if (twai_node_transmit(s_node, &slot->frame, 0) != ESP_OK) {
        xQueueSend(s_tx_free, &slot, 0);
        return false;
    }
    return true;
}

// ------------------------------------------------------------------ USB output

static void usb_out(const char *s, size_t n)
{
    if (n) {
        usb_serial_jtag_write_bytes(s, n, pdMS_TO_TICKS(USB_WRITE_WAIT_MS));
    }
}

// CAN -> host. Drains the whole ring per wake-up into one buffer per USB write.
static void writer_task(void *arg)
{
    (void)arg;
    static char out[2048];
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
        size_t n = 0;
        unsigned lines = 0;
        for (;;) {
            uint32_t tail = atomic_load_explicit(&s_rx_tail, memory_order_relaxed);
            uint32_t head = atomic_load_explicit(&s_rx_head, memory_order_acquire);
            if (tail == head) {
                break;
            }
            if (!s_open) {
                atomic_store_explicit(&s_rx_tail, head, memory_order_release);
                break;
            }
            const rx_item_t *it = &s_rx[tail & RX_RING_MASK];
            slcan_frame_t f = {.id = it->id, .dlc = it->dlc, .ext = it->ext, .rtr = it->rtr};
            memcpy(f.data, it->data, sizeof(f.data));
            int32_t ts = s_timestamps ? (int32_t)((it->ts_us / 1000u) % 60000u) : -1;
            atomic_store_explicit(&s_rx_tail, tail + 1, memory_order_release);
            n += slcan_format_frame(&f, ts, out + n);
            lines++;
            if (n > sizeof(out) - SLCAN_MAX_LINE) {
                break;  // write this chunk, then continue draining
            }
        }
        if (!n) {
            continue;
        }
        if (usb_serial_jtag_write_bytes(out, n, pdMS_TO_TICKS(USB_WRITE_WAIT_MS)) == (int)n) {
            atomic_fetch_add(&st.rx, lines);
        } else {
            atomic_fetch_add(&st.usb_drop, lines);
            atomic_fetch_or(&st.flags, F_OVERRUN);
        }
        if (atomic_load(&s_rx_head) != atomic_load(&s_rx_tail)) {
            xTaskNotifyGive(xTaskGetCurrentTaskHandle());
        }
    }
}

// ------------------------------------------------------------------ commands

typedef struct {
    char buf[1024];
    size_t n;
} reply_t;

static void reply(reply_t *r, const char *s)
{
    size_t len = strlen(s);
    if (r->n + len > sizeof(r->buf)) {
        usb_out(r->buf, r->n);
        r->n = 0;
    }
    memcpy(r->buf + r->n, s, len);
    r->n += len;
}

#define ACK "\r"
#define NACK "\a"

static void stats_line(reply_t *r)
{
    char s[256];
    snprintf(s, sizeof(s),
             "?rx=%u,rx_overrun=%u,usb_drop=%u,tx_ok=%u,tx_fail=%u,tx_nack=%u,cmd_err=%u,"
             "bus_err=%u,bus_off=%u,state=%d,open=%d,bitrate=%lu\r",
             atomic_load(&st.rx), atomic_load(&st.rx_overrun), atomic_load(&st.usb_drop),
             atomic_load(&st.tx_ok), atomic_load(&st.tx_fail), atomic_load(&st.tx_nack),
             atomic_load(&st.cmd_err), atomic_load(&st.bus_err), atomic_load(&st.bus_off),
             atomic_load(&st.state), s_open ? (s_listen ? 2 : 1) : 0, (unsigned long)s_bitrate);
    reply(r, s);
}

static void stats_clear(void)
{
    atomic_store(&st.rx, 0);
    atomic_store(&st.rx_overrun, 0);
    atomic_store(&st.usb_drop, 0);
    atomic_store(&st.tx_ok, 0);
    atomic_store(&st.tx_fail, 0);
    atomic_store(&st.tx_nack, 0);
    atomic_store(&st.cmd_err, 0);
    atomic_store(&st.bus_err, 0);
    atomic_store(&st.bus_off, 0);
    atomic_store(&st.flags, 0);
}

static void handle_line(const char *line, size_t len, reply_t *r)
{
    if (len == 0) {
        reply(r, ACK);  // python-can sends an empty line for "no data bitrate"
        return;
    }
    char tmp[8];
    switch (line[0]) {
    case 't':
    case 'T':
    case 'r':
    case 'R': {
        slcan_frame_t f;
        if (!slcan_parse_frame(line, len, &f)) {
            atomic_fetch_add(&st.cmd_err, 1);
            reply(r, NACK);
            return;
        }
        xSemaphoreTake(s_lock, portMAX_DELAY);
        bool ok = node_transmit(&f);
        xSemaphoreGive(s_lock);
        if (!ok) {
            atomic_fetch_add(&st.tx_nack, 1);
            reply(r, NACK);
        } else {
            reply(r, f.ext ? "Z\r" : "z\r");
        }
        return;
    }
    case 'S': {
        uint32_t br = len == 2 ? slcan_bitrate_from_code(line[1]) : 0;
        if (!br || s_open) {
            reply(r, NACK);
        } else {
            s_bitrate = br;
            reply(r, ACK);
        }
        return;
    }
    case 'O':
    case 'L': {
        bool listen = line[0] == 'L';
        xSemaphoreTake(s_lock, portMAX_DELAY);
        esp_err_t err = ESP_OK;
        if (s_open && s_listen != listen) {
            err = ESP_ERR_INVALID_STATE;
        } else if (!s_open) {
            err = node_open(listen);
        }
        xSemaphoreGive(s_lock);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "open failed: %s", esp_err_to_name(err));
        }
        reply(r, err == ESP_OK ? ACK : NACK);
        return;
    }
    case 'C':
        xSemaphoreTake(s_lock, portMAX_DELAY);
        node_close();
        xSemaphoreGive(s_lock);
        reply(r, ACK);
        return;
    case 'Z':
        if (len == 2 && (line[1] == '0' || line[1] == '1')) {
            s_timestamps = line[1] == '1';
            reply(r, ACK);
        } else {
            reply(r, NACK);
        }
        return;
    case 'F':
        snprintf(tmp, sizeof(tmp), "F%02X\r", (unsigned)(atomic_exchange(&st.flags, 0) & 0xFF));
        reply(r, tmp);
        return;
    case 'V':
        reply(r, FW_VERSION_REPLY);
        return;
    case 'N':
        reply(r, FW_SERIAL_REPLY);
        return;
    case 'M':
    case 'm':
        reply(r, ACK);  // acceptance filter: always accept all
        return;
    case '?':
        stats_line(r);
        if (len == 2 && line[1] == 'C') {
            stats_clear();
        }
        return;
    default:
        atomic_fetch_add(&st.cmd_err, 1);
        reply(r, NACK);
        return;
    }
}

// host -> CAN
static void reader_task(void *arg)
{
    (void)arg;
    static uint8_t in[512];
    static char line[64];
    static reply_t r;
    size_t ll = 0;
    bool overflow = false;
    for (;;) {
        int n = usb_serial_jtag_read_bytes(in, sizeof(in), portMAX_DELAY);
        for (int i = 0; i < n; i++) {
            char c = (char)in[i];
            if (c == '\r') {
                if (overflow) {
                    atomic_fetch_add(&st.cmd_err, 1);
                    reply(&r, NACK);
                } else {
                    handle_line(line, ll, &r);
                }
                ll = 0;
                overflow = false;
            } else if (c == '\n') {
                continue;
            } else if (ll < sizeof(line) - 1) {
                line[ll++] = c;
            } else {
                overflow = true;
            }
        }
        usb_out(r.buf, r.n);
        r.n = 0;
    }
}

// Bus-off: close and reopen the node (also rebuilds the TX pool).
static void house_task(void *arg)
{
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        vTaskDelay(pdMS_TO_TICKS(BUS_OFF_REOPEN_MS));
        xSemaphoreTake(s_lock, portMAX_DELAY);
        if (s_open) {
            bool listen = s_listen;
            node_close();
            esp_err_t err = node_open(listen);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "reopen after bus-off failed: %s", esp_err_to_name(err));
            }
        }
        xSemaphoreGive(s_lock);
    }
}

void app_main(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_tx_free = xQueueCreate(TX_POOL_LEN, sizeof(tx_slot_t *));
    usb_serial_jtag_driver_config_t usb = {
        .tx_buffer_size = USB_TX_BUF,
        .rx_buffer_size = USB_RX_BUF,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    xTaskCreate(writer_task, "slcan_wr", 4096, NULL, 12, &s_writer);
    xTaskCreate(reader_task, "slcan_rd", 4096, NULL, 11, NULL);
    xTaskCreate(house_task, "slcan_hk", 3072, NULL, 5, &s_house);
    ESP_LOGW(TAG, "SLCAN ready (TWAI TX=%d RX=%d), waiting for O", CAN_TX_GPIO, CAN_RX_GPIO);
}
