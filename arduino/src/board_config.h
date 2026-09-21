/*
 * board_config.h
 *
 * Selects the correct board_config_*.h based on a macro set in this
 * environment's build_flags in platformio.ini. packet_protocol.h and
 * arduino_node.ino include this instead of a specific board file, so
 * neither has to know which board it's building for.
 */

#ifndef BOARD_CONFIG_SELECTOR_H
#define BOARD_CONFIG_SELECTOR_H

#if defined(BOARD_FEATHER_M0)
  #include "board_config_feather_m0.h"
#elif defined(BOARD_PICO)
  #include "board_config_pico.h"
#else
  #error "No board selected — set BOARD_FEATHER_M0 or BOARD_PICO via build_flags in platformio.ini"
#endif

#endif