#pragma once

#include <cstdint>
#include <vector>

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

#ifdef USE_ESP32
#include <esp_gattc_api.h>

namespace esphome {
namespace afu_scale {

class AFUScale : public sensor::Sensor,
                 public Component,
                 public ble_client::BLEClientNode,
                 public esp32_ble_tracker::ESPBTDeviceListener {
 public:
  void dump_config() override;
  void setup() override;
  float get_setup_priority() const override;

  bool parse_device(const esp32_ble_tracker::ESPBTDevice &device) override;

  void set_impedance_raw_sensor(sensor::Sensor *sensor) { this->impedance_raw_sensor_ = sensor; }
  void set_settle_time(uint32_t settle_time_ms) { this->settle_time_ms_ = settle_time_ms; }

 protected:
  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

  bool parse_notify_packet(const uint8_t *data, size_t len);
  bool parse_adv_data_for_packet(const std::vector<uint8_t> &data);
  void update_measurement_(int32_t weight_g, bool is_stable, uint16_t impedance_raw);
  void reset_measurement_();

  sensor::Sensor *impedance_raw_sensor_{nullptr};
  uint32_t settle_time_ms_{2000};
  int32_t pending_weight_g_{0};
  uint16_t pending_impedance_raw_{0};
  uint32_t weight_unchanged_since_{0};
  bool has_pending_measurement_{false};
  bool pending_stable_{false};
  bool measurement_published_{false};
};

}  // namespace afu_scale
}  // namespace esphome

#endif  // USE_ESP32
