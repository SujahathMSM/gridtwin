/*
 * GridTwin-HIL firmware for NUCLEO-G491RE
 *
 * What it does
 *   1. Emulates a 4-cell LiFePO4 battery (100 Ah) with a simple physics model.
 *   2. Serves it as a Modbus RTU slave (address 1) on USART1 (PC4 TX / PC5 RX),
 *      wired to the Raspberry Pi's UART.
 *   3. Emulates the battery's contactor interlock, driven by the controller:
 *        legacy    PB4 high = trip (the original contract, rounds 1-5)
 *        failsafe  PB4 high = permissive ("battery allowed"); low, a cut wire or
 *                  a dead Pi = trip. PB5 carries a heartbeat: no edge for 250 ms
 *                  opens the interlock (a hung or killed controller trips).
 *   4. Timestamps (1 us resolution, TIM2) the moment a cell crosses the limit,
 *      the first Modbus reply that reported it, and the moment the interlock
 *      opened, then reports the latency over the ST-Link USB serial (LPUART1).
 *   5. A fault library the agents can drive: stale data, late, lost, corrupted
 *      and burst-lost replies, and a cut trip wire.
 *
 * CubeMX peripherals expected (see README.md):
 *   LPUART1  115200 8N1, PA2/PA3 (ST-Link virtual COM), global interrupt ON
 *   USART1   9600 8N1,   PC4/PC5,                      global interrupt ON
 *   TIM2     internal clock, period 0xFFFFFFFF (prescaler is set here at runtime)
 *   ADC1     IN1 single-ended on PA0
 *   PB4      GPIO_EXTI4, pull-down, EXTI line[4] interrupt ON (both edges set here)
 *   PB5      heartbeat input, configured here (pull-down), no CubeMX change needed
 *   PA5      GPIO output (on-board LED LD2, heartbeat)
 */
#include "main.h"
#include "gridtwin_app.h"

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern UART_HandleTypeDef huart1;   /* Pi link: Modbus RTU            */
extern UART_HandleTypeDef hlpuart1; /* ST-Link VCP: laptop monitor    */
extern TIM_HandleTypeDef htim2;     /* free-running 1 MHz time base   */
extern ADC_HandleTypeDef hadc1;     /* potentiometer on PA0           */

/* ------------------------------------------------------------------ */
/* Configuration                                                      */
/* ------------------------------------------------------------------ */
#define FW_VERSION         "gridtwin-2"
#define MODBUS_ADDR        1u
#define NUM_CELLS          4
#define NUM_REGS           12u
#define REG_CHARGE_LIMIT   11u      /* the only register the Pi may write */

#define CAPACITY_AH        100.0f
#define CELL_R_OHM         0.001f   /* internal resistance per cell        */
#define THERMAL_C_J_PER_K  2000.0f
#define THERMAL_K_W_PER_K  2.0f
#define AMBIENT_C          30.0f
#define TIME_SCALE         60.0f    /* 1 s real time = 60 s simulated      */
#define PHYS_STEP_MS       100u
#define TELEMETRY_MS       500u
#define POT_MAX_DRIFT_MV   400      /* pot full-scale adds this to cell 4  */
#define REARM_HYST_MV      50       /* re-arm when all cells < limit - this */
#define NO_TRIP_TIMEOUT_MS 3000u
#define HB_TIMEOUT_MS      250u     /* failsafe: no heartbeat edge this long = trip */

/* Modbus register map (holding registers, function 0x03 / 0x06)
 *   0..3  cell 1..4 voltage            mV
 *   4     pack voltage                 10 mV
 *   5     pack current (+ = charging)  0.1 A, signed
 *   6     temperature                  0.1 degC
 *   7     state of charge              0.1 %
 *   8     grid voltage                 V
 *   9     status bits: b0 interlock open (tripped), b1 drift from pot, b2 failsafe mode
 *   10    simulation tick (increments every 100 ms)
 *   11    charge current limit (RW)    0.1 A, default 1000 (100 A)
 */

/* ------------------------------------------------------------------ */
/* State                                                              */
/* ------------------------------------------------------------------ */
typedef struct {
    float soc;           /* 0..1 */
    float current_a;     /* + = charging */
    float temp_c;
    float drift_mv;      /* extra voltage on cell 4 */
    uint16_t cell_mv[NUM_CELLS];
    uint16_t grid_v;
    uint16_t tick;
} battery_t;

static battery_t bat = { .soc = 0.50f, .temp_c = AMBIENT_C, .grid_v = 230 };

static float    cmd_current_a  = 50.0f;  /* what the "charger" wants to push   */
static uint16_t charge_limit_da = 1000;  /* written by the Pi (reg 11)          */
static int      drift_from_pot = 0;   /* knob ignored until 'drift pot' (PA0 floats if unwired) */
static float    manual_drift_mv = 0.0f;
/* 'drift sync <mV>': apply the drift right after the next read reply has left
 * the wire, the worst moment for a polling controller (it has just looked, and
 * won't look again for a whole poll interval). Makes timing races reproducible. */
#define SYNC_TIMEOUT_MS    2000u
static uint8_t  sync_armed;
static float    sync_drift_mv;
static uint32_t sync_armed_ms;
static uint16_t limit_mv       = 3650;
static uint32_t deadline_ms    = 500;

/* Interlock (what the controller's trip wire drives) */
static uint8_t           ilk_failsafe;      /* 0 = legacy (high = trip), 1 = failsafe   */
static uint8_t           flt_cut;           /* fault: trip wire cut (reads low)         */
static uint32_t          hb_last_edge_us;   /* failsafe: last heartbeat edge on PB5     */
static uint8_t           hb_level;
static uint8_t           wd_open;           /* failsafe: heartbeat watchdog expired     */
static uint32_t          hb_edges;

/* trip timing (all in TIM2 microseconds) */
static volatile uint32_t trip_edge_us;
static volatile uint8_t  trip_edge_flag;
static volatile uint8_t  trip_edge_cause;   /* 0 wire, 1 heartbeat lost, 2 wire cut   */
static uint32_t cross_us;            /* 0 = no over-voltage pending */
static uint32_t first_served_us;     /* first reply carrying the bad value (first byte) */
static uint8_t  tripped;
static uint8_t  no_trip_reported;
static uint8_t  premature_reported;
static uint32_t trip_done_us;        /* when the trip for this over-voltage happened */
static uint32_t polls_served;

/* Modbus receive (USART1, byte-by-byte interrupt) */
static uint8_t           mb_rx_byte;
static uint8_t           mb_buf[64];
static volatile uint16_t mb_len;
static volatile uint32_t mb_last_rx_us;
static volatile uint8_t  mb_overflow;
static volatile uint8_t  mb_frame_bad;      /* framing/parity/overrun error inside this frame */
static uint32_t          mb_t35_us;

/* Modbus transmit (interrupt-driven: the main loop never waits for the wire) */
static uint8_t           mb_tx_buf[3 + 2 * NUM_REGS + 2];
static volatile uint8_t  mb_tx_busy;
static volatile uint8_t  mb_tx_done;        /* set by the TX-complete interrupt   */
static uint8_t           mb_tx_is_read;

/* Link-quality counters, reported in telemetry */
static volatile uint32_t mb_noise_errors;   /* NE: glitch on a bit, byte usually still fine   */
static volatile uint32_t mb_frame_errors;   /* FE/PE/ORE: byte corrupted or lost             */
static uint32_t          mb_crc_errors;     /* complete frame arrived but CRC didn't match    */
static uint32_t          mb_short_frames;   /* fragments under 4 bytes (frame cut too early)  */

/* Laptop command line (LPUART1) */
static uint8_t           cmd_rx_byte;
static char              cmd_line[64];
static volatile uint16_t cmd_len;
static volatile uint8_t  cmd_ready;

/* VCP output: ring buffer drained by interrupts, so printing never blocks */
#define VCP_RING 2048u
static char              vcp_ring[VCP_RING];
static volatile uint16_t vr_head, vr_tail, vr_chunk;
static volatile uint8_t  vcp_busy;
static uint32_t          vcp_dropped;

static uint32_t last_phys_ms, last_tele_ms;
static const char *boot_reason = "power_on";

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */
static inline uint32_t now_us(void) { return htim2.Instance->CNT; }

static void vcp_kick(void)
{
    __disable_irq();
    if (!vcp_busy && vr_head != vr_tail) {
        uint16_t end = (vr_head > vr_tail) ? vr_head : (uint16_t)VCP_RING;
        vr_chunk = (uint16_t)(end - vr_tail);
        vcp_busy = 1;
        __enable_irq();
        if (HAL_UART_Transmit_IT(&hlpuart1, (uint8_t *)&vcp_ring[vr_tail], vr_chunk) != HAL_OK)
            vcp_busy = 0;
        return;
    }
    __enable_irq();
}

/* Queue a message. Never waits: if the ring is full the message is dropped and
 * counted (important ones are short; telemetry is the bulk). */
static void vcp_printf(int important, const char *fmt, ...)
{
    char line[640];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    if (n >= (int)sizeof line) n = (int)sizeof line - 1;

    __disable_irq();
    uint16_t used = (uint16_t)((vr_head + VCP_RING - vr_tail) % VCP_RING);
    uint16_t room = (uint16_t)(VCP_RING - 1u - used);
    if (!important && room < VCP_RING / 2u) room = 0;   /* keep space for events */
    if ((uint16_t)n > room) { vcp_dropped++; __enable_irq(); return; }
    for (int k = 0; k < n; k++) {
        vcp_ring[vr_head] = line[k];
        vr_head = (uint16_t)((vr_head + 1u) % VCP_RING);
    }
    __enable_irq();
    vcp_kick();
}

static uint16_t crc16_modbus(const uint8_t *p, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    while (len--) {
        crc ^= *p++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1u) ? (uint16_t)((crc >> 1) ^ 0xA001u) : (uint16_t)(crc >> 1);
    }
    return crc;
}

/* Whole-string integer parse: "abc", "12x" and "" are rejected. */
static int parse_long(const char *s, long *out)
{
    if (!s || !*s) return 0;
    char *end;
    long v = strtol(s, &end, 10);
    if (*end != '\0') return 0;
    *out = v;
    return 1;
}

static float ocv_mv(float soc)
{
    /* Simplified LiFePO4 open-circuit voltage curve. */
    if (soc < 0.0f) soc = 0.0f;
    if (soc > 1.0f) soc = 1.0f;
    if (soc < 0.10f) return 3000.0f + (soc / 0.10f) * 200.0f;
    if (soc < 0.90f) return 3200.0f + ((soc - 0.10f) / 0.80f) * 150.0f;
    return 3350.0f + ((soc - 0.90f) / 0.10f) * 250.0f;
}

static uint16_t max_cell_mv(void)
{
    uint16_t m = 0;
    for (int i = 0; i < NUM_CELLS; i++)
        if (bat.cell_mv[i] > m) m = bat.cell_mv[i];
    return m;
}

/* The trip wire as the Nucleo sees it (a cut wire reads low: pull-down). */
static uint8_t wire_high(void)
{
    if (flt_cut) return 0;
    return HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_4) == GPIO_PIN_SET;
}

/* Is the interlock open, i.e. is the battery disconnected (tripped)? */
static uint8_t trip_active(void)
{
    if (!ilk_failsafe) return wire_high();
    return (uint8_t)(!wire_high() || wd_open);
}

/* Record the moment the interlock opened (first one wins until processed). */
static void note_trip(uint32_t t_us, uint8_t cause)
{
    __disable_irq();
    if (!trip_edge_flag) {
        trip_edge_us = t_us;
        trip_edge_cause = cause;
        trip_edge_flag = 1;
    }
    __enable_irq();
}

static const char *cause_name(uint8_t c)
{
    return c == 1 ? "heartbeat_lost" : c == 2 ? "wire_cut" : "trip_line";
}

/* ------------------------------------------------------------------ */
/* Fault library: what a misbehaving BMS looks like from the outside   */
/* ------------------------------------------------------------------ */
static uint8_t  flt_stale;                 /* serve frozen values (stuck BMS buffer)   */
static uint16_t flt_stale_regs[NUM_REGS];  /* the frozen snapshot                     */
static uint32_t flt_delay_ms;              /* answer every request this late          */
static uint8_t  flt_drop_pct;              /* ignore this % of requests                */
static uint8_t  flt_crc_pct;               /* break the CRC on this % of replies       */
static uint32_t flt_dropped, flt_corrupted;
static uint8_t  flt_burst;                 /* drop exactly this many read replies...   */
static uint8_t  burst_left;                /* ...right after the drift is applied      */
static uint32_t rng_state = 0x1234567u;

static uint8_t rand_pct(void)              /* xorshift32: cheap, good enough for faults */
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 17;
    rng_state ^= rng_state << 5;
    return (uint8_t)(rng_state % 100u);
}

static uint16_t reg_value(uint16_t r);

/* What the controller is told. With 'fault stale' the cells, pack voltage,
 * current, temperature, SOC and tick freeze; the real battery keeps going. */
static uint16_t served_value(uint16_t r)
{
    if (flt_stale && r != 9 && r != 11) return flt_stale_regs[r];
    return reg_value(r);
}

static uint16_t reg_value(uint16_t r)
{
    switch (r) {
    case 0: case 1: case 2: case 3: return bat.cell_mv[r];
    case 4: {
        uint32_t s = 0;
        for (int i = 0; i < NUM_CELLS; i++) s += bat.cell_mv[i];
        return (uint16_t)(s / 10u);
    }
    case 5:  return (uint16_t)(int16_t)(bat.current_a * 10.0f);
    case 6:  return (uint16_t)(int16_t)(bat.temp_c * 10.0f);
    case 7:  return (uint16_t)(bat.soc * 1000.0f);
    case 8:  return bat.grid_v;
    case 9:  return (uint16_t)((trip_active() ? 1u : 0u) | (drift_from_pot ? 2u : 0u) |
                               (ilk_failsafe ? 4u : 0u));
    case 10: return bat.tick;
    case 11: return charge_limit_da;
    default: return 0;
    }
}

/* ------------------------------------------------------------------ */
/* Physics                                                            */
/* ------------------------------------------------------------------ */
static float read_pot_drift_mv(void)
{
    uint32_t raw = 0;
    if (HAL_ADC_Start(&hadc1) == HAL_OK) {
        if (HAL_ADC_PollForConversion(&hadc1, 5) == HAL_OK)
            raw = HAL_ADC_GetValue(&hadc1);
        HAL_ADC_Stop(&hadc1);
    }
    /* Small dead band at the bottom so "pot at zero" really means zero. */
    if (raw < 60u) raw = 0;
    return (float)raw * (float)POT_MAX_DRIFT_MV / 4095.0f;
}

/* Recompute the cell voltages from the current state (no time passes), then
 * do the over-voltage bookkeeping for the latency measurement. */
static void update_cells(void)
{
    float base = ocv_mv(bat.soc) + bat.current_a * CELL_R_OHM * 1000.0f;
    for (int c = 0; c < NUM_CELLS; c++) {
        float v = base + (float)(c * 3); /* tiny natural spread between cells */
        if (c == NUM_CELLS - 1) v += bat.drift_mv;
        if (v < 0.0f) v = 0.0f;
        if (v > 65000.0f) v = 65000.0f;
        bat.cell_mv[c] = (uint16_t)v;
    }

    uint16_t vmax = max_cell_mv();
    if (!tripped && cross_us == 0 && vmax >= limit_mv) {
        cross_us = now_us();
        if (cross_us == 0) cross_us = 1;      /* 0 means "none" */
        first_served_us = 0;
        no_trip_reported = 0;
        premature_reported = 0;
        if (trip_active()) {
            /* Already in the safe state before the cell crossed the limit:
             * the battery was never unprotected. */
            tripped = 1;
            trip_done_us = cross_us;
            vcp_printf(1, "{\"event\":\"overvoltage\",\"cell_mv\":%u,\"limit_mv\":%u}\r\n",
                       vmax, limit_mv);
            vcp_printf(1,
                "{\"event\":\"trip\",\"latency_us\":0,\"until_reported_us\":0,"
                "\"reaction_us\":0,\"deadline_ms\":%lu,\"result\":\"PASS\","
                "\"note\":\"already_tripped\"}\r\n", (unsigned long)deadline_ms);
        } else {
            vcp_printf(1, "{\"event\":\"overvoltage\",\"cell_mv\":%u,\"limit_mv\":%u}\r\n",
                       vmax, limit_mv);
        }
    }
    /* Re-arm once the cell is back to normal and the interlock closed again. */
    if ((tripped || cross_us) && vmax + REARM_HYST_MV < limit_mv && !trip_active()) {
        tripped = 0;
        cross_us = 0;
        first_served_us = 0;
        vcp_printf(1, "{\"event\":\"rearmed\"}\r\n");
    }
}

/* One 100 ms step of simulated time. */
static void physics_step(void)
{
    const float dt = (PHYS_STEP_MS / 1000.0f) * TIME_SCALE;

    bat.drift_mv = drift_from_pot ? read_pot_drift_mv() : manual_drift_mv;

    /* Charger pushes cmd_current_a, limited by what the Pi allows. */
    float limit_a = charge_limit_da / 10.0f;
    float i = cmd_current_a;
    if (i > limit_a) i = limit_a;
    if (bat.soc >= 1.0f && i > 0.0f) i = 0.0f;
    if (bat.soc <= 0.0f && i < 0.0f) i = 0.0f;
    bat.current_a = i;

    bat.soc += (i * dt / 3600.0f) / CAPACITY_AH;
    if (bat.soc > 1.0f) bat.soc = 1.0f;
    if (bat.soc < 0.0f) bat.soc = 0.0f;

    float heat_w = i * i * CELL_R_OHM * NUM_CELLS;
    bat.temp_c += (heat_w - THERMAL_K_W_PER_K * (bat.temp_c - AMBIENT_C)) * dt / THERMAL_C_J_PER_K;

    bat.tick++;
    update_cells();

    if (cross_us && !tripped && !no_trip_reported &&
        (now_us() - cross_us) > NO_TRIP_TIMEOUT_MS * 1000u) {
        no_trip_reported = 1;
        vcp_printf(1, "{\"event\":\"no_trip\",\"result\":\"FAIL\",\"waited_ms\":%lu}\r\n",
                   (unsigned long)NO_TRIP_TIMEOUT_MS);
    }
}

/* ------------------------------------------------------------------ */
/* Modbus RTU slave                                                   */
/* ------------------------------------------------------------------ */
/* A delayed reply waits here until it is due (sent from app_loop). */
static uint8_t  pend_buf[3 + 2 * NUM_REGS + 2];
static uint16_t pend_len;
static uint8_t  pend_ov;
static uint32_t pend_due_ms;

static void apply_sync_drift(const char *how)
{
    sync_armed = 0;
    drift_from_pot = 0;
    manual_drift_mv = sync_drift_mv;
    bat.drift_mv = sync_drift_mv;
    burst_left = flt_burst;
    update_cells();          /* the over-voltage starts at this instant; no extra time step */
    vcp_printf(1, "{\"event\":\"drift_applied\",\"drift_mv\":%d,\"timing\":\"%s\"}\r\n",
               (int)sync_drift_mv, how);
}

static void mb_transmit(const uint8_t *frame, uint16_t len, uint8_t carries_ov)
{
    if (mb_tx_busy || len > sizeof mb_tx_buf) return;   /* half duplex: cannot happen */
    /* Timing starts when the bad value starts to go onto the wire (first byte):
     * the "reaction" figure therefore includes the reply's own wire time. */
    if (carries_ov && cross_us && !first_served_us) {
        first_served_us = now_us();
        if (first_served_us == 0) first_served_us = 1;
    }
    memcpy(mb_tx_buf, frame, len);
    mb_tx_is_read = (uint8_t)(len > 2 && frame[1] == 0x03u);
    mb_tx_busy = 1;
    if (HAL_UART_Transmit_IT(&huart1, mb_tx_buf, len) != HAL_OK) mb_tx_busy = 0;
}

static void mb_send_ov(uint8_t *frame, uint16_t len_without_crc, uint8_t carries_ov)
{
    uint16_t crc = crc16_modbus(frame, len_without_crc);
    frame[len_without_crc]     = (uint8_t)(crc & 0xFF);
    frame[len_without_crc + 1] = (uint8_t)(crc >> 8);
    uint16_t len = (uint16_t)(len_without_crc + 2u);

    if (burst_left && frame[1] == 0x03u) { burst_left--; flt_dropped++; return; }   /* deterministic burst */
    if (flt_drop_pct && rand_pct() < flt_drop_pct) { flt_dropped++; return; }
    if (flt_crc_pct && rand_pct() < flt_crc_pct) {
        frame[len_without_crc] ^= 0x5Au;              /* flip bits in the CRC */
        flt_corrupted++;
    }
    if (flt_delay_ms) {
        if (len > sizeof pend_buf) return;
        memcpy(pend_buf, frame, len);                 /* a newer request replaces it */
        pend_len = len;
        pend_ov = carries_ov;
        pend_due_ms = HAL_GetTick() + flt_delay_ms;
        return;
    }
    mb_transmit(frame, len, carries_ov);
}

static void mb_send(uint8_t *frame, uint16_t len_without_crc)
{
    mb_send_ov(frame, len_without_crc, 0);
}

static void mb_send_pending(void)
{
    if (pend_len && !mb_tx_busy && (int32_t)(HAL_GetTick() - pend_due_ms) >= 0) {
        uint16_t len = pend_len;
        pend_len = 0;
        mb_transmit(pend_buf, len, pend_ov);
    }
}

static void mb_exception(uint8_t fn, uint8_t code)
{
    uint8_t f[5] = { MODBUS_ADDR, (uint8_t)(fn | 0x80u), code, 0, 0 };
    mb_send(f, 3);
}

static void mb_handle_frame(const uint8_t *req, uint16_t len)
{
    if (len < 4) { mb_short_frames++; return; }   /* a fragment, not a frame */
    if (req[0] != MODBUS_ADDR) return;                         /* not for us */
    uint16_t crc_rx = (uint16_t)(req[len - 2] | (req[len - 1] << 8));
    if (crc16_modbus(req, (uint16_t)(len - 2)) != crc_rx) {        /* bad CRC: stay silent */
        mb_crc_errors++;
        return;
    }

    uint8_t fn = req[1];
    if (fn == 0x03) {
        if (len != 8) { mb_exception(fn, 0x03); return; }
        uint16_t start = (uint16_t)((req[2] << 8) | req[3]);
        uint16_t qty   = (uint16_t)((req[4] << 8) | req[5]);
        if (qty == 0 || qty > NUM_REGS) { mb_exception(fn, 0x03); return; }
        if ((uint32_t)start + qty > NUM_REGS) { mb_exception(fn, 0x02); return; }

        uint8_t out[3 + 2 * NUM_REGS + 2];
        out[0] = MODBUS_ADDR;
        out[1] = 0x03;
        out[2] = (uint8_t)(qty * 2u);
        uint8_t carries_ov = 0;     /* does this reply show an over-limit cell? */
        for (uint16_t k = 0; k < qty; k++) {
            uint16_t r = (uint16_t)(start + k);
            uint16_t v = served_value(r);
            if (r < NUM_CELLS && v >= limit_mv) carries_ov = 1;
            out[3 + 2 * k]     = (uint8_t)(v >> 8);
            out[3 + 2 * k + 1] = (uint8_t)(v & 0xFF);
        }
        polls_served++;
        mb_send_ov(out, (uint16_t)(3u + 2u * qty), carries_ov);
    } else if (fn == 0x06) {
        if (len != 8) { mb_exception(fn, 0x03); return; }
        uint16_t reg = (uint16_t)((req[2] << 8) | req[3]);
        uint16_t val = (uint16_t)((req[4] << 8) | req[5]);
        if (reg != REG_CHARGE_LIMIT) { mb_exception(fn, 0x02); return; }
        if (val > 2000u) { mb_exception(fn, 0x03); return; }
        charge_limit_da = val;
        uint8_t echo[8];
        memcpy(echo, req, 6);
        mb_send(echo, 6);
        polls_served++;
    } else {
        mb_exception(fn, 0x01);
    }
}

static void mb_poll(void)
{
    /*
     * Has the line been quiet for 3.5 characters (end of a Modbus frame)?
     * Snapshot the byte count and last-byte time with interrupts paused, and
     * only THEN read the clock. Reading the clock first left a window where a
     * byte could arrive in between: last-byte time > now, the unsigned
     * subtraction wrapped to ~4e9 us, and a half-received frame was processed.
     */
    __disable_irq();
    uint16_t pending = mb_len;
    uint32_t last    = mb_last_rx_us;
    __enable_irq();
    if (pending == 0) return;
    if ((int32_t)(now_us() - last) < (int32_t)mb_t35_us) return;   /* still arriving */

    uint8_t frame[sizeof mb_buf];
    uint16_t len;
    uint8_t overflow, bad;
    __disable_irq();
    if (mb_last_rx_us != last) {          /* a byte arrived just now: not finished */
        __enable_irq();
        return;
    }
    len = mb_len;
    memcpy(frame, mb_buf, len);
    overflow = mb_overflow;
    bad = mb_frame_bad;
    mb_len = 0;
    mb_overflow = 0;
    mb_frame_bad = 0;
    __enable_irq();

    /* Modbus rule: a frame with a corrupted character is discarded, no reply. */
    if (!overflow && !bad) mb_handle_frame(frame, len);
}

/* ------------------------------------------------------------------ */
/* Interlock: heartbeat watchdog (failsafe mode)                      */
/* ------------------------------------------------------------------ */
static void interlock_poll(void)
{
    uint8_t lvl = HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_5) == GPIO_PIN_SET;
    uint32_t t = now_us();
    if (lvl != hb_level) {
        hb_level = lvl;
        hb_last_edge_us = t;
        hb_edges++;
        if (wd_open) wd_open = 0;             /* heartbeat is back: watchdog closes */
    }
    if (ilk_failsafe && !wd_open && (t - hb_last_edge_us) > HB_TIMEOUT_MS * 1000u) {
        uint8_t was_open = trip_active();
        wd_open = 1;
        if (!was_open) note_trip(hb_last_edge_us + HB_TIMEOUT_MS * 1000u, 1);
    }
}

static void interlock_set(uint8_t failsafe)
{
    ilk_failsafe = failsafe;
    hb_last_edge_us = now_us();               /* grace period starts now */
    hb_level = HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_5) == GPIO_PIN_SET;
    wd_open = 0;
    trip_edge_flag = 0;
}

/* ------------------------------------------------------------------ */
/* Laptop commands                                                    */
/* ------------------------------------------------------------------ */
static void print_help(void)
{
    vcp_printf(1,
        "{\"help\":\"status | baseline | i <amps> | drift <mV>|pot|sync <mV> | limit <mV> | "
        "deadline <ms> | soc <pct> | grid <V> | interlock legacy|failsafe | "
        "fault stale on|off | fault delay <ms> | fault drop <pct> | fault crc <pct> | "
        "fault burst <n> | fault cut on|off | fault clear | help\"}\r\n");
}

static void faults_clear(void)
{
    flt_stale = 0;
    flt_delay_ms = 0;
    flt_drop_pct = 0;
    flt_crc_pct = 0;
    flt_burst = 0;
    burst_left = 0;
    pend_len = 0;
    flt_cut = 0;
}

static void handle_fault(const char *what, const char *val)
{
    if (!what) { vcp_printf(1, "{\"error\":\"fault needs a name\"}\r\n"); return; }
    if (!strcmp(what, "clear")) {
        faults_clear();
        vcp_printf(1, "{\"ok\":\"fault clear\"}\r\n");
        return;
    }
    if (!val) { vcp_printf(1, "{\"error\":\"missing value for fault %s\"}\r\n", what); return; }

    if (!strcmp(what, "stale") || !strcmp(what, "cut")) {
        uint8_t on;
        if (!strcmp(val, "on")) on = 1;
        else if (!strcmp(val, "off")) on = 0;
        else { vcp_printf(1, "{\"error\":\"fault %s on|off\"}\r\n", what); return; }
        if (what[0] == 's') {
            if (on) for (uint16_t r = 0; r < NUM_REGS; r++) flt_stale_regs[r] = reg_value(r);
            flt_stale = on;
        } else {
            uint8_t was_open = trip_active();
            flt_cut = on;
            if (on && !was_open && trip_active()) note_trip(now_us(), 2);
        }
        vcp_printf(1, "{\"ok\":\"fault %s %s\"}\r\n", what, val);
        return;
    }

    long v;
    if (!parse_long(val, &v)) { vcp_printf(1, "{\"error\":\"fault %s needs a number\"}\r\n", what); return; }
    if (!strcmp(what, "delay")) {
        if (v < 0 || v > 2000) { vcp_printf(1, "{\"error\":\"delay must be 0..2000 ms\"}\r\n"); return; }
        flt_delay_ms = (uint32_t)v;
        if (!v) pend_len = 0;
    } else if (!strcmp(what, "drop")) {
        if (v < 0 || v > 100) { vcp_printf(1, "{\"error\":\"drop must be 0..100 %%\"}\r\n"); return; }
        flt_drop_pct = (uint8_t)v;
    } else if (!strcmp(what, "crc")) {
        if (v < 0 || v > 100) { vcp_printf(1, "{\"error\":\"crc must be 0..100 %%\"}\r\n"); return; }
        flt_crc_pct = (uint8_t)v;
    } else if (!strcmp(what, "burst")) {
        if (v < 0 || v > 10) { vcp_printf(1, "{\"error\":\"burst must be 0..10 replies\"}\r\n"); return; }
        flt_burst = (uint8_t)v;
        burst_left = 0;                    /* armed by the next drift command */
    } else {
        vcp_printf(1, "{\"error\":\"unknown fault %s\"}\r\n", what);
        return;
    }
    vcp_printf(1, "{\"ok\":\"fault %s %ld\"}\r\n", what, v);
}

static void handle_command(char *line)
{
    char *cmd = strtok(line, " \t");
    char *arg = strtok(NULL, " \t");
    if (!cmd) return;

    if (!strcmp(cmd, "help")) { print_help(); return; }
    if (!strcmp(cmd, "status")) { last_tele_ms = 0; return; }
    if (!strcmp(cmd, "fault")) { handle_fault(arg, strtok(NULL, " \t")); return; }
    if (!strcmp(cmd, "baseline")) {
        /* Known starting point for every test run (the interlock mode stays). */
        faults_clear();
        drift_from_pot = 0;
        manual_drift_mv = 0.0f;
        sync_armed = 0;
        cmd_current_a = 10.0f;
        bat.soc = 0.50f;
        bat.grid_v = 230;
        limit_mv = 3650;
        deadline_ms = 500;
        vcp_printf(1, "{\"ok\":\"baseline\"}\r\n");
        return;
    }
    if (!arg) { vcp_printf(1, "{\"error\":\"missing value for %s\"}\r\n", cmd); return; }
    if (!strcmp(cmd, "interlock")) {
        if (!strcmp(arg, "legacy")) interlock_set(0);
        else if (!strcmp(arg, "failsafe")) interlock_set(1);
        else { vcp_printf(1, "{\"error\":\"interlock legacy|failsafe\"}\r\n"); return; }
        vcp_printf(1, "{\"ok\":\"interlock %s\"}\r\n", arg);
        return;
    }
    if (!strcmp(cmd, "drift") && !strcmp(arg, "sync")) {
        long mv;
        if (!parse_long(strtok(NULL, " \t"), &mv)) { vcp_printf(1, "{\"error\":\"drift sync needs mV\"}\r\n"); return; }
        if (mv < -1000 || mv > 1000) { vcp_printf(1, "{\"error\":\"drift must be -1000..1000 mV\"}\r\n"); return; }
        sync_drift_mv = (float)mv;
        sync_armed_ms = HAL_GetTick();
        sync_armed = 1;
        vcp_printf(1, "{\"ok\":\"drift sync %ld\"}\r\n", mv);
        return;
    }
    if (!strcmp(cmd, "drift") && !strcmp(arg, "pot")) {
        sync_armed = 0;
        drift_from_pot = 1;
        vcp_printf(1, "{\"ok\":\"drift pot\"}\r\n");
        return;
    }

    long v;
    if (!parse_long(arg, &v)) { vcp_printf(1, "{\"error\":\"%s needs a whole number\"}\r\n", cmd); return; }
    if (!strcmp(cmd, "i")) {
        if (v < -200 || v > 200) { vcp_printf(1, "{\"error\":\"i must be -200..200 A\"}\r\n"); return; }
        cmd_current_a = (float)v;
    } else if (!strcmp(cmd, "drift")) {
        if (v < -1000 || v > 1000) { vcp_printf(1, "{\"error\":\"drift must be -1000..1000 mV\"}\r\n"); return; }
        sync_armed = 0;
        drift_from_pot = 0;
        manual_drift_mv = (float)v;
        burst_left = flt_burst;
    } else if (!strcmp(cmd, "limit")) {
        if (v < 3000 || v > 4200) { vcp_printf(1, "{\"error\":\"limit must be 3000..4200 mV\"}\r\n"); return; }
        limit_mv = (uint16_t)v;
    } else if (!strcmp(cmd, "deadline")) {
        if (v < 10 || v > 10000) { vcp_printf(1, "{\"error\":\"deadline must be 10..10000 ms\"}\r\n"); return; }
        deadline_ms = (uint32_t)v;
    } else if (!strcmp(cmd, "soc")) {
        if (v < 0 || v > 100) { vcp_printf(1, "{\"error\":\"soc must be 0..100\"}\r\n"); return; }
        bat.soc = (float)v / 100.0f;
    } else if (!strcmp(cmd, "grid")) {
        if (v < 0 || v > 400) { vcp_printf(1, "{\"error\":\"grid must be 0..400 V\"}\r\n"); return; }
        bat.grid_v = (uint16_t)v;
    } else {
        vcp_printf(1, "{\"error\":\"unknown command %s\"}\r\n", cmd);
        return;
    }
    vcp_printf(1, "{\"ok\":\"%s %ld\"}\r\n", cmd, v);
}

/* ------------------------------------------------------------------ */
/* HAL callbacks (override the weak HAL versions)                     */
/* ------------------------------------------------------------------ */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == huart1.Instance) {
        if (mb_len < sizeof mb_buf) mb_buf[mb_len++] = mb_rx_byte;
        else mb_overflow = 1;
        mb_last_rx_us = now_us();
        HAL_UART_Receive_IT(&huart1, &mb_rx_byte, 1);
    } else if (huart->Instance == hlpuart1.Instance) {
        char c = (char)cmd_rx_byte;
        if (!cmd_ready) {
            if (c == '\r' || c == '\n') {
                if (cmd_len > 0) { cmd_line[cmd_len] = '\0'; cmd_ready = 1; }
            } else if (cmd_len < sizeof cmd_line - 1) {
                cmd_line[cmd_len++] = c;
            }
        }
        HAL_UART_Receive_IT(&hlpuart1, &cmd_rx_byte, 1);
    }
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == huart1.Instance) {
        mb_tx_busy = 0;
        mb_tx_done = 1;                       /* the last stop bit has left the wire */
    } else if (huart->Instance == hlpuart1.Instance) {
        vr_tail = (uint16_t)((vr_tail + vr_chunk) % VCP_RING);
        vcp_busy = 0;
        vcp_kick();
    }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    /*
     * How the ST HAL behaves here (stm32g4xx_hal_uart.c, HAL_UART_IRQHandler):
     *  - Noise, framing or parity error: the byte has ALREADY been stored via
     *    HAL_UART_RxCpltCallback, reception keeps running ("non blocking").
     *  - Overrun: reception is aborted and RxState goes back to READY.
     * So: never wipe the buffer here. Noise alone is counted but tolerated
     * (the CRC will catch a really bad byte); a corrupted or lost byte marks
     * the frame bad so it is dropped once the frame ends.
     */
    uint32_t err = huart->ErrorCode;

    if (huart->Instance == huart1.Instance) {
        if (err & HAL_UART_ERROR_NE) mb_noise_errors++;
        if (err & (HAL_UART_ERROR_FE | HAL_UART_ERROR_PE | HAL_UART_ERROR_ORE)) {
            mb_frame_errors++;
            mb_frame_bad = 1;
        }
        mb_last_rx_us = now_us();          /* keep the end-of-frame timing honest */
        if (huart->RxState == HAL_UART_STATE_READY)
            HAL_UART_Receive_IT(&huart1, &mb_rx_byte, 1);
    } else if (huart->Instance == hlpuart1.Instance) {
        if (huart->RxState == HAL_UART_STATE_READY)
            HAL_UART_Receive_IT(&hlpuart1, &cmd_rx_byte, 1);
    }
}

void HAL_GPIO_EXTI_Callback(uint16_t GPIO_Pin)
{
    if (GPIO_Pin != GPIO_PIN_4) return;
    uint32_t t = now_us();                    /* timestamp first, everything else later */
    if (flt_cut) return;                      /* the wire is "cut": the Nucleo can't see it */
    uint8_t high = HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_4) == GPIO_PIN_SET;
    /* legacy: rising edge = trip. failsafe: falling edge (permissive lost) = trip,
     * unless the heartbeat watchdog had already opened the interlock. */
    if ((!ilk_failsafe && high) || (ilk_failsafe && !high && !wd_open)) note_trip(t, 0);
}

/* ------------------------------------------------------------------ */
/* Independent watchdog (a hung emulator would otherwise look like a   */
/* perfectly stale BMS). Register-level: no CubeMX change needed.      */
/* ------------------------------------------------------------------ */
static void iwdg_start(void)
{
    if (RCC->CSR & RCC_CSR_IWDGRSTF) boot_reason = "watchdog";
    else if (RCC->CSR & RCC_CSR_SFTRSTF) boot_reason = "software";
    else if (RCC->CSR & RCC_CSR_BORRSTF) boot_reason = "brown_out";
    else if (RCC->CSR & RCC_CSR_PINRSTF) boot_reason = "reset_pin";
    RCC->CSR |= RCC_CSR_RMVF;

    DBGMCU->APB1FZR1 |= DBGMCU_APB1FZR1_DBG_IWDG_STOP;   /* pauses at a breakpoint */
    IWDG->KR = 0xCCCCu;                                  /* start (LSI ~32 kHz)    */
    IWDG->KR = 0x5555u;                                  /* unlock PR/RLR          */
    IWDG->PR = 4u;                                       /* /64 -> 2 ms per count  */
    IWDG->RLR = 1000u;                                   /* ~2 s timeout           */
    for (uint32_t n = 0; IWDG->SR && n < 1000000u; n++) { }
    IWDG->KR = 0xAAAAu;                                  /* reload                 */
}

/* ------------------------------------------------------------------ */
/* Public API                                                         */
/* ------------------------------------------------------------------ */
void app_init(void)
{
    /* TIM2 at 1 MHz whatever the system clock is (150 or 170 MHz). */
    uint32_t tim_clk = HAL_RCC_GetPCLK1Freq();
    if ((RCC->CFGR & RCC_CFGR_PPRE1_2) != 0u) tim_clk *= 2u;   /* APB1 prescaler > 1 */
    htim2.Instance->PSC = (tim_clk / 1000000u) - 1u;
    htim2.Instance->ARR = 0xFFFFFFFFu;
    htim2.Instance->EGR = TIM_EGR_UG;     /* load the new prescaler now */
    HAL_TIM_Base_Start(&htim2);

    HAL_ADCEx_Calibration_Start(&hadc1, ADC_SINGLE_ENDED);

    /* PB4 trip wire: interrupt on both edges (legacy trips on rising, failsafe on falling). */
    EXTI->RTSR1 |= EXTI_RTSR1_RT4;
    EXTI->FTSR1 |= EXTI_FTSR1_FT4;
    /* PB5 heartbeat input with pull-down (a missing wire = no heartbeat). */
    GPIO_InitTypeDef gi = {0};
    gi.Pin = GPIO_PIN_5;
    gi.Mode = GPIO_MODE_INPUT;
    gi.Pull = GPIO_PULLDOWN;
    HAL_GPIO_Init(GPIOB, &gi);

    /* The trip timestamp is the product: its interrupt goes first. */
    HAL_NVIC_SetPriority(EXTI4_IRQn, 0, 0);
    HAL_NVIC_SetPriority(USART1_IRQn, 1, 0);
    HAL_NVIC_SetPriority(LPUART1_IRQn, 2, 0);

    /* Modbus inter-frame silence: 3.5 characters, fixed 1750 us above 19200 baud. */
    uint32_t baud = huart1.Init.BaudRate;
    mb_t35_us = (baud > 19200u) ? 1750u : (uint32_t)((35u * 11u * 1000000u) / (10u * baud)) + 1u;

    HAL_UART_Receive_IT(&huart1, &mb_rx_byte, 1);
    HAL_UART_Receive_IT(&hlpuart1, &cmd_rx_byte, 1);

    iwdg_start();
    interlock_set(0);
    physics_step();
    vcp_printf(1, "{\"event\":\"boot\",\"fw\":\"" FW_VERSION "\",\"reset\":\"%s\","
               "\"modbus_baud\":%lu,\"t35_us\":%lu}\r\n",
               boot_reason, (unsigned long)baud, (unsigned long)mb_t35_us);
    last_phys_ms = HAL_GetTick();
    last_tele_ms = HAL_GetTick();
}

void app_loop(void)
{
    IWDG->KR = 0xAAAAu;                       /* still alive */

    mb_poll();
    mb_send_pending();
    interlock_poll();

    if (mb_tx_done) {                         /* a reply has completely left the wire */
        mb_tx_done = 0;
        if (sync_armed && mb_tx_is_read) apply_sync_drift("after_reply");
    }
    /* No read reply went out (controller stopped, or every reply dropped):
     * apply the drift anyway so the scenario still runs. */
    if (sync_armed && HAL_GetTick() - sync_armed_ms >= SYNC_TIMEOUT_MS) apply_sync_drift("timeout");

    if (trip_edge_flag) {
        __disable_irq();
        uint32_t edge = trip_edge_us;
        uint8_t cause = trip_edge_cause;
        trip_edge_flag = 0;
        __enable_irq();
        if (cross_us && !tripped && (int32_t)(edge - cross_us) < 0) {
            /* The interlock opened before the crossing was recorded: safe state. */
            tripped = 1;
            trip_done_us = cross_us;
            vcp_printf(1, "{\"event\":\"trip\",\"latency_us\":0,\"until_reported_us\":0,"
                       "\"reaction_us\":0,\"deadline_ms\":%lu,\"result\":\"PASS\","
                       "\"note\":\"already_tripped\",\"cause\":\"%s\"}\r\n",
                       (unsigned long)deadline_ms, cause_name(cause));
        } else if (cross_us && !tripped) {
            uint32_t total  = edge - cross_us;
            uint32_t detect = first_served_us ? first_served_us - cross_us : 0u;
            uint32_t react  = first_served_us ? edge - first_served_us : 0u;
            tripped = 1;
            trip_done_us = edge;
            vcp_printf(1,
                "{\"event\":\"trip\",\"latency_us\":%lu,\"until_reported_us\":%lu,"
                "\"reaction_us\":%lu,\"deadline_ms\":%lu,\"result\":\"%s\",\"cause\":\"%s\"}\r\n",
                (unsigned long)total, (unsigned long)detect, (unsigned long)react,
                (unsigned long)deadline_ms, (total <= deadline_ms * 1000u) ? "PASS" : "FAIL",
                cause_name(cause));
        } else if (!cross_us) {
            vcp_printf(1, "{\"event\":\"spurious_trip\",\"cause\":\"%s\",\"note\":\"trip with no over-voltage\"}\r\n",
                       cause_name(cause));
        }
    }

    /* The interlock closed again while a cell is still over the limit: the
     * controller released the trip too early. The battery is unprotected. */
    if (tripped && cross_us && !premature_reported && !trip_active() && max_cell_mv() >= limit_mv) {
        premature_reported = 1;
        vcp_printf(1, "{\"event\":\"premature_release\",\"result\":\"FAIL\",\"cell_mv\":%u,"
                   "\"held_ms\":%lu}\r\n", max_cell_mv(),
                   (unsigned long)((now_us() - trip_done_us) / 1000u));
    }

    if (cmd_ready) {
        char line[sizeof cmd_line];
        __DMB();                              /* read the line only after the flag */
        memcpy(line, cmd_line, sizeof line);
        cmd_len = 0;
        cmd_ready = 0;
        handle_command(line);
    }

    uint32_t ms = HAL_GetTick();
    if (ms - last_phys_ms >= PHYS_STEP_MS) {
        last_phys_ms += PHYS_STEP_MS;
        physics_step();
    }
    if (last_tele_ms == 0 || ms - last_tele_ms >= TELEMETRY_MS) {
        last_tele_ms = ms ? ms : 1;
        HAL_GPIO_TogglePin(GPIOA, GPIO_PIN_5);   /* heartbeat on LD2 */
        uint32_t hb_ms = (now_us() - hb_last_edge_us) / 1000u;
        vcp_printf(0,
            "{\"cells\":[%u,%u,%u,%u],\"i_da\":%d,\"temp_dc\":%d,\"soc_pm\":%u,"
            "\"grid_v\":%u,\"drift_mv\":%d,\"limit_mv\":%u,\"trip\":%u,\"polls\":%lu,"
            "\"noise_err\":%lu,\"frame_err\":%lu,\"crc_err\":%lu,\"short_err\":%lu,"
            "\"f_stale\":%u,\"f_delay_ms\":%lu,\"f_drop\":%u,\"f_crc\":%u,\"f_burst\":%u,\"f_cut\":%u,"
            "\"dropped\":%lu,\"corrupted\":%lu,\"ilk\":\"%s\",\"hb_ms\":%lu,\"vcp_drop\":%lu}\r\n",
            bat.cell_mv[0], bat.cell_mv[1], bat.cell_mv[2], bat.cell_mv[3],
            (int)(bat.current_a * 10.0f), (int)(bat.temp_c * 10.0f),
            (unsigned)(bat.soc * 1000.0f), bat.grid_v, (int)bat.drift_mv, limit_mv,
            (unsigned)trip_active(), (unsigned long)polls_served,
            (unsigned long)mb_noise_errors, (unsigned long)mb_frame_errors,
            (unsigned long)mb_crc_errors, (unsigned long)mb_short_frames,
            (unsigned)flt_stale, (unsigned long)flt_delay_ms, (unsigned)flt_drop_pct,
            (unsigned)flt_crc_pct, (unsigned)flt_burst, (unsigned)flt_cut,
            (unsigned long)flt_dropped, (unsigned long)flt_corrupted,
            ilk_failsafe ? "failsafe" : "legacy", (unsigned long)(hb_ms > 99999u ? 99999u : hb_ms),
            (unsigned long)vcp_dropped);
    }
}
