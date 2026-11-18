cmake_minimum_required(VERSION 3.22)

# ESP32-C6 SLCAN adapter (Lawicel over USB-Serial/JTAG), ESP-IDF 6.1.1.
set(SLCAN_IDF_VERSION "6.1.1")

set(IDF_TARGET esp32c6)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# IDF_VERSION_* come from project.cmake (ESP_IDF_VERSION from EIM is only "major.minor").
set(_idf_ver "${IDF_VERSION_MAJOR}.${IDF_VERSION_MINOR}.${IDF_VERSION_PATCH}")
if(NOT _idf_ver STREQUAL SLCAN_IDF_VERSION)
    message(WARNING "esp32c6-slcan is tested on ESP-IDF ${SLCAN_IDF_VERSION}, building with ${_idf_ver}")
endif()

project(esp32c6_slcan)
