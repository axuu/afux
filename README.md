# AFU 体脂秤 ESPHome 集成

用 ESP32 通过 BLE 读取 AFU-WL-TZ-A1 类体脂秤，并把每次称重的最终体重和原始阻抗发送到 Home Assistant 与自托管数据服务。

组件只采集设备数据：不保存身高、年龄、性别，也不在设备端推算 BMI、体脂率、水分率、肌肉量、蛋白质或骨量。派生指标和个人参数留给消费端处理。

协议解析参考 [smart-body-scale-android](https://github.com/maoziban/smart-body-scale-android)。

## 数据行为

| 实体 | 内容 | 单位 |
| --- | --- | --- |
| `体重` | 协议重量字段换算后的最终体重 | kg |
| `原始阻抗` | 报文字节 8–9 的无符号 16 位原始值 | 未知 |

原始阻抗不标记为 Ω，因为目前没有可靠依据确认原厂的缩放和校准关系。

一次称重按以下规则发布：

1. 收到秤的稳定标志。
2. 重量连续 `settle_time` 未变化。
3. 先发布原始阻抗，再发布体重作为本次测量的提交事件。
4. 离秤并收到零重量后，才接受下一次测量。

两个实体都启用了 `force_update`，所以连续两次结果完全相同也会产生两条测量事件。

## Docker 数据服务

仓库内置一个零依赖 Python 服务，同时提供：

- `POST /api/measurements`：接收称重数据
- `GET /api/measurements`：查询最近记录
- `/`：查看最新读数、体重趋势和原始历史
- SQLite 持久化：数据库位于 Docker volume 的 `/data/measurements.db`

### Coolify 部署

在 Coolify 项目中创建新资源，选择 Git 仓库，然后设置：

- Branch：`main`
- Build Pack：`Docker Compose`
- Base Directory：`/`
- Docker Compose Location：`/compose.yaml`

私有仓库使用 GitHub App 或 Deploy Key。先在本机生成两个随机值：

```bash
openssl rand -hex 32
openssl rand -base64 24
```

然后在 Coolify 的 Environment Variables 中填写，三个变量都只启用 Runtime Variable：

```dotenv
API_TOKEN=第一条命令的输出
WEB_USERNAME=admin
WEB_PASSWORD=第二条命令的输出
```

最后为 `scale-api` 服务设置域名：

```text
https://scale.example.com:8080
```

这里的 `8080` 是容器内部端口，公网仍由 Coolify 使用标准 HTTPS 端口。部署后访问 `/healthz`，应返回 `{"status":"ok"}`。Compose 中的 `scale-data` volume 会保留 SQLite 数据，不要另外创建重复挂载。

浏览器打开服务地址后，使用 Coolify 中的 `WEB_USERNAME` 和 `WEB_PASSWORD` 登录。

写入接口使用 `X-API-Key`：

```bash
curl -X POST https://scale.example.com/api/measurements \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_API_TOKEN' \
  -d '{"device_id":"body-scale","weight_g":78350,"impedance_raw":1344}'
```

`measurement_id` 和 `measured_at` 可以省略，由服务器生成。客户端重试时如需严格防重，应主动发送稳定的 `measurement_id`。

## 准备

- 一块经典 ESP32 开发板，当前示例使用 `esp32dev`
- AFU 系列体脂秤
- ESPHome 与 Home Assistant
- ESP32 首次刷写所需的 USB 连接

## 快速开始

```bash
git clone git@github.com:axuu/afux.git
cd afux
cp secrets.example.yaml secrets.yaml
openssl rand -base64 32
```

把最后一条命令的输出填入 `secrets.yaml` 的 `api_encryption_key`，再填写 Wi-Fi、秤的 BLE MAC、公网 API 地址，以及与 Coolify 中 `API_TOKEN` 相同的上传 token。然后执行：

```bash
esphome config scale.yaml
esphome run scale.yaml
```

首次刷写选择 USB 串口；以后设备联网后可以使用 OTA。Home Assistant 发现 `body-scale` 后，使用同一个 `api_encryption_key` 完成 ESPHome 集成。

## Secrets 配置

真实配置保存在与 `scale.yaml` 同目录的 `secrets.yaml`：

```yaml
wifi_ssid: "你的 Wi-Fi 名称"
wifi_password: "你的 Wi-Fi 密码"
api_encryption_key: "openssl rand -base64 32 的输出"
scale_mac_address: "AA:BB:CC:DD:EE:FF"
measurement_api_url: "https://scale.example.com/api/measurements"
measurement_api_token: "与 Coolify 中 API_TOKEN 相同"
```

| 配置项 | 用途 | 是否敏感 |
| --- | --- | --- |
| `wifi_ssid` | ESP32 连接的无线网络 | 建议隐藏 |
| `wifi_password` | Wi-Fi 密码 | 是 |
| `api_encryption_key` | ESPHome 与 Home Assistant 的 API 加密 | 是 |
| `scale_mac_address` | 目标体脂秤的 BLE 地址 | 否，但属于设备实例配置 |
| `measurement_api_url` | Docker 服务的公网 HTTPS 写入地址 | 否 |
| `measurement_api_token` | 设备写入 API 的凭据 | 是 |

仓库只提交 [`secrets.example.yaml`](secrets.example.yaml)，真实的 `secrets.yaml` 已被 `.gitignore` 排除。提交前可检查：

```bash
git check-ignore secrets.yaml
git status --short
```

不要把真实密钥粘贴到 README、Issue、日志或提交记录中。如果 API key 曾经提交到 Git，请生成新 key；刷写新固件后还要同步更新 Home Assistant 中该设备的加密 key。

## 常用配置

完整配置见 [`scale.yaml`](scale.yaml)。通常只需要填写 `secrets.yaml`，并按实际情况调整稳定等待时间：

```yaml
sensor:
  - platform: afu_scale
    ble_client_id: scale_client
    name: "体重"
    force_update: true
    on_value:
      then:
        - http_request.post:
            url: !secret measurement_api_url
    settle_time: 2s
    impedance_raw:
      name: "原始阻抗"
      force_update: true
```

如果身体轻微晃动导致迟迟不发布，可缩短 `settle_time`；如果仍会过早记录，则加长它。建议从 `2s` 开始。

## 消费端记录

服务在收到体重更新时同时保存刚发布的原始阻抗。每条记录包含：

```text
measurement_id
device_id
measured_at
received_at
weight_g
impedance_raw
```

其中时间与测量 ID 默认由服务端生成；个人参数和派生指标仍由消费端管理。

## 排查

- `Secret ... is not defined`：确认 `secrets.yaml` 与 `scale.yaml` 位于同一目录，且四个键名完全一致。
- 无法连接秤：确认 MAC 地址正确、秤已唤醒，并退出可能占用 BLE 连接的原厂 App。
- 没有测量事件：先观察 DEBUG 日志，再调整 `settle_time`。
- 修改 API key 后设备在 HA 中离线：重新配置 ESPHome 集成，填入新 key。
- 日志出现 `Upload failed`：检查公网 URL、HTTPS 证书及 Docker 服务健康状态。
- HA 中仍有旧版 BMI 等实体：它们会变为不可用，可从实体注册表中删除。

逐包报文只在 `VERY_VERBOSE` 日志级别输出；日常运行保留 `DEBUG` 即可。
