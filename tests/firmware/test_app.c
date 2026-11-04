/* Host tests for firmware/Core/Src/gridtwin_app.c (no hardware needed).
 *   gcc -std=c11 -Wall -Itests/firmware -Ifirmware/Core/Inc tests/firmware/test_app.c \
 *       firmware/Core/Src/gridtwin_app.c -lm -o fwtest && ./fwtest
 * Time is simulated: step(n) advances n milliseconds and runs the main loop once per ms. */
#include "main.h"
#include "gridtwin_app.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

TIM_TypeDef tim2_regs; RCC_TypeDef rcc_regs; IWDG_TypeDef iwdg_regs; DBGMCU_TypeDef dbg_regs; EXTI_TypeDef exti_regs;
GPIO_TypeDef gpioa, gpiob; USART_TypeDef u1, lpu1;
UART_HandleTypeDef huart1 = { &u1, {9600}, HAL_UART_STATE_READY, HAL_UART_STATE_BUSY_RX, 0 };
UART_HandleTypeDef hlpuart1 = { &lpu1, {115200}, HAL_UART_STATE_READY, HAL_UART_STATE_BUSY_RX, 0 };
TIM_HandleTypeDef htim2 = { &tim2_regs }; ADC_HandleTypeDef hadc1;

static uint32_t tick_ms; static uint8_t *rx1, *rxl;
static uint8_t tx[64]; static int txlen, tx1_pending, txl_pending;
static int pb4, pb5;                         /* trip wire, heartbeat wire */
static char vcp[1 << 18]; static size_t vlen;
static int failures, checks;

static size_t mark;
static void dump(void) {
    vcp[vlen] = 0;
    for (char *l = strtok(vcp + mark, "\r\n"); l; l = strtok(NULL, "\r\n"))
        if (!strstr(l, "\"cells\"") && !strstr(l, "\"help\"")) printf("    %s\n", l);
    vlen = mark;                                       /* strtok mangled the log; drop it */
}
#define CHECK(cond, msg) do { checks++; if (!(cond)) { failures++; printf("FAIL: %s (line %d)\n", msg, __LINE__); dump(); } } while (0)

uint32_t HAL_GetTick(void) { return tick_ms; }
uint32_t HAL_RCC_GetPCLK1Freq(void) { return 170000000u; }
HAL_StatusTypeDef HAL_TIM_Base_Start(TIM_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_ADCEx_Calibration_Start(ADC_HandleTypeDef *h, uint32_t m) { (void)h; (void)m; return HAL_OK; }
HAL_StatusTypeDef HAL_ADC_Start(ADC_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_ADC_PollForConversion(ADC_HandleTypeDef *h, uint32_t t) { (void)h; (void)t; return HAL_OK; }
uint32_t HAL_ADC_GetValue(ADC_HandleTypeDef *h) { (void)h; return 4000; }   /* floating pin: must be ignored */
HAL_StatusTypeDef HAL_ADC_Stop(ADC_HandleTypeDef *h) { (void)h; return HAL_OK; }
GPIO_PinState HAL_GPIO_ReadPin(GPIO_TypeDef *p, uint16_t pin) { (void)p; return (pin == GPIO_PIN_4 ? pb4 : pb5) ? GPIO_PIN_SET : GPIO_PIN_RESET; }
void HAL_GPIO_TogglePin(GPIO_TypeDef *p, uint16_t pin) { (void)p; (void)pin; }
void HAL_GPIO_Init(GPIO_TypeDef *p, GPIO_InitTypeDef *i) { (void)p; (void)i; }
void HAL_NVIC_SetPriority(IRQn_Type irq, uint32_t a, uint32_t b) { (void)irq; (void)a; (void)b; }
HAL_StatusTypeDef HAL_UART_Transmit_IT(UART_HandleTypeDef *h, const uint8_t *d, uint16_t n) {
    if (h == &huart1) { if (tx1_pending) return HAL_BUSY; memcpy(tx, d, n); txlen = n; tx1_pending = 1; }
    else { if (txl_pending) return HAL_BUSY; if (vlen + n < sizeof vcp) { memcpy(vcp + vlen, d, n); vlen += n; } txl_pending = 1; }
    return HAL_OK;
}
HAL_StatusTypeDef HAL_UART_Receive_IT(UART_HandleTypeDef *h, uint8_t *d, uint16_t n) { (void)n; if (h == &huart1) rx1 = d; else rxl = d; return HAL_OK; }

/* one simulated millisecond: interrupts first (TX complete), then the main loop */
static void ms1(void) {
    tick_ms++; tim2_regs.CNT += 1000;
    if (tx1_pending) { tx1_pending = 0; HAL_UART_TxCpltCallback(&huart1); }
    while (txl_pending) { txl_pending = 0; HAL_UART_TxCpltCallback(&hlpuart1); }
    app_loop();
}
static void step(int ms) { while (ms-- > 0) ms1(); }
static void wire(int level) { pb4 = level; HAL_GPIO_EXTI_Callback(GPIO_PIN_4); }
static void cmd(const char *s) {
    for (const char *p = s; *p; p++) { *rxl = (uint8_t)*p; HAL_UART_RxCpltCallback(&hlpuart1); }
    *rxl = '\n'; HAL_UART_RxCpltCallback(&hlpuart1); step(2);
}
static uint16_t crc(const uint8_t *p, int n) { uint16_t c = 0xFFFF; while (n--) { c ^= *p++; for (int i = 0; i < 8; i++) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1; } return c; }
static const uint8_t RD[8] = { 0x01, 0x03, 0x00, 0x00, 0x00, 0x0C, 0x45, 0xCF };
/* send a read request; return cell 4 from the reply, -1 no reply, -2 bad CRC */
static int poll_cell4(void) {
    txlen = 0;
    for (int i = 0; i < 8; i++) { tim2_regs.CNT += 1042; *rx1 = RD[i]; HAL_UART_RxCpltCallback(&huart1); }
    step(8);
    if (!txlen) return -1;
    if (crc(tx, txlen - 2) != (tx[txlen - 2] | tx[txlen - 1] << 8)) return -2;
    return tx[9] << 8 | tx[10];
}
static int tick_reg(void) { return tx[23] << 8 | tx[24]; }
/* events since the last mark */
static void set_mark(void) { mark = vlen; }
static int has(const char *needle) { vcp[vlen] = 0; return strstr(vcp + mark, needle) != NULL; }
static long num_after(const char *key) {   /* first number after "key": since mark */
    vcp[vlen] = 0; char *p = strstr(vcp + mark, key); return p ? strtol(p + strlen(key), NULL, 10) : -1;
}
static void heartbeat(int ms) { for (int i = 0; i < ms; i++) { if (i % 20 == 0) pb5 = !pb5; ms1(); } }

int main(void) {
    rcc_regs.CSR = RCC_CSR_PINRSTF;
    tick_ms = 1; tim2_regs.CNT = 1000;
    app_init(); step(300);
    CHECK(has("\"fw\":\"gridtwin-2\"") && has("\"reset\":\"reset_pin\""), "boot event with firmware version and reset reason");
    CHECK(iwdg_regs.KR == 0xAAAAu && iwdg_regs.RLR == 1000u, "independent watchdog started and kicked");
    CHECK(exti_regs.RTSR1 & EXTI_RTSR1_RT4 && exti_regs.FTSR1 & EXTI_FTSR1_FT4, "trip wire interrupt on both edges");
    CHECK(poll_cell4() < 3400, "floating pot ignored at boot");

    /* --- legacy contract: high = trip --- */
    cmd("baseline"); step(300); set_mark();
    cmd("drift 400"); step(100);
    CHECK(has("\"overvoltage\""), "legacy: over-voltage detected");
    wire(1); step(5);
    CHECK(has("\"result\":\"PASS\"") && has("\"cause\":\"trip_line\""), "legacy: rising edge is the trip");
    long lat = num_after("\"latency_us\":");
    CHECK(lat > 0 && lat <= 100000, "legacy: latency measured from the crossing (drift lands on the next 100 ms step)");
    set_mark(); wire(0); step(5);
    CHECK(has("\"premature_release\"") && has("\"result\":\"FAIL\""), "legacy: releasing while a cell is over the limit is a FAIL");
    cmd("drift 0"); step(300);

    /* --- legacy: a cut wire hides the trip (the unsafe old contract) --- */
    cmd("baseline"); step(300); set_mark();
    cmd("fault cut on"); cmd("drift 400"); step(50); wire(1); step(3200);
    CHECK(has("\"no_trip\""), "legacy: cut wire -> controller trips but the battery never sees it (FAIL no_trip)");
    wire(0); cmd("fault clear"); cmd("drift 0"); step(300);

    /* --- failsafe contract: permissive high + heartbeat --- */
    set_mark(); cmd("interlock failsafe"); step(10);
    CHECK(has("\"ok\":\"interlock failsafe\""), "failsafe mode accepted");
    cmd("status"); step(5);
    CHECK(has("\"trip\":1"), "failsafe: no permissive signal = interlock open");
    set_mark(); wire(1); step(300);                      /* permissive high but no heartbeat */
    CHECK(has("\"spurious_trip\"") && has("heartbeat_lost"), "failsafe: no heartbeat -> interlock opens (a hung controller is safe)");
    heartbeat(300);
    cmd("baseline"); heartbeat(300); set_mark();
    cmd("drift 400"); heartbeat(150); wire(0); heartbeat(5);
    lat = num_after("\"latency_us\":");
    CHECK(has("\"cause\":\"trip_line\"") && lat > 50000 && lat <= 150000, "failsafe: dropping the permissive line is the trip");
    cmd("drift 0"); heartbeat(100); wire(1); heartbeat(400);
    CHECK(has("\"rearmed\""), "failsafe: re-armed after release below the limit");

    set_mark(); cmd("drift 400"); step(400);            /* controller freezes: heartbeat stops, wire stays high */
    lat = num_after("\"latency_us\":");
    CHECK(has("\"cause\":\"heartbeat_lost\"") && lat > 100000 && lat <= 260000, "failsafe: frozen controller -> watchdog trips within 250 ms");
    cmd("drift 0"); heartbeat(600);

    set_mark(); cmd("fault cut on"); heartbeat(20);
    CHECK(has("\"spurious_trip\"") && has("wire_cut"), "failsafe: a cut wire opens the interlock");
    set_mark(); cmd("drift 400"); heartbeat(150);
    CHECK(has("already_tripped"), "failsafe: over-voltage after a cut wire is already safe");
    cmd("fault clear"); cmd("drift 0"); heartbeat(600);
    CHECK(poll_cell4() < 3400, "failsafe: back to normal");

    /* --- worst-case timing: drift lands after the reply has left the wire, no extra time step --- */
    cmd("interlock legacy"); cmd("baseline"); step(300);
    set_mark(); cmd("drift sync 400"); step(30);
    CHECK(!has("drift_applied"), "sync: waits for a reply");
    int before = poll_cell4(); int t_before = tick_reg();
    CHECK(before < 3400 && has("\"timing\":\"after_reply\""), "sync: drift applied right after the reply");
    int after = poll_cell4(); int t_after = tick_reg();
    CHECK(after >= 3650 && t_after - t_before <= 1, "sync: over-limit on the next reply, simulated time not pushed forward");
    wire(1); step(5); wire(0); cmd("drift 0"); step(300);

    /* --- burst: exactly n replies lost after the drift --- */
    cmd("baseline"); step(300); cmd("fault burst 2"); cmd("drift sync 400"); poll_cell4();
    int a = poll_cell4(), b = poll_cell4(), c = poll_cell4();
    CHECK(a == -1 && b == -1 && c >= 3650, "burst: next two replies lost, then the over-voltage is visible");
    wire(1); step(5); wire(0); cmd("fault clear"); cmd("drift 0"); step(300);

    /* --- command parsing --- */
    set_mark(); cmd("i abc"); cmd("drift 12x"); cmd("fault drop 5%"); cmd("fault burst 11"); cmd("interlock maybe");
    CHECK(!has("\"ok\""), "malformed numbers and values are rejected, not read as 0");
    set_mark(); cmd("i -20"); CHECK(has("\"ok\":\"i -20\""), "valid negative number accepted");

    /* --- output never blocks; telemetry stays valid JSON --- */
    for (int k = 0; k < 300; k++) cmd("help");
    set_mark(); cmd("status"); step(50);
    vcp[vlen] = 0; char *t = strstr(vcp + mark, "{\"cells\"");
    CHECK(t && strstr(t, "\"vcp_drop\":") && strstr(t, "}\r\n"), "telemetry line complete under load");

    printf("%d/%d checks passed\n", checks - failures, checks);
    return failures ? 1 : 0;
}
