cmake_minimum_required(VERSION 3.22)

# ESP32-C6 SLCAN adapter (Lawicel over USB-Serial/JTAG), ESP-IDF 6.1.1.
set(SLCAN_IDF_VERSION "6.1.1")
if(DEFINED ENV{ESP_IDF_VERSION} AND NOT "$ENV{ESP_IDF_VERSION}" STREQUAL "${SLCAN_IDF_VERSION}")
    message(WARNING "esp32c6-slcan is tested on ESP-IDF ${SLCAN_IDF_VERSION}, building with $ENV{ESP_IDF_VERSION}")
endif()

set(IDF_TARGET esp32c6)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32c6_slcan)
