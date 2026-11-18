idf_component_register(SRCS "slcan_main.c" "slcan_proto.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_twai esp_driver_usb_serial_jtag esp_timer)
