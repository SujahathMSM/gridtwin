/*
 * GridTwin-HIL day 1: Nucleo-G491RE battery emulator + trip-latency timer.
 *
 * Call app_init() once after all MX_..._Init() calls in main(),
 * and app_loop() inside the while(1) loop.
 */
#ifndef GRIDTWIN_APP_H
#define GRIDTWIN_APP_H

void app_init(void);
void app_loop(void);

#endif /* GRIDTWIN_APP_H */
