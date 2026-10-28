# GridTwin-HIL — Day 1 kit

Goal for today: the Nucleo pretends to be a 4-cell LiFePO4 battery, the Raspberry Pi
polls it over Modbus RTU and pulls a safety-trip line when a cell goes over-voltage,
and the Nucleo measures **how long the Pi took to trip**, to the microsecond.

```
 laptop ──USB (ST-Link)── Nucleo-G491RE ──UART 9600 (Modbus RTU)── Raspberry Pi 5
  monitor.py                battery emulator      ◄── trip line (GPIO23 → PB4) ──   controller.py
                            + trip timer                                            (+ red LED)
```

| Folder | What | Runs on |
|---|---|---|
| `firmware/` | `gridtwin_app.c/.h` — battery model, Modbus slave, trip timer | Nucleo (STM32CubeIDE) |
| `pi/` | `controller.py` — the controller under test | Raspberry Pi 5 |
| `laptop/` | `monitor.py` — live view, trip latency, commands | Laptop |

---

## 1. Wiring

Both boards use 3.3 V logic, so they connect directly. Power each board from its own USB.

| From | To | Notes |
|---|---|---|
| Pi pin 8 (GPIO14, TX) | Nucleo **PC5** (Arduino **D0**) | USART1 RX |
| Pi pin 10 (GPIO15, RX) | Nucleo **PC4** (Arduino **D1**) | USART1 TX |
| Pi pin 6 (GND) | Nucleo GND | shared ground — required |
| Pi pin 16 (GPIO23) | Nucleo **PB4** (Arduino **D5**) | safety-trip line |
| Pi pin 16 (GPIO23) | 330 Ω → red LED (long leg) → LED short leg → GND | lights when tripped |
| trip line | 10 kΩ → GND | pull-down, no false trips at boot |
| Potentiometer outer legs | Nucleo 3V3 and GND | **not 5V** |
| Potentiometer middle leg | Nucleo **PA0** (Arduino **A0**) | turns up cell 4's voltage |

Use the breadboard rails for GND. Pin labels are from the NUCLEO-G491RE Arduino header;
double-check against the pinout card in the box or ST's user manual UM2505 if anything looks off.

---

## 2. Nucleo firmware (STM32CubeIDE)

**Create the project**
1. File → New → STM32 Project → **Board Selector** → `NUCLEO-G491RE` → name it `gridtwin`.
2. "Initialize all peripherals with their default mode?" → **Yes**.

> If the generated `main.c` uses `BSP_COM_Init(...)` / `hcom_uart` instead of `hlpuart1`,
> switch off the BSP virtual COM port (COM1) in the board-support settings of the `.ioc`
> and enable LPUART1 yourself as below. The firmware expects a handle named `hlpuart1`.

**Configure in the `.ioc` (Pinout & Configuration)**

| Peripheral | Setting |
|---|---|
| Connectivity → **LPUART1** | Asynchronous, 115200 8N1, pins PA2/PA3. NVIC: *LPUART1 global interrupt* ✔ |
| Connectivity → **USART1** | Asynchronous, **9600**, 8 bits, no parity, 1 stop. Pins **PC4 = TX, PC5 = RX** (click the pins to reassign if CubeMX chose PA9/PA10). NVIC: *USART1 global interrupt* ✔ |
| Timers → **TIM2** | Clock Source: *Internal Clock*. Counter Period: `4294967295`. Leave the prescaler at 0 — the firmware sets it to 1 MHz itself. |
| Analog → **ADC1** | IN1: *IN1 Single-ended* (PA0). Defaults are fine. |
| Pin **PB4** | Click → *GPIO_EXTI4*. System Core → GPIO → PB4: *External Interrupt, Rising edge*, **Pull-down**. NVIC: *EXTI line4 interrupt* ✔ |
| Pin **PA5** | GPIO_Output (on-board LED LD2 — the board default already does this) |

Leave the clock settings as generated. Save, and let it generate code.

**Add the GridTwin code**
1. Copy `gridtwin_app.c` → `Core/Src/`, `gridtwin_app.h` → `Core/Inc/`.
2. In `Core/Src/main.c`, inside the USER CODE blocks only:

```c
/* USER CODE BEGIN Includes */
#include "gridtwin_app.h"
/* USER CODE END Includes */

/* USER CODE BEGIN 2 */
app_init();
/* USER CODE END 2 */

while (1)
{
  /* USER CODE END WHILE */
  /* USER CODE BEGIN 3 */
  app_loop();
}
/* USER CODE END 3 */
```

3. Don't define `HAL_UART_RxCpltCallback`, `HAL_UART_ErrorCallback` or `HAL_GPIO_EXTI_Callback`
   anywhere else — `gridtwin_app.c` provides them.
4. Build (hammer) and Run (green play) to flash. LD2 should blink twice a second.

---

## 3. Raspberry Pi setup

```bash
# 1. Turn on the UART on GPIO14/15
sudo nano /boot/firmware/config.txt      # add this line at the end:
#     dtparam=uart0=on
sudo raspi-config                        # Interface Options → Serial Port:
                                         #   login shell over serial? → No
                                         #   serial port hardware?    → Yes
sudo reboot
ls -l /dev/ttyAMA0                       # should exist after reboot

# 2. Python packages (both ship with Raspberry Pi OS Bookworm; this just makes sure)
sudo apt install -y python3-serial python3-gpiozero

# 3. Copy controller.py to the Pi and run it
python3 controller.py
```

Options: `--poll-ms 100` (poll faster), `--limit-mv 3650`, `--port /dev/ttyAMA0`.

---

## 4. Laptop monitor

```bash
pip install pyserial
python monitor.py --port COM5            # Windows: Device Manager → Ports →
                                         # "STMicroelectronics STLink Virtual COM Port (COMx)"
python monitor.py --port /dev/ttyACM0    # Linux
```

Type commands and press Enter: `help`, `status`, `i 80` (charge current, A),
`drift 300` (fixed drift on cell 4, mV), `drift pot` (back to the knob), `limit 3650`,
`deadline 500` (pass/fail threshold, ms), `soc 90`, `grid 190`, `quit`.

---

## 5. The day-1 test

1. Flash the Nucleo, start `monitor.py`. You should see a `boot` line, then a status line every second.
2. Start `controller.py` on the Pi. In the monitor, **`polls` should start climbing**.
   If it stays at 0, see Troubleshooting.
3. Turn the knob to minimum. Cells sit around 3.33 V, trip `off`.
4. Slowly turn the knob up. Cell 4 rises; when it crosses 3650 mV:
   - the monitor prints `--> over-voltage … timer started`,
   - the Pi prints `TRIP: cell 4 …` and the red LED lights,
   - the monitor prints `*** TRIP PASS total XX ms (until reported …, Pi reaction …)`.
5. Turn the knob back down → the Pi prints `released`, the monitor prints `[rearmed]`.
6. Repeat ~10 times, then type `quit` in the monitor to get min / max / average latency.

**What to expect.** With the default 200 ms poll, total latency should land roughly between
40 and 250 ms: most of it is waiting for the next poll (*until reported*); the Modbus reply at
9600 baud plus the Pi's own processing (*Pi reaction*) adds a few tens of ms.
Run the controller with `--poll-ms 50` and watch the latency drop — that's your first real finding.

**Check the judge works.** Stop `controller.py` (Ctrl+C), turn the knob up, and wait 3 s:
the monitor must print `*** FAIL no trip`. A test bench that never fails is not testing anything.

**Day 1 is done when:** `polls` climbs, trips are timed and graded PASS/FAIL, and a missing
trip is reported as FAIL.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Monitor shows nothing | Wrong COM port, or LPUART1 not set up / BSP COM still enabled. Check LD2 blinks. |
| `polls` stays 0, Pi prints `no reply (timeout)` | TX/RX swapped, no shared GND, USART1 on PA9/PA10 instead of PC4/PC5, USART1 interrupt not enabled, or `dtparam=uart0=on` missing. |
| Pi prints `bad CRC` | Baud mismatch (both must be 9600) or a loose jumper. |
| `spurious_trip` events | Trip line floating — fit the 10 kΩ pull-down. |
| Cell 4 never rises | Pot on 5V or middle leg not on PA0; ADC1 IN1 not enabled; or you typed `drift <n>` (type `drift pot`). |
| Latency is 0 or wildly wrong | TIM2 isn't set to *Internal Clock*, or its Counter Period isn't `4294967295`. |

## Modbus register map (slave address 1)

| Reg | Meaning | Unit |
|---|---|---|
| 0–3 | cell 1–4 voltage | mV |
| 4 | pack voltage | 10 mV |
| 5 | pack current (+ = charging) | 0.1 A, signed |
| 6 | temperature | 0.1 °C |
| 7 | state of charge | 0.1 % |
| 8 | grid voltage | V |
| 9 | status (bit0 trip line high, bit1 drift from knob) | — |
| 10 | simulation tick (+1 every 100 ms) | — |
| 11 | charge current limit — **writable** with function 0x06 | 0.1 A |

Next (day 2): fault-injection commands on the Nucleo — stuck values, split and delayed
Modbus replies, grid sag — which the agents will later combine into attack scenarios.
