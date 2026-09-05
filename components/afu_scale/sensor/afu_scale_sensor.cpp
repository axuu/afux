#include "afu_scale_sensor.h"

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#ifdef USE_ESP32

namespace esphome {
namespace afu_scale {

static const char *const TAG = "afu_scale";

static const esp32_ble_tracker::ESPBTUUID SERVICE_UUID =
    esp32_ble_tracker::ESPBTUUID::from_uint16(0xFFB0);
static const esp32_ble_tracker::ESPBTUUID NOTIFY_CHAR_UUID =
    esp32_ble_tracker::ESPBTUUID::from_uint16(0xFFB2);

static const uint8_t PACKET_MAGIC = 0xAC;
static const uint8_t STABLE_FLAG = 0x02;
static const uint8_t HANDSHAKE[] = {0xFD, 0x37, 0x00, 0x00, 0x00,
                                    0x00, 0x00, 0x00, 0x00, 0x37};

constexpr bool measurement_ready(bool has_pending, bool published, bool stable,
                                 uint32_t elapsed_ms, uint32_t settle_time_ms) {
  return has_pending && !published && stable && elapsed_ms >= settle_time_ms;
}

static_assert(!measurement_ready(true, false, true, 1999, 2000));
static_assert(measurement_ready(true, false, true, 2000, 2000));
static_assert(!measurement_ready(true, false, false, 2000, 2000));
static_assert(!measurement_ready(true, true, true, 2000, 2000));

float AFUScale::get_setup_priority() const { return setup_priority::AFTER_BLUETOOTH; }

void AFUScale::setup() {
  ESP_LOGCONFIG(TAG, "Setting up AFU Scale...");
  if (this->parent() != nullptr) {
    ESP_LOGI(TAG, "AFU Scale configured for BLE MAC: %s", this->parent()->address_str());
  } else {
    ESP_LOGW(TAG, "AFU Scale: ble_client parent not set yet");
  }
}

void AFUScale::dump_config() {
  ESP_LOGCONFIG(TAG, "AFU Scale:");
  if (this->parent() != nullptr) {
    ESP_LOGCONFIG(TAG, "  MAC Address: %s", this->parent()->address_str());
  } else {
    ESP_LOGCONFIG(TAG, "  MAC Address: (not connected)");
  }
  ESP_LOGCONFIG(TAG, "  Settle time: %u ms", static_cast<unsigned>(this->settle_time_ms_));
  LOG_SENSOR("  ", "Weight", this);
  LOG_SENSOR("  ", "Raw impedance", this->impedance_raw_sensor_);
}

bool AFUScale::parse_device(const esp32_ble_tracker::ESPBTDevice &device) {
  bool claimed = false;
  for (auto &md : device.get_manufacturer_datas()) {
    if (this->parse_adv_data_for_packet(md.data)) {
      claimed = true;
    }
  }
  for (auto &sd : device.get_service_datas()) {
    if (this->parse_adv_data_for_packet(sd.data)) {
      claimed = true;
    }
  }

  for (auto &uuid : device.get_service_uuids()) {
    if (uuid == SERVICE_UUID) {
      char addr_buf[MAC_ADDRESS_PRETTY_BUFFER_SIZE];
      ESP_LOGD(TAG, "Scale service found via advertisement (MAC: %s)",
               device.address_str_to(addr_buf));
      claimed = true;
    }
  }
  return claimed;
}

bool AFUScale::parse_adv_data_for_packet(const std::vector<uint8_t> &data) {
  for (size_t i = 0; i < data.size(); i++) {
    if (data[i] == PACKET_MAGIC && (data.size() - i) >= 10) {
      this->parse_notify_packet(data.data() + i, data.size() - i);
      return true;
    }
  }
  return false;
}

void AFUScale::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                   esp_ble_gattc_cb_param_t *param) {
  const char *mac = this->parent() != nullptr ? this->parent()->address_str() : "unknown";

  switch (event) {
    case ESP_GATTC_OPEN_EVT:
      if (param->open.status == ESP_GATT_OK) {
        ESP_LOGI(TAG, "[%s] GATT connection opened (BLE connected)", mac);
      } else {
        ESP_LOGW(TAG, "[%s] GATT open failed, status=%d", mac, param->open.status);
      }
      break;
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto *chr = this->parent()->get_characteristic(SERVICE_UUID, NOTIFY_CHAR_UUID);
      if (chr == nullptr) {
        ESP_LOGW(TAG, "[%s] FFB2 notify characteristic not found, scale may not be AFU type", mac);
        break;
      }
      ESP_LOGI(TAG, "[%s] FFB2 characteristic found, handle=0x%04x", mac, chr->handle);

      auto status = esp_ble_gattc_register_for_notify(this->parent()->get_gattc_if(),
                                                      this->parent()->get_remote_bda(), chr->handle);
      if (status != ESP_OK) {
        ESP_LOGW(TAG, "[%s] esp_ble_gattc_register_for_notify failed, status=%d", mac, status);
      }

      auto *svc = this->parent()->get_service(SERVICE_UUID);
      if (svc != nullptr) {
        for (auto *c : svc->characteristics) {
          if (c->properties & ESP_GATT_CHAR_PROP_BIT_WRITE ||
              c->properties & ESP_GATT_CHAR_PROP_BIT_WRITE_NR) {
            ESP_LOGD(TAG, "[%s] Sending handshake to char handle=0x%04x", mac, c->handle);
            c->write_value((uint8_t *) HANDSHAKE, sizeof(HANDSHAKE), ESP_GATT_WRITE_TYPE_RSP);
          }
        }
      } else {
        ESP_LOGW(TAG, "[%s] FFB0 service not found in parent cache", mac);
      }
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (param->reg_for_notify.status == ESP_GATT_OK) {
        this->node_state = esp32_ble_tracker::ClientState::ESTABLISHED;
        ESP_LOGI(TAG, "[%s] Notify registered, connection ESTABLISHED", mac);
      } else {
        ESP_LOGW(TAG, "[%s] Register for notify failed, status=%d", mac,
                 param->reg_for_notify.status);
      }
      break;
    case ESP_GATTC_NOTIFY_EVT:
      if (param->notify.is_notify) {
        this->parse_notify_packet(param->notify.value, param->notify.value_len);
      }
      break;
    case ESP_GATTC_DISCONNECT_EVT:
      ESP_LOGW(TAG, "[%s] GATT disconnected (reason=0x%x)", mac, param->disconnect.reason);
      break;
    default:
      break;
  }
}

bool AFUScale::parse_notify_packet(const uint8_t *data, size_t len) {
  if (len < 10 || data[0] != PACKET_MAGIC) {
    ESP_LOGVV(TAG, "Ignored packet, len=%u magic=0x%02X", static_cast<unsigned>(len),
              len > 0 ? data[0] : 0);
    return false;
  }

  int32_t weight_g = (static_cast<int32_t>(data[3]) - 0x68) * 65536 +
                     static_cast<int32_t>(data[4]) * 256 + data[5];
  if (weight_g < 0) {
    weight_g = 0;
  }
  const bool is_stable = data[6] == STABLE_FLAG;
  const uint16_t impedance_raw = (static_cast<uint16_t>(data[8]) << 8) | data[9];

  ESP_LOGVV(TAG, "Packet: weight=%dg stable=%s impedance_raw=%u", static_cast<int>(weight_g),
            is_stable ? "YES" : "no", static_cast<unsigned>(impedance_raw));
  this->update_measurement_(weight_g, is_stable, impedance_raw);
  return true;
}

void AFUScale::update_measurement_(int32_t weight_g, bool is_stable, uint16_t impedance_raw) {
  if (weight_g <= 0) {
    this->reset_measurement_();
    return;
  }
  if (this->measurement_published_) {
    return;
  }

  if (!this->has_pending_measurement_ || weight_g != this->pending_weight_g_) {
    this->pending_weight_g_ = weight_g;
    this->pending_impedance_raw_ = impedance_raw;
    this->weight_unchanged_since_ = millis();
    this->has_pending_measurement_ = true;
    this->pending_stable_ = is_stable;
    return;
  }

  this->pending_impedance_raw_ = impedance_raw;
  this->pending_stable_ = is_stable;
  const uint32_t elapsed_ms = millis() - this->weight_unchanged_since_;
  if (!measurement_ready(this->has_pending_measurement_, this->measurement_published_,
                         this->pending_stable_, elapsed_ms, this->settle_time_ms_)) {
    return;
  }

  if (this->impedance_raw_sensor_ != nullptr) {
    this->impedance_raw_sensor_->publish_state(this->pending_impedance_raw_);
  }
  this->publish_state(this->pending_weight_g_ / 1000.0f);
  this->measurement_published_ = true;
  ESP_LOGI(TAG, "Measurement complete: weight=%dg impedance_raw=%u",
           static_cast<int>(this->pending_weight_g_),
           static_cast<unsigned>(this->pending_impedance_raw_));
}

void AFUScale::reset_measurement_() {
  this->has_pending_measurement_ = false;
  this->pending_stable_ = false;
  this->measurement_published_ = false;
}

}  // namespace afu_scale
}  // namespace esphome

#endif  // USE_ESP32
