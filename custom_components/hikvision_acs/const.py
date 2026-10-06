"""海康门禁 HA 集成的常量定义。"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hikvision_acs"
MANUFACTURER: Final = "Hikvision"

# 配置项
CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_DOOR: Final = "door"
CONF_RTSP_CHANNEL: Final = "rtsp_channel"
CONF_USE_HTTP_LISTENING: Final = "use_http_listening"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_DOORBELL_SECONDS: Final = "doorbell_seconds"
CONF_SYNC_TIME: Final = "sync_time"

# 默认值
DEFAULT_USERNAME: Final = "admin"
DEFAULT_DOOR: Final = 1
DEFAULT_RTSP_CHANNEL: Final = 102          # 子码流：起流快，适合门铃预览
DEFAULT_SCAN_INTERVAL: Final = 30          # 秒；这是兜底轮询，主通道是设备推送
DEFAULT_DOORBELL_SECONDS: Final = 30
DEFAULT_USE_HTTP_LISTENING: Final = True
# 设备时钟偏差超过这个秒数才校时（避免频繁写设备）
CLOCK_SYNC_THRESHOLD: Final = 10
# 轮询窗口右端往后放宽这么多，容忍设备时钟偏快
CLOCK_SKEW_TOLERANCE: Final = 300

# 属性名
ATTR_PERSON: Final = "person"
ATTR_METHOD: Final = "method"
ATTR_TIME: Final = "time"
ATTR_DOOR: Final = "door"
ATTR_EVENT_NAME: Final = "event_name"
ATTR_REMOTE_HOST: Final = "remote_host"
ATTR_SERIAL: Final = "serial_no"

# 事件实体触发的事件类型（供自动化使用）
EVENT_TYPE_DOORBELL: Final = "doorbell"
EVENT_TYPE_ACCESS_GRANTED: Final = "access_granted"
EVENT_TYPE_ACCESS_DENIED: Final = "access_denied"
EVENT_TYPE_DOOR_OPENED: Final = "door_opened"
EVENT_TYPE_DOOR_CLOSED: Final = "door_closed"
EVENT_TYPE_EXIT_BUTTON: Final = "exit_button"
EVENT_TYPE_REMOTE_OPEN: Final = "remote_open"

EVENT_TYPES: Final = [
    EVENT_TYPE_DOORBELL,
    EVENT_TYPE_ACCESS_GRANTED,
    EVENT_TYPE_ACCESS_DENIED,
    EVENT_TYPE_DOOR_OPENED,
    EVENT_TYPE_DOOR_CLOSED,
    EVENT_TYPE_EXIT_BUTTON,
    EVENT_TYPE_REMOTE_OPEN,
]

# HA 事件总线上广播的事件名（供高级自动化 / 蓝图使用）
HA_EVENT_NEW_EVENT: Final = f"{DOMAIN}_event"

SERVICE_OPEN_DOOR: Final = "open_door"

# 一次保留在内存里的事件条数（用于"最后一条事件"等状态）
KEEP_EVENTS: Final = 50
