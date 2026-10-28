/*
 * GridTwin-HIL day 1 firmware for NUCLEO-G491RE
 *
 * What it does
 *   1. Emulates a 4-cell LiFePO4 battery (100 Ah) with a simple physics model.
 *   2. Serves it as a Modbus RTU slave (address 1) on USART1 (PC4 TX / PC5 RX),
 *      wired to the Raspberry Pi's UART.
 *   3. Lets you push cell 4 towards over-voltage with the potentiometer on PA0.
 *   4. Timestamps (1 us resolution, TIM2) the moment a cell crosses the limit,
 *      the first Modbus reply that reported it, and the Pi's safety-trip edge on
 *      PB4, then reports the latency over the ST-Link USB serial (LPUART1).
 *
 * CubeMX peripherals expected (see README.md):
 *   LPUART1  115200 8N1, PA2/PA3 (ST-Link virtual COM), global interrupt ON
 *   USART1   9600 8N1,   PC4/PC5,                      global interrupt ON
 *   TIM2     internal clock, period 0xFFFFFFFF (prescaler is set here at runtime)
 *   ADC1     IN1 single-ended on PA0
 *   PB4      GPIO_EXTI4, rising edge, pull-down, EXTI line[4] interrupt ON
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

/* Modbus register map (holding registers, function 0x03 / 0x06)
 *   0..3  cell 1..4 voltage            mV
 *   4     pack voltage                 10 mV
 *   5     pack current (+ = charging)  0.1 A, signed
 *   6     temperature                  0.1 degC
 *   7     state of charge              0.1 %
 *   8     grid voltage                 V
 *   9     status bits: b0 trip line high, b1 drift from pot
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
static int      drift_from_pot = 1;
static float    manual_drift_mv = 0.0f;
static uint16_t limit_mv       = 3650;
static uint32_t deadline_ms    = 500;

/* trip timing (all in TIM2 microseconds) */
static volatile uint32_t trip_edge_us;
static volatile uint8_t  trip_edge_flag;
static uint32_t cross_us;            /* 0 = no over-voltage pending */
static uint32_t first_served_us;     /* first reply carrying the bad value */
static uint8_t  tripped;
static uint8_t  no_trip_reported;
static uint32_t polls_served;

/* Modbus receive (USART1, byte-by-byte interrupt) */
static uint8_t           mb_rx_byte;
static uint8_t           mb_buf[64];
static volatile uint16_t mb_len;
static volatile uint32_t mb_last_rx_us;
static volatile uint8_t  mb_overflow;
static volatile uint8_t  mb_frame_bad;      /* framing/parity/overrun error inside this frame */
static uint32_t          mb_t35_us;

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

/* VCP transmit buffer (interrupt-driven so it never blocks Modbus) */
static char vcp_buf[320];

static uint32_t last_phys_ms, last_tele_ms;

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */
static inline uint32_t now_us(void) { return htim2.Instance->CNT; }

static void vcp_printf(int must_send, const char *fmt, ...)
{
    /* Wait for the previous transmission if this message matters. */
    uint32_t start = HAL_GetTick();
    while (hlpuart1.gState != HAL_UART_STATE_READY) {
        if (!must_send || (HAL_GetTick() - start) > 50u) return;
    }
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(vcp_buf, sizeof vcp_buf, fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    if (n >= (int)sizeof vcp_buf) n = (int)sizeof vcp_buf - 1;
    HAL_UART_Transmit_IT(&hlpuart1, (uint8_t *)vcp_buf, (uint16_t)n);
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

static uint8_t trip_line_high(void)
{
    return HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_4) == GPIO_PIN_SET;
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
    case 9:  return (uint16_t)((trip_line_high() ? 1u : 0u) | (drift_from_pot ? 2u : 0u));
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

    float base = ocv_mv(bat.soc) + i * CELL_R_OHM * 1000.0f;
    for (int c = 0; c < NUM_CELLS; c++) {
        float v = base + (float)(c * 3); /* tiny natural spread between cells */
        if (c == NUM_CELLS - 1) v += bat.drift_mv;
        if (v < 0.0f) v = 0.0f;
        if (v > 65000.0f) v = 65000.0f;
        bat.cell_mv[c] = (uint16_t)v;
    }
    bat.tick++;

    /* Over-voltage bookkeeping for the trip-latency measurement. */
    uint16_t vmax = max_cell_mv();
    if (!tripped && cross_us == 0 && vmax >= limit_mv) {
        cross_us = now_us();
        if (cross_us == 0) cross_us = 1;      /* 0 means "none" */
        first_served_us = 0;
        no_trip_reported = 0;
        vcp_printf(1, "{\"event\":\"overvoltage\",\"cell_mv\":%u,\"limit_mv\":%u}\r\n",
                   vmax, limit_mv);
    }
    if (cross_us && !tripped && !no_trip_reported &&
        (now_us() - cross_us) > NO_TRIP_TIMEOUT_MS * 1000u) {
        no_trip_reported = 1;
        vcp_printf(1, "{\"event\":\"no_trip\",\"result\":\"FAIL\",\"waited_ms\":%lu}\r\n",
                   (unsigned long)NO_TRIP_TIMEOUT_MS);
    }
    /* Re-arm once the cell is back to normal and the Pi released the trip. */
    if ((tripped || cross_us) && vmax + REARM_HYST_MV < limit_mv && !trip_line_high()) {
        tripped = 0;
        cross_us = 0;
        first_served_us = 0;
        vcp_printf(1, "{\"event\":\"rearmed\"}\r\n");
    }
}

/* ------------------------------------------------------------------ */
/* Modbus RTU slave                                                   */
/* ------------------------------------------------------------------ */
static void mb_send(uint8_t *frame, uint16_t len_without_crc)
{
    uint16_t crc = crc16_modbus(frame, len_without_crc);
    frame[len_without_crc]     = (uint8_t)(crc & 0xFF);
    frame[len_without_crc + 1] = (uint8_t)(crc >> 8);
    HAL_UART_Transmit(&huart1, frame, (uint16_t)(len_without_crc + 2u), 200);
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
        for (uint16_t k = 0; k < qty; k++) {
            uint16_t v = reg_value((uint16_t)(start + k));
            out[3 + 2 * k]     = (uint8_t)(v >> 8);
            out[3 + 2 * k + 1] = (uint8_t)(v & 0xFF);
        }
        /* Did this reply carry a cell value that is over the limit? */
        if (cross_us && !first_served_us && start < NUM_CELLS) {
            first_served_us = now_us();
            if (first_served_us == 0) first_served_us = 1;
        }
        polls_served++;
        mb_send(out, (uint16_t)(3u + 2u * qty));
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
/* Laptop commands                                                    */
/* ------------------------------------------------------------------ */
static void print_help(void)
{
    vcp_printf(1,
        "{\"help\":\"status | i <amps> | drift <mV>|pot | limit <mV> | "
        "deadline <ms> | soc <pct> | grid <V> | help\"}\r\n");
}

static void handle_command(char *line)
{
    char *cmd = strtok(line, " \t");
    char *arg = strtok(NULL, " \t");
    if (!cmd) return;

    if (!strcmp(cmd, "help")) { print_help(); return; }
    if (!strcmp(cmd, "status")) { last_tele_ms = 0; return; }
    if (!arg) { vcp_printf(1, "{\"error\":\"missing value for %s\"}\r\n", cmd); return; }

    long v = strtol(arg, NULL, 10);
    if (!strcmp(cmd, "i")) {
        if (v < -200 || v > 200) { vcp_printf(1, "{\"error\":\"i must be -200..200 A\"}\r\n"); return; }
        cmd_current_a = (float)v;
    } else if (!strcmp(cmd, "drift")) {
        if (!strcmp(arg, "pot")) { drift_from_pot = 1; }
        else { drift_from_pot = 0; manual_drift_mv = (float)v; }
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
    vcp_printf(1, "{\"ok\":\"%s %s\"}\r\n", cmd, arg);
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
    if (GPIO_Pin == GPIO_PIN_4 && !trip_edge_flag) {
        trip_edge_us = now_us();      /* timestamp first, everything else later */
        trip_edge_flag = 1;
    }
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

    /* Modbus inter-frame silence: 3.5 characters, fixed 1750 us above 19200 baud. */
    uint32_t baud = huart1.Init.BaudRate;
    mb_t35_us = (baud > 19200u) ? 1750u : (uint32_t)((35u * 11u * 1000000u) / (10u * baud)) + 1u;

    HAL_UART_Receive_IT(&huart1, &mb_rx_byte, 1);
    HAL_UART_Receive_IT(&hlpuart1, &cmd_rx_byte, 1);

    physics_step();
    vcp_printf(1, "{\"event\":\"boot\",\"fw\":\"gridtwin-day1\",\"modbus_baud\":%lu,\"t35_us\":%lu}\r\n",
               (unsigned long)baud, (unsigned long)mb_t35_us);
    last_phys_ms = HAL_GetTick();
    last_tele_ms = HAL_GetTick();
}

void app_loop(void)
{
    mb_poll();

    if (trip_edge_flag) {
        uint32_t edge = trip_edge_us;
        trip_edge_flag = 0;
        if (cross_us && !tripped) {
            uint32_t total  = edge - cross_us;
            uint32_t detect = first_served_us ? first_served_us - cross_us : 0u;
            uint32_t react  = first_served_us ? edge - first_served_us : 0u;
            tripped = 1;
            vcp_printf(1,
                "{\"event\":\"trip\",\"latency_us\":%lu,\"until_reported_us\":%lu,"
                "\"reaction_us\":%lu,\"deadline_ms\":%lu,\"result\":\"%s\"}\r\n",
                (unsigned long)total, (unsigned long)detect, (unsigned long)react,
                (unsigned long)deadline_ms, (total <= deadline_ms * 1000u) ? "PASS" : "FAIL");
        } else if (!cross_us) {
            vcp_printf(1, "{\"event\":\"spurious_trip\",\"note\":\"trip with no over-voltage\"}\r\n");
        }
    }

    if (cmd_ready) {
        char line[sizeof cmd_line];
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
        vcp_printf(0,
            "{\"cells\":[%u,%u,%u,%u],\"i_da\":%d,\"temp_dc\":%d,\"soc_pm\":%u,"
            "\"grid_v\":%u,\"drift_mv\":%d,\"limit_mv\":%u,\"trip\":%u,\"polls\":%lu,"
            "\"noise_err\":%lu,\"frame_err\":%lu,\"crc_err\":%lu,\"short_err\":%lu}\r\n",
            bat.cell_mv[0], bat.cell_mv[1], bat.cell_mv[2], bat.cell_mv[3],
            (int)(bat.current_a * 10.0f), (int)(bat.temp_c * 10.0f),
            (unsigned)(bat.soc * 1000.0f), bat.grid_v, (int)bat.drift_mv, limit_mv,
            (unsigned)trip_line_high(), (unsigned long)polls_served,
            (unsigned long)mb_noise_errors, (unsigned long)mb_frame_errors,
            (unsigned long)mb_crc_errors, (unsigned long)mb_short_frames);
    }
}
