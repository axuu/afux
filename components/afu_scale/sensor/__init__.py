import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import ble_client, esp32_ble_tracker, sensor
from esphome.const import DEVICE_CLASS_WEIGHT, STATE_CLASS_MEASUREMENT, UNIT_KILOGRAM

from .. import afu_scale_ns

CODEOWNERS = ["@your_github_username"]
DEPENDENCIES = ["ble_client", "esp32_ble_tracker"]

AFUScale = afu_scale_ns.class_(
    "AFUScale",
    sensor.Sensor,
    cg.Component,
    ble_client.BLEClientNode,
    esp32_ble_tracker.ESPBTDeviceListener,
)

CONF_IMPEDANCE_RAW = "impedance_raw"
CONF_SETTLE_TIME = "settle_time"

CONFIG_SCHEMA = (
    sensor.sensor_schema(
        AFUScale,
        unit_of_measurement=UNIT_KILOGRAM,
        accuracy_decimals=2,
        device_class=DEVICE_CLASS_WEIGHT,
        state_class=STATE_CLASS_MEASUREMENT,
    )
    .extend(
        {
            cv.Optional(CONF_IMPEDANCE_RAW): sensor.sensor_schema(
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_SETTLE_TIME, default="2s"): cv.positive_time_period_milliseconds,
        }
    )
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = await sensor.new_sensor(config)
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
    await esp32_ble_tracker.register_ble_device(var, config)

    cg.add(var.set_settle_time(config[CONF_SETTLE_TIME]))
    if CONF_IMPEDANCE_RAW in config:
        impedance_raw = await sensor.new_sensor(config[CONF_IMPEDANCE_RAW])
        cg.add(var.set_impedance_raw_sensor(impedance_raw))
