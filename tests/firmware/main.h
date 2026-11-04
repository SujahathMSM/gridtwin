/* Host-test stub of the STM32 HAL: just enough to compile and drive gridtwin_app.c on a PC. */
#pragma once
#include <stdint.h>
typedef enum { HAL_OK = 0, HAL_ERROR = 1, HAL_BUSY = 2 } HAL_StatusTypeDef;
typedef struct { volatile uint32_t CNT, PSC, ARR, EGR; } TIM_TypeDef;
typedef struct { int dummy; } USART_TypeDef;
typedef struct { int dummy; } GPIO_TypeDef;
typedef struct { volatile uint32_t CFGR, CSR; } RCC_TypeDef;
typedef struct { volatile uint32_t KR, PR, RLR, SR; } IWDG_TypeDef;
typedef struct { volatile uint32_t APB1FZR1; } DBGMCU_TypeDef;
typedef struct { volatile uint32_t RTSR1, FTSR1; } EXTI_TypeDef;
typedef struct { USART_TypeDef *Instance; struct { uint32_t BaudRate; } Init; volatile uint32_t gState; volatile uint32_t RxState; volatile uint32_t ErrorCode; } UART_HandleTypeDef;
typedef struct { TIM_TypeDef *Instance; } TIM_HandleTypeDef;
typedef struct { int dummy; } ADC_HandleTypeDef;
typedef struct { uint32_t Pin, Mode, Pull, Speed; } GPIO_InitTypeDef;
typedef enum { GPIO_PIN_RESET = 0, GPIO_PIN_SET } GPIO_PinState;
typedef enum { EXTI4_IRQn = 10, USART1_IRQn = 37, LPUART1_IRQn = 91 } IRQn_Type;
#define HAL_UART_STATE_READY 0x20u
#define HAL_UART_STATE_BUSY_RX 0x22u
#define HAL_UART_ERROR_PE 1u
#define HAL_UART_ERROR_NE 2u
#define HAL_UART_ERROR_FE 4u
#define HAL_UART_ERROR_ORE 8u
#define GPIO_PIN_4 0x0010u
#define GPIO_PIN_5 0x0020u
#define GPIO_MODE_INPUT 0u
#define GPIO_PULLDOWN 2u
#define TIM_EGR_UG 1u
#define RCC_CFGR_PPRE1_2 (1u << 10)
#define RCC_CSR_RMVF (1u << 23)
#define RCC_CSR_PINRSTF (1u << 26)
#define RCC_CSR_BORRSTF (1u << 27)
#define RCC_CSR_SFTRSTF (1u << 28)
#define RCC_CSR_IWDGRSTF (1u << 29)
#define DBGMCU_APB1FZR1_DBG_IWDG_STOP (1u << 12)
#define EXTI_RTSR1_RT4 (1u << 4)
#define EXTI_FTSR1_FT4 (1u << 4)
#define ADC_SINGLE_ENDED 0x7Fu
extern TIM_TypeDef tim2_regs; extern RCC_TypeDef rcc_regs; extern IWDG_TypeDef iwdg_regs;
extern DBGMCU_TypeDef dbg_regs; extern EXTI_TypeDef exti_regs;
extern GPIO_TypeDef gpioa, gpiob;
#define RCC (&rcc_regs)
#define IWDG (&iwdg_regs)
#define DBGMCU (&dbg_regs)
#define EXTI (&exti_regs)
#define GPIOA (&gpioa)
#define GPIOB (&gpiob)
uint32_t HAL_GetTick(void);
uint32_t HAL_RCC_GetPCLK1Freq(void);
HAL_StatusTypeDef HAL_TIM_Base_Start(TIM_HandleTypeDef *h);
HAL_StatusTypeDef HAL_ADCEx_Calibration_Start(ADC_HandleTypeDef *h, uint32_t m);
HAL_StatusTypeDef HAL_ADC_Start(ADC_HandleTypeDef *h);
HAL_StatusTypeDef HAL_ADC_PollForConversion(ADC_HandleTypeDef *h, uint32_t t);
uint32_t HAL_ADC_GetValue(ADC_HandleTypeDef *h);
HAL_StatusTypeDef HAL_ADC_Stop(ADC_HandleTypeDef *h);
GPIO_PinState HAL_GPIO_ReadPin(GPIO_TypeDef *p, uint16_t pin);
void HAL_GPIO_TogglePin(GPIO_TypeDef *p, uint16_t pin);
void HAL_GPIO_Init(GPIO_TypeDef *p, GPIO_InitTypeDef *i);
void HAL_NVIC_SetPriority(IRQn_Type irq, uint32_t pre, uint32_t sub);
HAL_StatusTypeDef HAL_UART_Transmit_IT(UART_HandleTypeDef *h, const uint8_t *d, uint16_t n);
HAL_StatusTypeDef HAL_UART_Receive_IT(UART_HandleTypeDef *h, uint8_t *d, uint16_t n);
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *h);
void HAL_UART_TxCpltCallback(UART_HandleTypeDef *h);
void HAL_UART_ErrorCallback(UART_HandleTypeDef *h);
void HAL_GPIO_EXTI_Callback(uint16_t pin);
static inline void __disable_irq(void) {}
static inline void __enable_irq(void) {}
static inline void __DMB(void) {}
